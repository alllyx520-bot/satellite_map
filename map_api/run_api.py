"""Read-only v2 event transport; clients resume from committed database cursors."""
import json
import time
import uuid

from django.db import close_old_connections, transaction
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone

from .models import AgentRun, AgentSession, RunEvent
from .run_journal import event_payload
from .run_kernel import TERMINAL


def summary(run):
    return {
        "id": run.id, "goal": run.goal, "status": run.status,
        "current_step_id": run.current_step_id, "plan_version": run.plan_version,
        "context_version": run.context_version, "event_sequence": run.event_sequence,
        "provider": run.provider, "model": run.model, "error": run.error,
        "created_at": run.created_at.isoformat(), "updated_at": run.updated_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def run_list(request):
    if request.method == "POST":
        from .views import agent_session_list
        request.agent_queue_only = True
        response = agent_session_list(request)
        if response.status_code != 200:
            return response
        payload = json.loads(response.content)["data"]
        run = owned_run(request, payload["artifacts"]["run_id"])
        response = JsonResponse({"code": 202, "data": summary(run)}, status=202)
        response["Location"] = f"/api/v2/agent/runs/{run.id}/"
        return response
    if request.method != "GET":
        return JsonResponse({"code": 405, "msg": "只支持 GET 或 POST"}, status=405)
    try:
        limit = _integer(request.GET.get("limit", "20"), 1, 50)
    except (TypeError, ValueError):
        return JsonResponse({"code": 400, "msg": "limit 必须在 1 到 50 之间"}, status=400)
    owner = request.session.session_key
    ids = AgentSession.objects.filter(owner_session_key=owner).values_list("artifacts__run_id", flat=True) if owner else []
    runs = AgentRun.objects.filter(id__in=ids).order_by("-updated_at")[:limit]
    response = JsonResponse({"code": 200, "data": [summary(run) for run in runs]})
    response["Cache-Control"] = "no-store"
    return response


def run_actions(request, run_id):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "只支持 POST"}, status=405)
    run = owned_run(request, run_id)
    if not run:
        return JsonResponse({"code": 404, "msg": "找不到运行"}, status=404)
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict) or body.get("action") not in {"pause", "cancel", "retry_step", "expand_dates", "switch_source", "retry_fast", "generate_report"}:
            raise ValueError
    except (ValueError, TypeError):
        return JsonResponse({"code": 400, "msg": "无效的运行操作"}, status=400)
    from .views import agent_session_messages
    from .run_kernel import sync_session
    session = AgentSession.objects.get(owner_session_key=request.session.session_key, artifacts__run_id=run_id)
    if run.execution_engine == "dag":
        from .run_scheduler import control_run, StepConflict
        from .run_executor import project_legacy
        from .run_journal import RunCommandConflict
        action = body["action"]
        key = request.headers.get("Idempotency-Key") or body.get("message_id") or uuid.uuid4().hex
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            return JsonResponse({"code": 400, "msg": "操作标识无效"}, status=400)
        if action == "generate_report" and run.status == "completed":
            return JsonResponse({"code": 200, "data": summary(run)})
        if action not in {"pause", "cancel", "retry_step"}:
            return JsonResponse({"code": 409, "msg": "请通过重新规划明确修改条件，完成后将重新校验数据需求"}, status=409)
        if action == "pause" and run.status == "waiting_user":
            return JsonResponse({"code": 202, "data": summary(run)}, status=202)
        try:
            run = control_run(run_id, action, command_key=key)
            project_legacy(run_id)
            return JsonResponse({"code": 202 if action == "pause" else 200, "data": summary(run)}, status=202 if action == "pause" else 200)
        except (StepConflict, RunCommandConflict) as exc:
            return JsonResponse({"code": 409, "msg": str(exc), "error": exc.details}, status=409)
    if body["action"] == "pause":
        with transaction.atomic():
            from .run_journal import lock_run, append_locked
            run = lock_run(run_id)
            if run.status not in {"queued", "planning", "running", "waiting_user"}:
                return JsonResponse({"code": 409, "msg": "当前运行无法暂停"}, status=409)
            session = AgentSession.objects.select_for_update().get(pk=session.id)
            if run.status != "waiting_user" and not (session.artifacts or {}).get("pause_requested"):
                session.artifacts = {**(session.artifacts or {}), "pause_requested": True}
                if not session.artifacts.get("worker_claim"):
                    from .run_kernel import _set_state
                    session.status = "waiting_user"
                    session.artifacts["waiting"] = {"message": "已暂停，可以修改条件并重新规划。", "options": [{"code": "retry_step", "label": "继续执行"}], "data": {"paused_by_user": True}}
                    _set_state(run, "waiting_user", "")
                session.save(update_fields=["status", "artifacts", "updated_at"])
                append_locked(run, "user.pause_requested", {"message": "当前调用结束后暂停，可修改调查条件"}, command_key=f"pause:{run.plan_version}:{run.event_sequence}")
        return JsonResponse({"code": 202, "data": summary(run)}, status=202)
    response = agent_session_messages(request, session.id)
    if response.status_code not in {200, 202}:
        return response
    sync_session(session.id)
    run.refresh_from_db()
    return JsonResponse({"code": response.status_code, "data": summary(run)}, status=response.status_code)


