"""Durable, user-auditable Agent execution events."""

import json
import re
from django.db import transaction

from ..models import AgentSession, ExecutionEvent


def _compact(value, budget=7000):
    """事件只保存可审计摘要，避免 polygon/模型正文撑爆轮询。"""
    if isinstance(value, dict):
        out = {k: _compact(v, budget) for k, v in value.items()}
    elif isinstance(value, list):
        out = value if len(value) <= 40 else value[:40] + [{"_truncated": len(value) - 40}]
        out = [_compact(v, budget) for v in out]
    else:
        out = value
    try:
        import json
        if len(json.dumps(out, ensure_ascii=False, default=str)) > budget:
            return {"_truncated": True, "type": type(value).__name__}
    except Exception:
        return {"_truncated": True}
    return out


EVENT_STATUSES = {
    "task_started": "running", "plan_created": "running", "plan_changed": "running",
    "model_decision": "running", "rule_decision": "warning", "model_unavailable": "waiting_user",
    "tool_started": "running", "tool_progress": "running", "tool_result": "done",
    "evidence_added": "done", "quality_check": "done", "replan_required": "warning",
    "user_confirmation_required": "waiting_user", "user_action_received": "done",
    "checkpoint": "done", "task_completed": "done", "task_failed": "failed",
    "task_cancelled": "cancelled",
}


def _safe_text(value, limit=800):
    text = str(value or "").strip()
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+", lambda m: f"{m.group(1)}{m.group(2) or ''}[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    return text[:limit]


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if re.search(r"(?i)(api[_-]?key|authorization|token|password|secret)", str(key)):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value[:80]]
    if isinstance(value, str):
        return _safe_text(value, 1200)
    return value


def normalized_payload(kind, payload=None):
    payload = dict(payload or {}) if isinstance(payload, dict) else {"summary": payload}
    phase = payload.pop("phase", None) or payload.pop("step", None) or ""
    status = payload.pop("status", None) or EVENT_STATUSES.get(kind, "running")
    summary = payload.get("summary") or payload.get("thought") or payload.get("message") or payload.get("error") or ""
    why = payload.get("why") or []
    if isinstance(why, str):
        why = [why]
    normalized = {
        "phase": _safe_text(phase, 80),
        "status": _safe_text(status, 30),
        "summary": _safe_text(summary),
        "why": [_safe_text(item, 300) for item in why[:8]],
        **payload,
    }
    normalized.pop("thought", None)
    normalized["summary"] = _safe_text(normalized.get("summary"))
    return _redact(normalized)


def emit(session_id, kind, payload=None, expected_claim=None):
    """Append one event with a per-session monotonic sequence.

    The session row is locked only for sequence allocation; tool execution never
    runs inside this transaction.
    """
    with transaction.atomic():
        session = AgentSession.objects.select_for_update().get(id=session_id)
        if expected_claim and (session.artifacts or {}).get("worker_claim") != expected_claim:
            return None
        last = ExecutionEvent.objects.filter(session_id=session_id).order_by("-sequence").values_list("sequence", flat=True).first()
        event = ExecutionEvent.objects.create(
            session=session,
            sequence=(last + 1) if last is not None else 0,
            kind=str(kind)[:40],
            payload=_compact(normalized_payload(str(kind), payload)),
        )
    return event


def event_dict(event):
    payload = event.payload or {}
    return {
        "sequence": event.sequence,
        "kind": event.kind,
        "phase": payload.get("phase") or "",
        "status": payload.get("status") or EVENT_STATUSES.get(event.kind, "running"),
        "payload": payload,
        "created_at": event.created_at.isoformat(),
    }


def sse_data(event):
    return f"id: {event.sequence}\nevent: agent_event\ndata: {json.dumps(event_dict(event), ensure_ascii=False)}\n\n"
