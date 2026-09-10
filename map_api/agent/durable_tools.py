"""Durable execution receipts. Only committed successful calls are replayed."""
import copy
import json
import os
import uuid
from datetime import timedelta

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from ..models import AgentSession, RunToolCall
from ..run_journal import append_locked, digest, lock_run, retry_sqlite_write
from .waiting import WaitingForUser, option


class ToolClaimLost(RuntimeError):
    """The worker may no longer publish a result or start another tool."""


def _missing_output(call):
    name = (call.context_patch.get("set") or {}).get("file_name")
    if not name:
        return False
    from ..media_paths import safe_media_path
    path = safe_media_path(os.path.join(settings.MEDIA_ROOT, "satellite_imgs"), name, (".jpg", ".jpeg", ".png"))
    return not path or not os.path.isfile(path)


def _validate_result(result):
    if not isinstance(result, dict) or result.get("status") not in {"ok", "error", "failed", "partial", "waiting"}:
        raise ValueError("工具未返回合法的结构化结果")
    body = result.get("result")
    if result["status"] == "ok" and isinstance(body, dict) and body.get("available") is False:
        result = {**result, "status": "error", "message": body.get("reason") or "所需计算结果不可用"}
    json.dumps(result, allow_nan=False)
    return result


@transaction.atomic
def authorize_retry(session, call_key):
    run_id = (session.artifacts or {}).get("run_id")
    if not run_id:
        return False
    run = lock_run(run_id)
    call = RunToolCall.objects.filter(run=run, call_key=call_key).first()
    if not call:
        return False
    if call.status == "running" and call.lease_until > timezone.now():
        return False
    if call.status == "running" or (call.status == "completed" and _missing_output(call)):
        call.status = "failed"
        call.claim = uuid.uuid4().hex
        call.error = {"message": "用户确认重试中断调用或重新生成缺失产物"}
        call.save(update_fields=["status", "claim", "error"])
        append_locked(run, "tool.retry_authorized", {"tool_call_id": call.id},
                      command_key=f"tool:{call.id}:{call.attempt}:retry-authorized")
    return True


def _input_scope(name, ctx, args):
    slots = ctx.get("slots") or {}
    if name == "geocode_place":
        return {"place_name": args.get("place_name") or slots.get("place_name")}
    if name == "query_fire_detections":
        return {"bbox": args.get("bbox") or ctx.get("bbox"), "days": args.get("days"), "source": args.get("source")}
    if name == "query_osm_context":
        return {"bbox": args.get("bbox") or ctx.get("bbox")}
    if name == "query_weather_context":
        return {"bbox": args.get("bbox") or ctx.get("bbox"), "lat": args.get("lat"), "lng": args.get("lng"), "date": args.get("date")}
    if name == "query_water_baseline":
        return {"bbox": args.get("bbox") or ctx.get("bbox"), "ndwi_ratio": args.get("ndwi_ratio")}
    if name == "query_landcover_context":
        return {"bbox": args.get("bbox") or ctx.get("bbox")}
    scope = {"bbox": args.get("bbox") or ctx.get("bbox"), "force_continue": bool(ctx.get("force_continue"))}
    if name == "search_sentinel_imagery":
        scope.update({key: args.get(key) or slots.get(key) for key in ("date_start", "date_end")})
        # collection 决定实际数据源,sentinel1/copdem 也是合法输入。
        scope["collection"] = args.get("collection") or "sentinel-2-l2a"
        scope["source"] = "sentinel2"
    elif name != "fetch_mapbox_imagery":
        scope.update({
            "scene_id": args.get("scene_id") or ctx.get("scene_id"),
            "file_name": args.get("file_name") or ctx.get("file_name"),
            "aoi": (slots.get("resolved_place") or {}).get("polygon"),
            "mode": args.get("mode") or ctx.get("mode"),
            "question": args.get("question") or ctx.get("follow_up") or ctx.get("goal"),
        })
    return scope


def _diff(before, after):
    result = {"set": {}, "delete": [], "nested": {}}
    for key in before.keys() - after.keys():
        result["delete"].append(key)
    for key, value in after.items():
        if key in before and isinstance(value, dict) and isinstance(before[key], dict):
            nested = _diff(before[key], value)
            if any(nested.values()):
                result["nested"][key] = nested
        elif key not in before or before[key] != value:
            result["set"][key] = value
    return result


def _apply(ctx, patch):
    for key in patch.get("delete", []):
        ctx.pop(key, None)
    ctx.update(copy.deepcopy(patch.get("set", {})))
    for key, value in patch.get("nested", {}).items():
        child = ctx.get(key)
        if not isinstance(child, dict):
            child = {}
            ctx[key] = child
        _apply(child, value)


