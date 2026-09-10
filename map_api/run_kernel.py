"""集中式 AgentRun 状态转移与 checkpoint 服务。"""
import hashlib
import json
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from .models import AgentRun, RunCheckpoint, RunEvidence, RunArtifact
from .run_journal import append_locked, digest, lock_run, repeated_command, retry_sqlite_write

ALLOWED = {
    "queued": {"planning", "waiting_user", "failed", "cancelling"},
    "planning": {"running", "waiting_user", "failed", "blocked", "not_supported", "external_service_unavailable", "cancelling"},
    "running": {"running", "waiting_user", "retrying", "completed", "failed", "blocked", "not_supported", "external_service_unavailable", "cancelling"},
    "waiting_user": {"planning", "running", "retrying", "cancelling", "failed", "blocked"},
    "retrying": {"planning", "running", "waiting_user", "failed", "blocked", "not_supported", "external_service_unavailable", "cancelling"},
    "cancelling": {"cancelled", "failed"},
    "failed": {"retrying", "planning", "cancelling"},
    "blocked": {"retrying", "planning", "cancelling"},
    "not_supported": {"planning", "cancelling"},
    "external_service_unavailable": {"retrying", "planning", "cancelling"},
}
TERMINAL = {"cancelled", "completed", "failed", "blocked", "not_supported", "external_service_unavailable"}

STEP_IDS = {
    "understand": "understand_goal", "locate": "resolve_aoi",
    "select_source": "match_source_capabilities", "retrieve_imagery": "search_scenes",
    "quality_check": "quality_gate", "ndwi": "compute_metric",
    "vl_analysis": "visual_review", "review": "final_review",
    "complete": "publish_artifacts",
}


def evidence_key(item):
    """Identity survives dictionary ordering changes, rolling windows and restarts."""
    explicit = item.get("evidence_id")
    if explicit:
        if not isinstance(explicit, str) or len(explicit) > 160:
            raise ValueError("evidence_id must be a string of at most 160 characters")
        return explicit
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "evidence:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@transaction.atomic
def acknowledge_cancel(session_id, worker_claim):
    """Called only after the executing loop exits; stale workers cannot release a lease."""
    from .models import AgentSession
    session = AgentSession.objects.select_for_update().filter(pk=session_id).first()
    if not session or not session.cancel_requested:
        return
    artifacts = dict(session.artifacts or {})
    if artifacts.get("worker_claim") != worker_claim:
        return
    artifacts.pop("worker_claim", None)
    artifacts.pop("worker_claimed_at", None)
    session.artifacts = artifacts
    session.save(update_fields=["artifacts", "updated_at"])
    sync_session(session.id)


