"""Same-origin conversation and event transport."""
import json
import time
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.http import FileResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie

from ..models import Conversation, ConversationEvent, RunArtifact, RunEvidence
from .common import (APIError, artifact_json, evidence_json, body, conversation_json, emit_locked,
                     endpoint, event_json, lock_conversation, message_json, owner,
                     owned_conversation, run_json)
from .conversations import control, submit


@ensure_csrf_cookie
def workbench(request):
    owner(request)
    manifest = Path(settings.BASE_DIR) / "static/v3/.vite/manifest.json"
    if not manifest.exists():
        return render(request, "workbench_unbuilt.html", status=503)
    entry = json.loads(manifest.read_text(encoding="utf-8"))["index.html"]
    return render(request, "workbench_v3.html", {"v3_js": ["/static/v3/" + entry["file"]],
        "v3_css": ["/static/v3/" + item for item in entry.get("css", [])]})


@ensure_csrf_cookie
@endpoint("GET", "POST")
def conversations(request):
    key = owner(request)
    if request.method == "POST":
        title = body(request).get("title") or "新的影像问答"
        if not isinstance(title, str) or len(title) > 200:
            raise APIError("会话名称过长")
        return {"conversation": conversation_json(Conversation.objects.create(owner_session_key=key, title=title))}
    return {"items": [conversation_json(c) for c in Conversation.objects.filter(owner_session_key=key, archived=False).select_related("active_run")[:100]]}


@endpoint("GET", "PATCH", "DELETE")
def conversation(request, conversation_id):
    c = owned_conversation(request, conversation_id)
    if c is None:
        raise APIError("会话不存在", "not_found", 404)
    if request.method != "GET":
        data = body(request) if request.method == "PATCH" else {"archived": True}
        with transaction.atomic():
            c = lock_conversation(c.id)
            for field in ("title", "workspace", "starred", "archived"):
                if field not in data:
                    continue
                value = data[field]
                if field == "title" and (not isinstance(value, str) or not 1 <= len(value) <= 200):
                    raise APIError("会话名称无效")
                if field == "workspace" and (not isinstance(value, dict) or len(json.dumps(value)) > 16000):
                    raise APIError("工作区设置无效")
                if field in {"starred", "archived"} and not isinstance(value, bool):
                    raise APIError("状态必须为布尔值")
                setattr(c, field, value)
            c.save()
            emit_locked(c, "conversation.updated", {"conversation": conversation_json(c)})
        return {"conversation": conversation_json(c)}
    from .assets import attachment_payload, observation_payload
    return {"conversation": conversation_json(c),
            "messages": [message_json(m) for m in c.messages.prefetch_related("attachments")],
            "attachments": [attachment_payload(a) for a in c.attachments.all()],
            "observations": [observation_payload(o) for o in c.observations.order_by("created_at")],
            "runs": [run_json(r) for r in c.runs.order_by("-id")[:100]],
            "evidence": [evidence_json(e) for e in RunEvidence.objects.filter(run__conversation=c)],
            "artifacts": [artifact_json(a) for a in RunArtifact.objects.filter(run__conversation=c)]}


@endpoint("GET", "POST")
def messages(request, conversation_id):
    c = owned_conversation(request, conversation_id)
    if c is None:
        raise APIError("会话不存在", "not_found", 404)
    if request.method == "GET":
        return {"items": [message_json(m) for m in c.messages.all()]}
    m, run = submit(c.id, owner(request), body(request))
    return {"message": message_json(m), "run": run_json(run)}


@endpoint("GET")
def events(request, conversation_id):
    c = owned_conversation(request, conversation_id)
    try:
        cursor = max(0, int(request.headers.get("Last-Event-ID") or request.GET.get("after", 0)))
    except (ValueError, TypeError):
        raise APIError("事件游标无效")
    if request.GET.get("format") == "json":
        rows = list(c.events.filter(sequence__gt=cursor)[:200])
        return {"items": [event_json(e) for e in rows], "cursor": rows[-1].sequence if rows else cursor}
    def stream():
        after, deadline = cursor, time.monotonic() + 25
        while time.monotonic() < deadline:
            close_old_connections()
            rows = list(ConversationEvent.objects.filter(conversation_id=c.id, sequence__gt=after).order_by("sequence")[:100])
            for row in rows:
                after = row.sequence
                yield f"id: {after}\ndata: {json.dumps(event_json(row), ensure_ascii=False)}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
                time.sleep(0.5)
    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["X-Accel-Buffering"] = "no"
    return response


@endpoint("GET")
def run(request, run_id):
    from ..models import AgentRun
    r = AgentRun.objects.get(pk=run_id, conversation__owner_session_key=owner(request))
    return {"run": run_json(r), "tools": list(r.tool_calls.values("id", "name", "arguments", "status", "result", "error", "started_at", "completed_at"))}


@endpoint("POST")
def actions(request, run_id):
    return {"run": run_json(control(run_id, owner(request), body(request)))}


@endpoint("GET")
def observations(request):
    from .assets import observation_payload
    c = owned_conversation(request, request.GET.get("conversation_id"))
    return {"items": [observation_payload(o) for o in c.observations.all()]}


@endpoint("GET")
def evidence(request):
    c = owned_conversation(request, request.GET.get("conversation_id"))
    return {"items": [evidence_json(e) for e in RunEvidence.objects.filter(run__conversation=c)]}


@endpoint("GET")
def artifacts(request):
    c = owned_conversation(request, request.GET.get("conversation_id"))
    return {"items": [artifact_json(a) for a in RunArtifact.objects.filter(run__conversation=c)]}


@endpoint("GET")
def artifact_download(request, artifact_id):
    a = RunArtifact.objects.get(pk=artifact_id, run__conversation__owner_session_key=owner(request))
    root = (Path(settings.MEDIA_ROOT) / "v3").resolve()
    path = (root / a.metadata.get("relative_path", "")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise APIError("产物不可用", "artifact_unavailable", 404)
    from .sandbox import file_hash
    if a.metadata.get("sha256") and file_hash(path) != a.metadata["sha256"]:
        raise APIError("产物完整性检查失败", "integrity_failure", 409)
    response = FileResponse(path.open("rb"), as_attachment=True, filename=path.name, content_type=a.mime_type or "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    return response


@endpoint("GET")
def capabilities(request):
    from .provider import model_capabilities
    from .sandbox import sandbox_status
    from .tools import registry
    from .data_adapter import source_capabilities
    sandbox = sandbox_status()
    tools = []
    for definition in registry().values():
        item = definition.public()
        if definition.name in {"python_analysis", "import_python_output"}:
            item["available"] = bool(sandbox.get("available"))
            if not item["available"]:
                item["unavailable_reason"] = sandbox.get("detail") or "隔离分析容器不可用"
        tools.append(item)
    return {"models": model_capabilities(), "sandbox": sandbox, "execution": sandbox,
            "sources": source_capabilities(),
            "tools": tools,
            "limits": {"upload_bytes": 1073741824, "window_size": 1024, "window_overlap": 128}}


@endpoint("GET")
def places(request):
    from .places import search
    try:
        return search(request.GET.get("q", ""))
    except ValueError as exc:
        raise APIError(str(exc), "place_lookup_failed", 400) from exc
