"""Single coordinator, durable steer/FIFO input and idempotent run control."""
import uuid

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import AgentRun, ConversationMessage, SpatialAttachment
from ..run_journal import append_locked, digest, lock_run, retry_sqlite_write
from .common import APIError, emit_locked, lock_conversation, message_json, run_json

ACTIVE = {"queued", "running", "planning", "cancelling"}
DEFAULT_BUDGET = {"wall_seconds": 7200, "controller_calls": 128, "vision_calls": 128,
                  "spatial_parallel": 4, "python_parallel": 2}


def new_run_locked(c, message):
    run = AgentRun.objects.create(conversation=c, trigger_message=message, goal=message.content,
                                  context_version=message.context_version, run_key="v3:" + uuid.uuid4().hex,
                                  provider="deepseek", model="deepseek-flash", execution_engine="harness",
                                  mode="deep", budget=dict(DEFAULT_BUDGET), usage={})
    message.run = run
    message.status = "accepted"
    message.save(update_fields=["run", "status"])
    c.active_run = run
    c.save(update_fields=["active_run", "updated_at"])
    append_locked(run, "run.created", {"message_id": str(message.id)}, context={"plan": [], "facts": [],
                  "hypotheses": [], "open_questions": [], "input_ids": [str(message.id)]})
    emit_locked(c, "run.updated", {"run": run_json(run)})
    return run


def start_next_locked(c):
    pending = c.messages.filter(role="user", status="queued").order_by("sequence").first()
    if pending:
        return new_run_locked(c, pending)
    c.active_run = None
    c.save(update_fields=["active_run", "updated_at"])
    return None


@retry_sqlite_write
@transaction.atomic
def submit(c_id, owner_key, data):
    c = lock_conversation(c_id)
    if c.owner_session_key != owner_key:
        raise APIError("会话不存在", "not_found", 404)
    content, ids = data.get("content", ""), data.get("attachment_ids", [])
    refs = data.get("references", [])
    names = data.get("attachment_names", {})
    key = data.get("request_id")
    delivery = data.get("delivery", "steer")
    if not isinstance(content, str) or len(content) > 64000 or not isinstance(ids, list) or len(ids) > 32:
        raise APIError("消息过长或附件过多")
    if not isinstance(key, str) or not 1 <= len(key) <= 120 or delivery not in {"steer", "queue"}:
        raise APIError("消息标识或投递方式无效")
    if not isinstance(refs, list) or len(refs) > 64 or any(not isinstance(x, dict) for x in refs):
        raise APIError("引用格式无效")
    if not isinstance(names, dict) or set(names) - set(ids) or any(
        not isinstance(n, str) or not 1 <= len(n.strip()) <= 240 for n in names.values()
    ):
        raise APIError("附件名称无效")
    if not content.strip() and not ids and not refs:
        raise APIError("请输入问题或添加影像")
    fingerprint = digest({"content": content, "ids": ids, "references": refs, "delivery": delivery, "names": names})
    old = c.messages.filter(request_id=key).first()
    if old:
        if old.request_digest != fingerprint:
            raise APIError("同一消息标识已用于不同内容", "idempotency_conflict", 409)
        return old, old.run
    try:
        attachments = list(SpatialAttachment.objects.filter(pk__in=ids, owner_session_key=owner_key))
    except (ValueError, TypeError):
        raise APIError("附件标识无效")
    if len(attachments) != len(set(ids)) or any(a.conversation_id not in (None, c.id) for a in attachments):
        raise APIError("附件不属于当前会话", "attachment_scope", 404)
    parts = [{"type": "text", "text": content}] if content else []
    for ref in refs:
        if ref.get("type") == "observation_ref":
            if not c.observations.filter(pk=ref.get("id")).exists():
                raise APIError("空间引用不属于当前会话")
        elif ref.get("type") != "attachment_ref" or ref.get("id") not in ids:
            raise APIError("引用类型或目标无效")
    c.context_version += 1
    c.save(update_fields=["context_version"])
    seq = (c.messages.aggregate(n=Max("sequence"))["n"] or 0) + 1
    m = ConversationMessage.objects.create(conversation=c, role="user", content=content,
        parts=parts + [{"type": "image_ref", "attachment_id": str(a.id), "sha256": a.sha256,
                       "name": names.get(str(a.id), a.name)} for a in attachments] + refs,
        request_id=key, request_digest=fingerprint, sequence=seq, delivery=delivery, context_version=c.context_version)
    m.attachments.set(attachments)
    for a in attachments:
        if not a.conversation_id:
            a.conversation = c
            a.save(update_fields=["conversation", "updated_at"])
        if str(a.id) in names and not a.messages.exclude(pk=m.pk).exists():
            a.name = names[str(a.id)].strip()
            a.save(update_fields=["name", "updated_at"])
    if c.title == "新的影像问答":
        c.title = (content.strip() or attachments[0].name)[:70]
        c.save(update_fields=["title", "updated_at"])
    current = c.active_run
    if current and current.status in ACTIVE:
        if delivery == "steer":
            m.run, m.status = current, "pending"
            m.save(update_fields=["run", "status"])
        run = current
    else:
        run = new_run_locked(c, m)
    emit_locked(c, "message.created", {"message": message_json(m)})
    return m, run


