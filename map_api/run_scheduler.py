"""Transactional DAG reducer. Work runs outside transactions under fenced leases."""
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from jsonschema import Draft202012Validator, ValidationError

from .models import RunArtifact, RunEvidence, RunStep, RunToolCall
from .run_journal import append_locked, digest, lock_run, repeated_command, retry_sqlite_write
from .run_kernel import TERMINAL, _set_state


MANDATORY_STEPS = {"declare_data_requirements", "match_source_capabilities", "quality_gate",
                   "evidence_compilation", "final_review", "publish_artifacts"}
ACTIVE_STATES = {"queued", "planning", "running", "retrying"}


class StepConflict(ValueError):
    def __init__(self, code, message):
        self.details = {"code": code, "message": message}
        super().__init__(message)


def validate_plan(steps, *, mandatory=MANDATORY_STEPS):
    """Validate the whole graph before any persisted plan is changed."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= 60:
        raise StepConflict("invalid_plan", "计划必须包含 1 到 60 个步骤")
    graph = {}
    for item in steps:
        if not isinstance(item, dict):
            raise StepConflict("invalid_step", "步骤必须为结构化对象")
        sid = item.get("step_id")
        if not isinstance(sid, str) or not sid or len(sid) > 120 or sid in graph:
            raise StepConflict("invalid_step_id", "步骤标识为空、重复或过长")
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("kind", "label", "purpose")):
            raise StepConflict("missing_step_contract", "每个步骤必须声明类型、名称和目的")
        deps = item.get("depends_on")
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps) or len(set(deps)) != len(deps):
            raise StepConflict("invalid_dependencies", "步骤依赖必须为唯一标识列表")
        schema = item.get("completion_schema")
        if not isinstance(schema, dict) or not schema:
            raise StepConflict("missing_completion_condition", "每个步骤必须声明输出完成条件")
        Draft202012Validator.check_schema(schema)
        if sid in mandatory and item.get("optional"):
            raise StepConflict("mandatory_step", "数据、质量、证据和最终复核步骤不能跳过")
        if not isinstance(item.get("max_attempts", 3), int) or not 1 <= item.get("max_attempts", 3) <= 5:
            raise StepConflict("invalid_retry_policy", "步骤尝试次数必须在 1 到 5 之间")
        graph[sid] = deps
    if not mandatory.issubset(graph):
        raise StepConflict("mandatory_step_missing", "计划缺少数据声明、能力匹配、质量、证据或最终复核")
    pending = set(graph)
    resolved = set()
    while pending:
        ready = {sid for sid in pending if set(graph[sid]).issubset(resolved)}
        if not ready:
            raise StepConflict("invalid_dag", "计划存在循环或引用了不存在的依赖")
        resolved.update(ready)
        pending.difference_update(ready)
    return steps


@retry_sqlite_write
@transaction.atomic
def install_plan(run_id, steps, *, context, command_key, mandatory=MANDATORY_STEPS):
    validate_plan(steps, mandatory=mandatory)
    run = lock_run(run_id)
    fingerprint = digest({"steps": steps, "context": context})
    if repeated_command(run, command_key, fingerprint):
        return run
    if run.status not in {"queued", "planning", "waiting_user", "failed", "blocked", "not_supported", "external_service_unavailable"}:
        raise StepConflict("plan_busy", "请先暂停执行再修改计划")
    if run.steps.filter(status="running", lease_until__gt=timezone.now()).exists():
        raise StepConflict("active_lease", "步骤仍在执行，暂时不能修改计划")
    if run.steps.exists():
        run.plan_version += 1
        # Previous plans and output refs remain in immutable checkpoints.
        run.steps.all().delete()
    fields = {field.name for field in RunStep._meta.fields} - {"id", "run", "status", "attempt", "error", "started_at", "completed_at",
               "lease_token", "lease_until", "lease_plan_version", "lease_cancellation_epoch"}
    RunStep.objects.bulk_create([RunStep(run=run, max_attempts=item.get("max_attempts", 3),
                                      **{key: value for key, value in item.items() if key in fields and key != "max_attempts"}) for item in steps])
    run.execution_engine = "dag"
    run.current_step_id = ""
    run.save(update_fields=["execution_engine", "plan_version", "current_step_id"])
    _set_state(run, "planning", "")
    append_locked(run, "plan.updated", {"plan_version": run.plan_version}, context=context,
                  command_key=command_key, command_digest=fingerprint)
    return run


@retry_sqlite_write
@transaction.atomic
def claim_step(run_id, worker_id, *, lease_seconds=300):
    run = lock_run(run_id)
    if run.execution_engine != "dag" or run.status not in ACTIVE_STATES:
        return None
    now = timezone.now()
    steps = list(run.steps.order_by("id"))
    # A single run has one active step. Independent runs can execute concurrently;
    # this prevents parallel steps from overwriting each other's context patches.
    for step in steps:
        if step.status == "running" and step.lease_until and step.lease_until > now:
            return None
    finished = {step.step_id for step in steps if step.status == "completed" or (step.status == "skipped" and step.optional)}
    for step in steps:
        if step.status not in {"queued", "running"} or not set(step.depends_on).issubset(finished):
            continue
        if step.status == "running" and not step.retry_policy.get("recoverable", False):
            step.status = "waiting_user"
            step.error = "上次执行中断，结果未知，请确认重试"
            step.save(update_fields=["status", "error"])
            _set_state(run, "waiting_user", "")
            append_locked(run, "user.required", {"reason": step.error, "step_id": step.step_id}, command_key=uuid.uuid4().hex)
            return None
        if step.attempt >= step.max_attempts:
            step.status = "failed"
            step.error = "步骤已达到尝试次数上限"
            step.save(update_fields=["status", "error"])
            _set_state(run, "failed", step.error)
            append_locked(run, "run.failed", {"reason": step.error, "step_id": step.step_id}, command_key=uuid.uuid4().hex)
            return None
        if step.approval_policy.get("required") and not step.approval_policy.get("approved"):
            run.current_step_id = step.step_id
            run.save(update_fields=["current_step_id"])
            _set_state(run, "waiting_user", "")
            append_locked(run, "user.required", {"reason": "此步骤需要确认", "step_id": step.step_id}, command_key=uuid.uuid4().hex)
            return None
        step.status = "running"
        step.attempt += 1
        step.error = ""
        step.started_at = step.started_at or now
        step.completed_at = None
        step.lease_token = uuid.uuid4().hex
        step.lease_until = now + timedelta(seconds=max(10, min(lease_seconds, 1800)))
        step.lease_plan_version = run.plan_version
        step.lease_cancellation_epoch = run.cancellation_epoch
        step.save()
        run.current_step_id = step.step_id
        run.save(update_fields=["current_step_id"])
        if run.status == "queued":
            _set_state(run, "planning", "")
        _set_state(run, "running", "")
        append_locked(run, "step.started", {"step_id": step.step_id, "attempt": step.attempt, "worker_id": worker_id},
                      command_key=f"step:{run.plan_version}:{step.step_id}:{step.attempt}:started")
        call_key = digest({"plan_version": run.plan_version, "step_id": step.step_id, "input_refs": step.input_refs})
        call, _ = RunToolCall.objects.update_or_create(run=run, call_key=call_key, defaults={
            "name": step.kind, "step_id": step.step_id, "arguments": {}, "inputs": {"refs": step.input_refs},
            "status": "running", "attempt": step.attempt, "claim": step.lease_token,
            "lease_until": step.lease_until, "error": {}, "completed_at": None,
        })
        append_locked(run, "tool.called", {"tool_call_id": call.id, "name": call.name, "attempt": call.attempt},
                      command_key=f"dag-tool:{call.id}:{call.attempt}:started")
        return step
    return None


def _owned_step(run, claim):
    step = run.steps.filter(pk=claim.id).first()
    if (not step or step.status != "running" or step.lease_token != claim.lease_token
            or step.lease_plan_version != run.plan_version
            or step.lease_cancellation_epoch != run.cancellation_epoch
            or not step.lease_until or step.lease_until <= timezone.now()
            or run.status not in {"running", "waiting_user"}):
        raise StepConflict("lease_lost", "执行租约已过期或任务已取消，旧结果不能写入")
    return step


@retry_sqlite_write
@transaction.atomic
def renew_step(claim, *, lease_seconds=300):
    run = lock_run(claim.run_id)
    step = _owned_step(run, claim)
    step.lease_until = timezone.now() + timedelta(seconds=max(10, min(lease_seconds, 1800)))
    step.save(update_fields=["lease_until"])
    run.tool_calls.filter(claim=claim.lease_token, status="running").update(lease_until=step.lease_until)
    return step


@retry_sqlite_write
@transaction.atomic
def finish_step(claim, output, *, context_patch=None, evidence=None, artifacts=None):
    run = lock_run(claim.run_id)
    fingerprint = digest({"output": output, "patch": context_patch, "evidence": evidence, "artifacts": artifacts})
    command_key = f"step:{claim.lease_plan_version}:{claim.step_id}:{claim.attempt}:finished"
    if repeated_command(run, command_key, fingerprint):
        return run.steps.get(pk=claim.id)
    step = _owned_step(run, claim)
    try:
        Draft202012Validator(step.completion_schema).validate(output)
    except ValidationError as exc:
        raise StepConflict("completion_condition_failed", "步骤输出未满足声明的完成条件") from exc
    artifact_id = f"step:{run.plan_version}:{step.step_id}"
    for item in evidence or []:
        RunEvidence.objects.create(run=run, **item)
    for item in artifacts or []:
        RunArtifact.objects.create(run=run, **item)
    RunArtifact.objects.create(run=run, artifact_id=artifact_id, kind="step_result", title=step.label,
                               uri=f"urn:satellitesense:{run.id}:{artifact_id}", mime_type="application/json", metadata=output)
    step.status = "completed"
    step.output_refs = [artifact_id]
    step.completed_at = timezone.now()
    step.lease_until = None
    step.save(update_fields=["status", "output_refs", "completed_at", "lease_until"])
    latest = run.checkpoints.order_by("-sequence").first()
    context = dict(latest.context_snapshot if latest else {})
    context.update(context_patch or {})
    context["outputs"] = {**context.get("outputs", {}), step.step_id: artifact_id}
    run.tool_calls.filter(claim=claim.lease_token, status="running").update(status="completed", completed_at=timezone.now(),
        result={"status": "ok", "result": output, "evidence_refs": [item["evidence_id"] for item in evidence or []],
                "artifacts": [item["artifact_id"] for item in artifacts or []], "retryable": False, "user_action": None, "diagnostics": {}},
        context_patch=context_patch or {})
    append_locked(run, "step.completed", {"step_id": step.step_id, "attempt": step.attempt}, context=context,
                  refs=[artifact_id], command_key=command_key, command_digest=fingerprint)
    append_locked(run, "tool.completed", {"name": step.kind, "status": "ok", "step_id": step.step_id},
                  command_key=command_key + ":tool", refs=[artifact_id])
    return step


@retry_sqlite_write
@transaction.atomic
def skip_step(claim, reason):
    run = lock_run(claim.run_id)
    step = _owned_step(run, claim)
    if not step.optional or step.step_id in MANDATORY_STEPS:
        raise StepConflict("mandatory_step", "必需步骤不能跳过")
    step.status = "skipped"
    step.lease_until = None
    step.completed_at = timezone.now()
    step.save(update_fields=["status", "lease_until", "completed_at"])
    run.tool_calls.filter(claim=claim.lease_token, status="running").update(status="skipped", completed_at=timezone.now())
    append_locked(run, "step.skipped", {"step_id": step.step_id, "reason": reason}, command_key=uuid.uuid4().hex)
    return step


@retry_sqlite_write
@transaction.atomic
def fail_step(claim, message, *, status="failed", retryable=False):
    if status not in {"failed", "waiting_user", "blocked", "not_supported", "external_service_unavailable"}:
        raise ValueError("invalid failure status")
    run = lock_run(claim.run_id)
    step = _owned_step(run, claim)
    step.status = status
    step.error = str(message)[:2000]
    step.lease_until = None
    step.completed_at = timezone.now()
    step.save(update_fields=["status", "error", "lease_until", "completed_at"])
    run.tool_calls.filter(claim=claim.lease_token, status="running").update(status=status, completed_at=timezone.now(),
        error={"message": step.error, "retryable": retryable},
        result={"status": "waiting" if status == "waiting_user" else "failed", "result": {}, "evidence_refs": [], "artifacts": [],
                "retryable": retryable, "user_action": "retry_step", "diagnostics": {"message": step.error}})
    _set_state(run, status, step.error)
    append_locked(run, "user.required" if status == "waiting_user" else "step.failed",
                  {"step_id": step.step_id, "reason": step.error, "retryable": retryable}, command_key=uuid.uuid4().hex)
    return step


@retry_sqlite_write
@transaction.atomic
def control_run(run_id, action, *, command_key):
    run = lock_run(run_id)
    fingerprint = digest({"action": action})
    if repeated_command(run, command_key, fingerprint):
        return run
    if action == "cancel":
        if run.status in {"completed", "cancelled"}:
            raise StepConflict("terminal_run", "运行已结束")
        _set_state(run, "cancelling", "")
        run.cancellation_epoch += 1
        run.save(update_fields=["cancellation_epoch"])
        # Fence active computations immediately. They may unwind externally but
        # cannot publish results or start any further tool calls.
        run.steps.exclude(status__in=["completed", "skipped"]).update(status="cancelled", lease_until=None)
        run.tool_calls.filter(status="running").update(status="cancelled", completed_at=timezone.now())
        append_locked(run, "run.cancelling", {"cancellation_epoch": run.cancellation_epoch}, command_key=command_key + ":fence")
        _set_state(run, "cancelled", "")
    elif action == "pause":
        if run.status not in ACTIVE_STATES:
            raise StepConflict("run_not_active", "当前运行不能暂停")
        _set_state(run, "waiting_user", "")
    elif action in {"retry_step", "resume", "approve"}:
        if run.status not in {"waiting_user", "failed", "blocked", "external_service_unavailable"}:
            raise StepConflict("run_not_stopped", "请等待当前步骤停止")
        if run.steps.filter(status="running", lease_until__gt=timezone.now()).exists():
            raise StepConflict("active_lease", "当前步骤仍在执行")
        step = run.steps.filter(step_id=run.current_step_id).first()
        if step and step.status not in {"completed", "skipped"}:
            if step.attempt >= step.max_attempts:
                raise StepConflict("attempts_exhausted", "已达到尝试次数上限，请修改条件并重新规划")
            if action == "approve":
                step.approval_policy = {**step.approval_policy, "approved": True}
            step.status = "queued"
            step.error = ""
            step.lease_until = None
            step.save(update_fields=["status", "error", "lease_until", "approval_policy"])
        _set_state(run, "retrying", "")
    else:
        raise StepConflict("unknown_action", "未知运行操作")
    latest = run.checkpoints.order_by("-sequence").first()
    context = dict(latest.context_snapshot if latest else {})
    context["paused_by_user"] = action == "pause"
    append_locked(run, "run." + run.status, {"action": action, "status": run.status}, context=context,
                  command_key=command_key, command_digest=fingerprint)
    return run