def run_artifacts(request, run_id):
    run, _, error = _request(request, run_id)
    if error is not None:
        return error
    response = JsonResponse({"code": 200, "data": list(run.artifacts_v2.order_by("id").values(
        "artifact_id", "kind", "title", "uri", "preview_uri", "mime_type", "metadata", "evidence_refs", "created_at",
    ))})
    response["Cache-Control"] = "no-store"
    return response


def run_artifact_download(request, run_id, artifact_id):
    run, _, error = _request(request, run_id)
    if error is not None:
        return error
    artifact = run.artifacts_v2.filter(artifact_id=artifact_id).first()
    if not artifact or not artifact.metadata.get("file_name"):
        return JsonResponse({"code": 404, "msg": "产物不存在"}, status=404)
    from .run_products import read_product
    try:
        content = read_product(run_id, artifact.metadata)
    except (OSError, ValueError, KeyError):
        return JsonResponse({"code": 409, "msg": "产物丢失或校验失败，请重新生成"}, status=409)
    response = HttpResponse(content, content_type=artifact.mime_type)
    response["Content-Disposition"] = ('inline' if artifact.mime_type in {"image/png", "image/jpeg"} else 'attachment') + f'; filename="{artifact.metadata["file_name"]}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def run_evidence(request, run_id):
    run, _, error = _request(request, run_id)
    if error is not None:
        return error
    response = JsonResponse({"code": 200, "data": list(run.evidence_v2.order_by("id").values(
        "evidence_id", "kind", "scene_id", "asset_id", "metric", "value", "method", "aoi",
        "mask_statistics", "data_contract", "confidence", "limitations",
    ))})
    response["Cache-Control"] = "no-store"
    return response


def run_replan(request, run_id):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "只支持 POST"}, status=405)
    if owned_run(request, run_id) is None:
        return JsonResponse({"code": 404, "msg": "找不到运行"}, status=404)
    from .run_journal import RunCommandConflict
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict) or set(body) - {"goal", "conditions", "request_id"}:
            raise ValueError("重新规划参数无效")
        goal = body.get("goal")
        if goal is not None and (not isinstance(goal, str) or not goal.strip() or len(goal) > 4000):
            raise ValueError("目标必须为 1 到 4000 个字符")
        conditions = body.get("conditions", {})
        if not isinstance(conditions, dict) or set(conditions) - {"place_name", "date_start", "date_end", "source", "bbox"}:
            raise ValueError("条件仅支持地点、日期、数据源和 bbox")
        from datetime import date
        for field in ("date_start", "date_end"):
            if field in conditions:
                date.fromisoformat(conditions[field])
        if conditions.get("date_start") and conditions.get("date_end") and conditions["date_start"] > conditions["date_end"]:
            raise ValueError("起始日期不能晚于结束日期")
        if "source" in conditions and conditions["source"] not in {"sentinel2", "mapbox", "tianditu", "esri", "sentinel1", "copdem"}:
            raise ValueError("不支持的数据源")
        if "place_name" in conditions and (not isinstance(conditions["place_name"], str) or not 1 <= len(conditions["place_name"]) <= 200):
            raise ValueError("地点名称无效")
        if "bbox" in conditions:
            from .views import _normalize_agent_bbox
            conditions["bbox"] = _normalize_agent_bbox(conditions["bbox"])
            if not conditions["bbox"]:
                raise ValueError("bbox 无效")
        header = request.headers.get("Idempotency-Key")
        if header and body.get("request_id") and header != body["request_id"]:
            raise ValueError("操作标识不一致")
        key = header or body.get("request_id") or uuid.uuid4().hex
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            raise ValueError("操作标识必须为 1 到 120 个字符")
        run = _replan(run_id, goal.strip() if goal else None, conditions, key)
        return JsonResponse({"code": 202, "data": summary(run)}, status=202)
    except RunCommandConflict as exc:
        return JsonResponse({"code": 409, "error": exc.details}, status=409)
    except ReplanConflict as exc:
        return JsonResponse({"code": 409, "msg": str(exc)}, status=409)
    except (ValueError, TypeError) as exc:
        return JsonResponse({"code": 400, "msg": str(exc)}, status=400)


class ReplanConflict(ValueError):
    """The current execution still owns work or cannot accept a new plan."""