@transaction.atomic
def sync_session(session_id, *, snapshot=None, error="", expected_claim=None):
    """兼容旧执行器，把 AgentSession 的事实状态投影到 AgentRun。"""
    from .models import AgentSession
    session = AgentSession.objects.select_for_update().get(pk=session_id)
    if expected_claim and (session.artifacts or {}).get("worker_claim") != expected_claim:
        return None
    run_id = (session.artifacts or {}).get("run_id")
    if not run_id:
        return None
    existing = AgentRun.objects.select_for_update().get(pk=run_id)
    if existing.execution_engine == "dag":
        # New runs are authoritative. Legacy views/workers cannot overwrite them.
        return existing
    target = session.status
    if existing.status == "queued" and target == "running" and not session.cancel_requested:
        transition(run_id, "planning")
    if session.cancel_requested:
        if existing.status in TERMINAL:
            return existing
        # A cancellation request is not an acknowledgement from an active worker.
        target = "cancelling"
        if existing.status != "cancelling":
            transition(run_id, "cancelling", snapshot={"status": "cancelling", "session_id": session.id})
        if session.status == "failed" and not (session.artifacts or {}).get("worker_claim"):
            target = "cancelled"
    run = lock_run(run_id)
    previous_status = run.status
    _set_state(run, target, error or session.error)
    observer = (session.artifacts or {}).get("observer") or {}
    raw_current = observer.get("current_step") or ""
    current = STEP_IDS.get(raw_current, raw_current)
    if raw_current in {"failed", "waiting_user"}:
        current = run.current_step_id
    if current and run.steps.filter(step_id=current).exists():
        run.current_step_id = current
        run.save(update_fields=["current_step_id", "updated_at"])
        for step in run.steps.all():
            if step.step_id == current:
                reported = observer.get("current_status", "running")
                step.status = {"done": "completed", "pending": "queued"}.get(reported, reported)
                if target in {"cancelling", "cancelled", "failed", "waiting_user", "blocked"}:
                    step.status = target
                if step.status == "running" and step.started_at is None:
                    step.started_at = timezone.now()
                if step.status in {"completed", "failed", "cancelled"} and step.completed_at is None:
                    step.completed_at = timezone.now()
                step.error = error or ""
                step.save(update_fields=["status", "started_at", "completed_at", "error"])
    evidence = (snapshot or {}).get("evidence") or []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        evidence_id = evidence_key(item)
        RunEvidence.objects.update_or_create(run=run, evidence_id=evidence_id, defaults={
            "kind": str(item.get("kind") or item.get("tool") or "tool_result")[:60],
            "scene_id": str(item.get("scene_id") or "")[:160],
            "asset_id": str(item.get("asset_id") or "")[:160],
            "metric": str(item.get("metric") or "")[:120],
            "value": item.get("value", item.get("summary")),
            "method": str(item.get("method") or item.get("tool") or "")[:240],
            "aoi": item.get("aoi"), "mask_statistics": item.get("mask_statistics") or {},
            "data_contract": item.get("data_contract") or {}, "confidence": item.get("confidence"),
            "limitations": item.get("limitations") or [],
        })
    artifacts = session.artifacts or {}
    for key in ("file_name", "image_url", "final_answer", "report_url", "ndwi", "spectral_indices", "scene"):
        value = artifacts.get(key)
        if value in (None, ""):
            continue
        artifact_id = f"{key}:{run.id}"
        uri = f"urn:satellitesense:{key}:{run.id}"
        if key in {"image_url", "report_url"}:
            uri = value
        elif key == "file_name":
            uri = "/api/satellite/show-img/?file=" + quote(value, safe="")
        RunArtifact.objects.update_or_create(run=run, artifact_id=artifact_id, defaults={
            "kind": "report" if "report" in key else ("imagery" if key in {"file_name", "image_url", "scene"} else "result"),
            "title": key, "uri": str(uri)[:500], "metadata": value if isinstance(value, dict) else {"value": value},
            "evidence_refs": artifacts.get("final_evidence_refs", []) if key == "final_answer" else [],
        })
    context = {
        "session_id": session.id, "slots": session.slots,
        "legacy_plan": session.plan, "scene_id": session.scene_id,
        "tool_history": artifacts.get("tool_history") or [],
        "working_memory": artifacts.get("working_memory") or {},
        "waiting": artifacts.get("waiting"),
        "checkpoint": snapshot or {},
    }
    append_locked(run, "run." + target if previous_status != target else "run.checkpointed", {
        "previous_status": previous_status, "status": target,
        "current_step_id": run.current_step_id,
    }, context=context)
    return run


class InvalidRunTransition(ValueError):
    def __init__(self, current, target):
        self.details = {"code": "invalid_run_transition", "current": current, "target": target}
        super().__init__(f"illegal transition: {current} -> {target}")


def _set_state(run, target, error):
    if target != run.status and target not in ALLOWED.get(run.status, set()):
        raise InvalidRunTransition(run.status, target)
    if target == "completed" and run.execution_engine == "dag":
        from .run_acceptance import assess_run
        if not assess_run(run, evidence=list(run.evidence_v2.all()), artifacts=list(run.artifacts_v2.all()))["passed"]:
            raise InvalidRunTransition(run.status, target)
    run.status = target
    if error:
        run.error = str(error)[:4000]
    elif target in {"planning", "running", "retrying"}:
        run.error = ""
    if run.started_at is None and target in {"planning", "running"}:
        run.started_at = timezone.now()
    if target in TERMINAL and run.completed_at is None:
        run.completed_at = timezone.now()
    elif target not in TERMINAL:
        run.completed_at = None
    run.save(update_fields=["status", "error", "started_at", "completed_at", "updated_at"])


@retry_sqlite_write
@transaction.atomic
def transition(run_id, target, *, error="", snapshot=None, command_key=None):
    run = lock_run(run_id)
    fingerprint = digest({"target": target, "error": error, "snapshot": snapshot})
    if repeated_command(run, command_key, fingerprint):
        return run
    previous_status = run.status
    _set_state(run, target, error)
    append_locked(run, "run." + target, {"previous_status": previous_status, "status": target},
                  context=snapshot, command_key=command_key, command_digest=fingerprint)
    return run