def _check_worker(session_id, expected_claim, starting=False):
    session = AgentSession.objects.get(pk=session_id)
    if session.cancel_requested or session.status != "running":
        raise ToolClaimLost("运行已暂停或取消，拒绝继续工具调用")
    if expected_claim and (session.artifacts or {}).get("worker_claim") != expected_claim:
        raise ToolClaimLost("worker 租约已被接管")
    if starting and (session.artifacts or {}).get("pause_requested"):
        raise WaitingForUser("当前步骤已停止，可以修改条件并重新规划。", [option("retry_step"), option("cancel")], data={"paused_by_user": True})
    return session


@retry_sqlite_write
@transaction.atomic
def _claim(run_id, session_id, name, args, scope, step_id, expected_claim):
    run = lock_run(run_id)
    _check_worker(session_id, expected_claim, starting=True)
    key = digest({"plan_version": run.plan_version, "name": name, "arguments": args, "inputs": scope})
    call = RunToolCall.objects.filter(run=run, call_key=key).first()
    if call and call.status == "completed":
        if _missing_output(call):
            raise WaitingForUser("已完成调用的影像文件缺失，请确认重新获取。", [option("retry_step"), option("cancel")], data={"call_key": key})
        return call, True
    if call and call.status == "running":
        message = "该工具仍由另一个 worker 执行" if call.lease_until > timezone.now() else "上次工具调用中断，结果未知，需确认后重试"
        raise WaitingForUser(message, [option("retry_step"), option("cancel")], data={"call_key": key})
    if call and call.attempt >= 3:
        raise WaitingForUser("该工具已达到三次尝试上限，请修改条件或取消", [option("cancel")], data={"call_key": key})
    if not call:
        call = RunToolCall(run=run, call_key=key, name=name, step_id=step_id, arguments=args, inputs=scope)
    else:
        call.attempt += 1
    call.status = "running"
    call.claim = uuid.uuid4().hex
    call.lease_until = timezone.now() + timedelta(minutes=10)
    call.completed_at = None
    call.error = {}
    call.save()
    append_locked(run, "tool.called", {"tool_call_id": call.id, "name": name, "attempt": call.attempt},
                  command_key=f"tool:{call.id}:{call.attempt}:started")
    return call, False


@retry_sqlite_write
@transaction.atomic
def _finish(call, session_id, result, patch, expected_claim, status):
    run = lock_run(call.run_id)
    _check_worker(session_id, expected_claim)
    current = RunToolCall.objects.get(pk=call.id)
    if current.claim != call.claim or current.status != "running":
        raise ToolClaimLost("工具租约已失效，拒绝写入旧结果")
    current.status = status
    current.result = result
    current.context_patch = patch if status == "completed" else {}
    current.completed_at = timezone.now()
    current.error = {} if status == "completed" else {"message": result.get("message", "工具未成功")}
    current.save(update_fields=["status", "result", "context_patch", "completed_at", "error"])
    append_locked(run, "tool.completed" if status == "completed" else "tool.failed", {
        "tool_call_id": current.id, "name": current.name, "status": status, "attempt": current.attempt,
    }, command_key=f"tool:{current.id}:{current.attempt}:finished")


def invoke(session, definition, ctx, args, step_id, expected_claim=None):
    definition.validate_args(args)
    run_id = (session.artifacts or {}).get("run_id")
    if not run_id:
        _check_worker(session.id, expected_claim, starting=True)
        return _validate_result(definition.invoke(ctx, args))
    scope = _input_scope(definition.name, ctx, args)
    call, replay = _claim(run_id, session.id, definition.name, args, scope, step_id, expected_claim)
    if replay:
        _apply(ctx, call.context_patch)
        return copy.deepcopy(call.result)
    before = copy.deepcopy(ctx)
    try:
        result = _validate_result(definition.invoke(ctx, args))
        status = "completed" if result["status"] == "ok" else "failed"
        patch = _diff(before, ctx)
        json.dumps({"result": result, "patch": patch}, allow_nan=False)
    except WaitingForUser as exc:
        _finish(call, session.id, {"status": "waiting", "message": exc.message}, {}, expected_claim, "waiting")
        raise
    except Exception as exc:
        ctx.clear()
        ctx.update(before)
        _finish(call, session.id, {"status": "error", "message": type(exc).__name__}, {}, expected_claim, "failed")
        raise
    if status != "completed":
        ctx.clear()
        ctx.update(before)
    _finish(call, session.id, result, patch, expected_claim, status)
    return result