@transaction.atomic
def _replan(run_id, goal, conditions, key):
    from .run_journal import lock_run, digest, repeated_command, append_locked
    from .run_kernel import _set_state
    run = lock_run(run_id)
    fingerprint = digest({"goal": goal, "conditions": conditions})
    key = "replan:" + key
    if repeated_command(run, key, fingerprint):
        return run
    if run.status not in {"waiting_user", "failed", "blocked", "not_supported", "external_service_unavailable"}:
        raise ReplanConflict("请先等待当前步骤停止；已完成和已取消的运行请新建任务")
    if run.tool_calls.filter(status="running", lease_until__gt=timezone.now()).exists():
        raise ReplanConflict("工具仍持有有效租约，请等待调用结束后重新规划")
    if run.steps.filter(status="running", lease_until__gt=timezone.now()).exists():
        raise ReplanConflict("步骤仍持有有效租约，请等待步骤停止后重新规划")
    session = AgentSession.objects.select_for_update().get(artifacts__run_id=run_id)
    if goal:
        run.goal = goal
        session.goal = goal
    session.slots = dict(conditions)
    session.scene = None
    session.status = "running"
    session.error = ""
    session.cancel_requested = False
    # Previous outputs remain in immutable checkpoints and artifact rows. They
    # cannot enter the new plan's model context or be replayed under its version.
    session.artifacts = {
        "entry": "agent", "run_id": run.id, "execution_mode": "queue",
        "queued_at": timezone.now().isoformat(), "retry_planning": True,
        "context": {"bbox": conditions["bbox"]} if conditions.get("bbox") else {},
    }
    session.plan = {}
    session.save(update_fields=["goal", "slots", "scene", "status", "error", "cancel_requested", "artifacts", "plan", "updated_at"])
    run.plan_version += 1
    run.current_step_id = "understand_goal"
    run.save(update_fields=["goal", "plan_version", "current_step_id", "updated_at"])
    run.steps.update(status="queued", attempt=0, error="", input_refs=[], output_refs=[], started_at=None, completed_at=None, lease_until=None, lease_token="")
    _set_state(run, "planning", "")
    append_locked(run, "plan.updated", {"plan_version": run.plan_version, "conditions": conditions},
                  context={"session_id": session.id, "input": conditions, "slots": conditions, "working_memory": {}, "tool_history": []},
                  command_key=key, command_digest=fingerprint)
    return run


def owned_run(request, run_id):
    owner = request.session.session_key
    if not owner or not AgentSession.objects.filter(owner_session_key=owner, artifacts__run_id=run_id).exists():
        return None
    return AgentRun.objects.filter(pk=run_id).first()


def _integer(value, minimum, maximum):
    if len(str(value)) > 16:
        raise ValueError
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError
    return result


def _request(request, run_id, stream=False):
    if request.method != "GET":
        return None, None, JsonResponse({"code": 405, "msg": "只支持 GET"}, status=405)
    run = owned_run(request, run_id)
    if run is None:
        return None, None, JsonResponse({"code": 404, "msg": "run not found"}, status=404)
    try:
        raw = request.headers.get("Last-Event-ID") if stream else None
        cursor = _integer(raw if raw is not None else request.GET.get("after", "0"), 0, 2**63 - 1)
    except (TypeError, ValueError):
        return None, None, JsonResponse({"code": 400, "msg": "事件游标必须是非负整数"}, status=400)
    if cursor > run.event_sequence:
        return None, None, JsonResponse({"code": 409, "msg": "事件游标超出当前运行，请重新读取运行状态"}, status=409)
    return run, cursor, None


def run_events(request, run_id):
    run, cursor, error = _request(request, run_id)
    if error is not None:
        return error
    try:
        limit = _integer(request.GET.get("limit", "100"), 1, 200)
    except (TypeError, ValueError):
        return JsonResponse({"code": 400, "msg": "limit 必须在 1 到 200 之间"}, status=400)
    events = list(RunEvent.objects.filter(run=run, sequence__gt=cursor).order_by("sequence")[:limit])
    next_sequence = events[-1].sequence if events else cursor
    response = JsonResponse({"code": 200, "data": {
        "events": [event_payload(event) for event in events],
        "next_sequence": next_sequence,
        "has_more": RunEvent.objects.filter(run=run, sequence__gt=next_sequence).exists(),
    }})
    response["Cache-Control"] = "no-store"
    return response


def _stream(run_id, cursor):
    deadline = time.monotonic() + 25
    try:
        while True:
            close_old_connections()
            events = list(RunEvent.objects.filter(run_id=run_id, sequence__gt=cursor).order_by("sequence")[:100])
            for event in events:
                cursor = event.sequence
                yield f"id: {cursor}\nevent: agent_event\ndata: {json.dumps(event_payload(event), ensure_ascii=False)}\n\n"
            if events:
                # Drain every committed event, including batches after the terminal event.
                continue
            run = AgentRun.objects.filter(pk=run_id).only("status", "event_sequence").first()
            if run is None or (run.status in TERMINAL and run.event_sequence <= cursor):
                return
            yield ": heartbeat\n\n"
            if time.monotonic() >= deadline:
                return
            time.sleep(0.5)
    finally:
        close_old_connections()


def run_events_stream(request, run_id):
    run, cursor, error = _request(request, run_id, stream=True)
    if error is not None:
        return error
    response = StreamingHttpResponse(_stream(run.id, cursor), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"
    return response