@retry_sqlite_write
@transaction.atomic
def control(run_id, owner_key, data):
    # Always lock conversation before run; this ordering is shared with worker.
    preliminary = AgentRun.objects.get(pk=run_id, execution_engine="harness", conversation__owner_session_key=owner_key)
    c = lock_conversation(preliminary.conversation_id)
    run = lock_run(run_id)
    action, key = data.get("action"), data.get("request_id")
    if action not in {"stop", "resume", "retry", "extend_budget"} or not isinstance(key, str) or not 1 <= len(key) <= 120:
        raise APIError("运行操作或标识无效")
    fingerprint = digest(data)
    previous = run.events.filter(command_key="action:" + key).first()
    if previous:
        if previous.command_digest != fingerprint:
            raise APIError("操作标识冲突", "idempotency_conflict", 409)
        return run
    if action == "stop":
        if run.status in ACTIVE:
            run.status = "cancelling" if run.worker_claim else "cancelled"
            run.cancellation_epoch += 1
            if run.status == "cancelled":
                run.completed_at = timezone.now()
    else:
        if c.active_run_id and c.active_run_id != run.id and c.active_run.status in ACTIVE:
            raise APIError("当前会话已有其他任务正在执行", "coordinator_busy", 409)
        if run.status in ACTIVE or run.status == "completed":
            raise APIError("当前运行状态不允许此操作", "invalid_transition", 409)
        if action == "extend_budget":
            patch = data.get("budget") or {}
            if not patch or set(patch) - {"wall_seconds", "controller_calls", "vision_calls"}:
                raise APIError("需要明确追加时间或调用预算")
            for field, amount in patch.items():
                if type(amount) is not int or not 1 <= amount <= (7200 if field == "wall_seconds" else 128):
                    raise APIError("单次追加最多 7200 秒或 128 次调用")
                run.budget[field] = run.budget.get(field, DEFAULT_BUDGET[field]) + amount
        elif run.status == "budget_exhausted":
            raise APIError("预算已用完，请先追加预算", "budget_exhausted", 409)
        run.status, run.error, run.worker_claim, run.lease_until, run.completed_at = "queued", "", "", None, None
        c.active_run = run
        c.save(update_fields=["active_run", "updated_at"])
    run.save()
    append_locked(run, "user." + action, {"action": action}, command_key="action:" + key, command_digest=fingerprint)
    emit_locked(c, "run.updated", {"run": run_json(run)})
    if run.status == "cancelled":
        start_next_locked(c)
    return run
