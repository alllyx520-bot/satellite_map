"""V3 wire format and owner-scoped HTTP helpers."""
import json
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import JsonResponse
from django.db.models import F

from ..models import Conversation, ConversationEvent


class APIError(ValueError):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def endpoint(*methods):
    def decorate(fn):
        @wraps(fn)
        def call(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    raise APIError("不支持此请求方法", "method_not_allowed", 405)
                result = fn(request, *args, **kwargs)
                response = JsonResponse(result) if isinstance(result, dict) else result
            except APIError as exc:
                response = JsonResponse({"error": {"code": exc.code, "message": str(exc)}}, status=exc.status)
            except (ObjectDoesNotExist, ValidationError):
                response = JsonResponse({"error": {"code": "not_found", "message": "内容不存在或不属于当前工作区"}}, status=404)
            response["Cache-Control"] = "private, no-store"
            return response
        return call
    return decorate


def body(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeError):
        raise APIError("请求内容必须为 JSON")
    if not isinstance(data, dict):
        raise APIError("请求内容必须为对象")
    return data


def owner(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def owned_conversation(request, pk):
    result = Conversation.objects.filter(pk=pk, owner_session_key=owner(request)).first()
    if result is None:
        raise APIError("会话不存在或不属于当前工作区", "not_found", 404)
    return result


def lock_conversation(pk):
    Conversation.objects.filter(pk=pk).update(event_sequence=F("event_sequence"))
    return Conversation.objects.get(pk=pk)


def emit_locked(conversation, kind, payload):
    """Caller owns transaction + conversation lock; all state and events commit together."""
    conversation.event_sequence += 1
    conversation.save(update_fields=["event_sequence", "updated_at"])
    return ConversationEvent.objects.create(conversation=conversation, sequence=conversation.event_sequence,
                                            type=kind, payload=payload)


def message_json(m):
    return {"id": str(m.id), "role": m.role, "content": m.content, "parts": m.parts,
            "attachment_ids": [str(x) for x in m.attachments.values_list("pk", flat=True)],
            "status": m.status, "delivery": m.delivery, "run_id": m.run_id,
            "sequence": m.sequence, "context_version": m.context_version, "created_at": m.created_at.isoformat()}


def run_json(r):
    if r is None:
        return None
    checkpoint = r.checkpoints.order_by("-sequence").first()
    context = checkpoint.context_snapshot if checkpoint else {}
    return {"id": r.id, "status": r.status, "goal": r.goal, "model": r.model,
            "current_action": r.current_step_id, "plan": context.get("plan", []),
            "public_update": context.get("public_update", ""),
            "budget": r.budget, "usage": r.usage, "error": r.error,
            "context_version": context.get("input_version", r.context_version), "checkpoint_version": r.context_version,
            "created_at": r.created_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None}


def conversation_json(c):
    return {"id": str(c.id), "title": c.title, "active_run": run_json(c.active_run),
            "event_sequence": c.event_sequence, "context_version": c.context_version,
            "workspace": c.workspace, "starred": c.starred, "archived": c.archived,
            "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat()}


def event_json(e):
    return {"sequence": e.sequence, "type": e.type, "payload": e.payload, "created_at": e.created_at.isoformat()}


def artifact_json(a):
    return {"id": a.id, "artifact_id": a.artifact_id, "run_id": a.run_id, "kind": a.kind,
            "title": a.title, "mime_type": a.mime_type, "metadata": a.metadata,
            "evidence_refs": a.evidence_refs, "download_url": f"/api/v3/artifacts/{a.id}/download"}


def evidence_json(e):
    return {"id": e.evidence_id, "run_id": e.run_id, "kind": e.kind, "metric": e.metric,
            "value": e.value, "method": e.method, "bbox": e.aoi, "data_contract": e.data_contract,
            "confidence": e.confidence, "limitations": e.limitations, "mask_statistics": e.mask_statistics}

# Compatibility names used by the independently developed asset adapter.
def api_error(code, message, status=400):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)

read_json = body
def owner_key(request, create=True):
    return owner(request) if create else request.session.session_key

def emit_event(conversation_id, kind, payload):
    from django.db import transaction
    with transaction.atomic():
        conversation = lock_conversation(conversation_id)
        return emit_locked(conversation, kind, payload)
