"""模型驱动的 Agent 工具循环（替换旧 run_agent_session 状态机）。

核心范式（对齐 Codex/Claude Code/OpenCode）：模型在工具注册表里自主选工具 ->
观察结果 -> 必要时重规划 -> 直到给出最终复核结论。护栏约束成本与 runaway。

- agent_step：唯一 GLM 决策拦截点，测试经 patch("map_api.agent.loop.agent_step",
  side_effect=[...]) 脚本化，无需 patch 底层 call_deepseek。
- run_agent_loop：替换 run_agent_session。
- resume_waiting_agent_session：替换 orchestrator 同名；结构化 action code 路由 +
  select_for_update 幂等 + 指令性 tool result 注入历史。

observer.public_thought 取模型生成的 thought（真实公开推理），固定 step 词汇表
保住测试断言与前端稳定。HITL：工具抛 WaitingForUser，循环把中断 tool_call 与合成
waiting result 一并持久化，resume 时注入指令 result 闭合配对，模型据此收敛。
"""
import json
import logging
import os
import time
import uuid

import requests
from django.db import close_old_connections, transaction
from django.utils import timezone

from .. import views as _views
from ..models import AgentSession, ChatHistory, ImageryScene
from ..payloads import scene_payload
from ..utils.agent_tools import build_agent_plan, merge_agent_slots
from .tools import REGISTRY, DEFINITIONS, TOOL_SPECS, build_ctx, refresh_ctx_scene, vision_image_key
from .events import emit as emit_execution_event
from .waiting import WaitingForUser, translate_action, apply_resume_action, resume_step_for, option

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 12
MAX_TOOL_CALLS = 8
MAX_VL_TOOL_CALLS = 2
MAX_AGENT_STEPS = 6
MAX_EMPTY_DECISIONS = 2

RECOVERY_FIELDS = (
    "bbox", "scene_id", "file_name", "ndwi", "vision_answer", "analysis_method",
    "facts", "hypotheses", "evidence", "vision_calls", "final_review_calls",
    "vision_call_keys", "final_review_done", "completed_steps", "spectral_indices",
)


def _model_error_retryable(exc):
    """只把瞬时网络/限流/服务端错误标为可重试。"""
    if isinstance(exc, (TimeoutError, ConnectionError, requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    return bool(response is not None and (response.status_code == 429 or response.status_code >= 500))

STEP_VOCAB = [
    "understand", "locate", "select_source", "retrieve_imagery",
    "quality_check", "ndwi", "compute_metric", "vl_analysis", "review", "complete", "failed",
]
STEP_ORDER = {s: i for i, s in enumerate(STEP_VOCAB)}

SYSTEM_PROMPT = """你是 SatelliteSense 遥感调查 Agent。基于用户目标，自主调用工具完成调查，最后输出复核结论。

## 可用工具
{tools_spec}

## 输出格式（严格 JSON，不要任何额外文字、不要 markdown 代码块）
{{
  "thought": "<一句话公开思路，给用户看，说明你这一步要做什么，不暴露内部推理细节>",
  "current_step": "<understand|locate|select_source|retrieve_imagery|quality_check|ndwi|compute_metric|vl_analysis|review|complete>",
  "plan": [{{"id": "<上述 step 之一>", "label": "<中文标签>"}}],
  "tool_call": {{"name": "<工具名>", "args": {{...}}}} | null,
  "final_answer": "<最终复核结论>" | null,
  "visual_observation": "<仅当本轮附图时填写的可见事实摘要>" | null,
  "decision_confidence": "<high|medium|low>" | null,
  "evidence_basis": ["<本轮可审计依据>"],
  "evidence_refs": ["<最终回答所引用的已保存 evidence_id；工具调用时可为空>"],
  "limitations": ["<最终回答的明确数据限制；工具调用时可为空>"],
  "next_action": "<下一步动作摘要>" | null
}}

## 规则
- tool_call 非空时 final_answer 必须为 null；每次只调一个工具。
- 收集到足够信息后 tool_call=null，final_answer 给出复核结论。
- 最终结论必须含"复核结论"三字，区分可见事实/模型推断/数据限制，不要夸大 bbox 筛查与 NDWI 的精度。
- 最终回答必须从当前上下文列出的 evidence_id 中引用当前场景的指标与视觉复核证据，并提供非空 limitations；不能编造引用。
- water/vegetation/agriculture/land_use 任务优先 Sentinel-2；small_target/built_up 优先 Mapbox 高清底图（fetch_mapbox_imagery 可用 basemap_source 切换天地图/Esri：中国区行政区调查优先 tianditu，全球细节可用 esri）；洪水/淹没/多云/夜间等全天候需求优先 Sentinel-1 SAR；地形/坡度/高程需求用 Copernicus DEM。
- 不要重复调用已成功的工具；复用工具返回的结果。
- 工具失败时先判断失败类型：瞬时网络问题可有限重试，权限/参数问题要换方案或请求用户；不要盲目重复同一调用。
- 计划不是固定流水线。可根据证据删减、插入或重排步骤，plan 中只保留当前真正需要的步骤。
- 典型流程：geocode_place -> search_sentinel_imagery / fetch_mapbox_imagery -> (water 任务) compute_ndwi -> analyze_imagery -> final_answer。search_sentinel_imagery 支持 collection 参数：默认 sentinel-2-l2a；洪水/全天候用 sentinel-1-grd，地形用 cop-dem-glo-30，L2A 无覆盖可试 sentinel-2-l1c。
- vegetation/agriculture 任务必须先调用 compute_spectral_index(index="ndvi")，current_step="compute_metric"，不能用视觉解译代替指标计算。
- 若工具返回 status=waiting，说明已暂停征求用户确认，本轮不要重复调用该工具。"""


def agent_step(messages, tools_spec, session_ctx):
    """唯一 GLM 决策点；关键节点可附带当前影像。

    生产：调 GLM JSON 模式，解析为 step dict。
    测试：经 patch("map_api.agent.loop.agent_step", side_effect=[...]) 脚本化。
    """
    from ..utils.agent_tools import call_glm_json, image_file_to_data_url
    system = SYSTEM_PROMPT.format(tools_spec=json.dumps(tools_spec, ensure_ascii=False))
    request_messages = [{"role": "system", "content": system}] + list(messages)
    image_urls = []
    trigger = str((session_ctx or {}).get("vision_trigger") or "").strip()
    allowed = trigger in {"quality_check", "source_selection", "replan", "final_review"}
    image_path = (session_ctx or {}).get("image_path")
    if allowed and image_path:
        image_urls.append(image_file_to_data_url(image_path))
    raw = call_glm_json(request_messages, image_urls=image_urls or None)
    from .decision import validate_legacy_decision
    validate_legacy_decision(raw, DEFINITIONS)
    tc = raw.get("tool_call")
    if not isinstance(tc, dict):
        tc = None
    fa = raw.get("final_answer")
    plan = raw.get("plan")
    if not isinstance(plan, list):
        plan = None
    thought = str(raw.get("thought", "")).strip()[:500]
    if not thought and image_urls:
        thought = "已查看当前影像，正在结合影像质量和任务目标做公开判断。"
    result = {
        "thought": thought,
        "current_step": raw.get("current_step") or "understand",
        "plan": plan,
        "tool_call": tc,
        "final_answer": fa if isinstance(fa, str) and fa.strip() else None,
        "evidence_refs": raw.get("evidence_refs") or [],
        "limitations": raw.get("limitations") or [],
    }
    result["vision_used"] = bool(image_urls)
    result["vision_trigger"] = trigger if image_urls else None
    result["visual_observation"] = str(raw.get("visual_observation") or raw.get("observation") or "")[:800] or None
    result["decision_confidence"] = raw.get("decision_confidence")
    basis = raw.get("evidence_basis") or raw.get("why") or []
    if isinstance(basis, str):
        basis = [basis]
    result["evidence_basis"] = [str(item)[:240] for item in basis if str(item).strip()][:8]
    result["next_action"] = str(raw.get("next_action") or raw.get("next") or "").strip()[:400] or None
    # 模型没有返回独立观察时保持为空；不能把“收到图片”伪装成视觉事实。
    # 事件层仍会保留 vision_used/image_ref，用户能知道调用发生过，
    # 但只有模型明确输出的观察才进入 visual_observation。
    if not result["thought"] and not result["tool_call"] and not result["final_answer"]:
        # HTTP 200 但没有可执行决策属于无效模型输出，不能伪装成正常思考。
        raise ValueError("GLM 返回空决策")
    return result


# ---------------- observer ----------------

def _step_label(sid):
    if sid == "compute_metric":
        return "计算光谱指数"
    for s in _views.AGENT_OBSERVER_DEFAULT_STEPS:
        if s.get("id") == sid:
            return s.get("label", sid)
    return sid


def _loop_set_observer(session, current_step, thought, plan, status="running",
                       message=None, completed_steps=None, expected_claim=None):
    completed_steps = completed_steps or set()
    raw = plan if isinstance(plan, list) and plan else None
    norm = []
    for s in (raw or _views.AGENT_OBSERVER_DEFAULT_STEPS):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or s.get("step_id")
        if not sid:
            continue
        norm.append({"id": sid, "label": s.get("label") or _step_label(sid)})
    # 模型有时会复述系统提示中的典型完整流程；这不是动态计划。
    # 与默认阶段完全一致时按已发生步骤投影，避免把未来 pending 阶段伪装成实时进度。
    default_ids = [item["id"] for item in _views.AGENT_OBSERVER_DEFAULT_STEPS]
    if [item["id"] for item in norm] == default_ids:
        norm = []
    if not norm:
        # 没有真实计划时只展示已发生的步骤和当前步骤，不生成未来 pending 阶段。
        ids = list(completed_steps or set())
        if current_step and current_step not in ids:
            ids.append(current_step)
        ids.sort(key=lambda sid: STEP_ORDER.get(sid, 999))
        norm = [{"id": sid, "label": _step_label(sid)} for sid in ids if sid]
    plan_steps = []
    for s in norm:
        sid = s["id"]
        if sid == current_step and status in ("running", "waiting_user", "failed"):
            sstatus = status
        elif sid in completed_steps:
            sstatus = "done"
        else:
            sstatus = "pending"
        plan_steps.append({"id": sid, "label": s["label"], "status": sstatus})
    current_label = _step_label(current_step)
    next_label = ""
    found = False
    for s in plan_steps:
        if found and s["status"] == "pending":
            next_label = s["label"]
            break
        if s["id"] == current_step:
            found = True
    if status == "failed":
        next_label = "等待处理失败原因"
    elif status == "waiting_user":
        next_label = "等待用户确认"
    observer = {
        "current_step": current_step,
        "current_label": current_label,
        "current_status": status,
        "public_thought": thought or _views.AGENT_STAGE_PUBLIC_THOUGHTS.get(
            current_step, "我正在按计划推进当前调查步骤。"),
        "doing": message or thought or current_label,
        "next": next_label,
        "decision": {},
        "plan_steps": plan_steps,
        "updated_at": timezone.now().isoformat(),
    }
    # Agent 可能与取消接口并发写入；行锁覆盖“读-合并-写”整个窗口，避免
    # refresh 后到 save 前被取消接口插入而再次用旧快照覆盖 cancel_requested。
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        artifacts = dict(locked.artifacts or {})
        if expected_claim and artifacts.get("worker_claim") != expected_claim:
            return False
        artifacts["observer"] = observer
        locked.artifacts = artifacts
        locked.save(update_fields=["artifacts", "updated_at"])
        session = locked
    return True


# ---------------- 持久化 ----------------

def _ctx_scene(ctx):
    sid = ctx.get("scene_id")
    return ImageryScene.objects.filter(id=sid).first() if sid else None


def _touch_worker_lease(session, expected_claim=None):
    """刷新当前 worker 租约；终态/取消任务绝不续租。"""
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if locked.status in (AgentSession.STATUS_COMPLETED, AgentSession.STATUS_FAILED) or locked.cancel_requested:
            return False
        artifacts = dict(locked.artifacts or {})
        if expected_claim and artifacts.get("worker_claim") != expected_claim:
            return False
        if not artifacts.get("worker_claim"):
            return True
        artifacts["worker_claimed_at"] = timezone.now().isoformat()
        locked.artifacts = artifacts
        locked.save(update_fields=["artifacts", "updated_at"])
        session = locked
    return True


def _persist(session, ctx, tool_history, expected_claim=None):
    # 上下文只保留最近工具交换，旧内容通过 durable event 可追溯，避免长任务越跑越慢。
    if len(tool_history) > 80:
        tool_history[:] = tool_history[-80:]
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
            return False
        artifacts = dict(locked.artifacts or {})
        artifacts["tool_history"] = tool_history
        artifacts["vision_calls"] = int(ctx.get("vision_calls") or 0)
        artifacts["final_review_calls"] = int(ctx.get("final_review_calls") or 0)
        artifacts["vision_call_keys"] = list(ctx.get("vision_call_keys") or [])[:20]
        artifacts["final_review_done"] = bool(ctx.get("final_review_done"))
        artifacts["working_memory"] = {key: ctx[key] for key in RECOVERY_FIELDS if key in ctx}
        if ctx.get("spectral_indices"):
            artifacts["spectral_indices"] = ctx["spectral_indices"]
        scene = _ctx_scene(ctx)
        if ctx.get("file_name"):
            artifacts["file_name"] = ctx["file_name"]
            artifacts["image_url"] = f"/api/satellite/show-img/?file={ctx['file_name']}"
        if scene:
            artifacts["scene"] = scene_payload(scene)
            locked.scene_id = scene.id
        if ctx.get("bbox"):
            artifacts["bbox"] = ctx["bbox"]
        locked.artifacts = artifacts
        locked.slots = ctx["slots"]
        locked.save(update_fields=["artifacts", "slots", "scene", "updated_at"])
        from ..run_kernel import sync_session
        sync_session(locked.id, snapshot={"evidence": ctx.get("evidence") or []}, expected_claim=expected_claim)
        session = locked
    event_slots = dict(ctx.get("slots") or {})
    resolved = dict(event_slots.get("resolved_place") or {})
    if "polygon" in resolved:
        polygon = resolved.pop("polygon") or []
        resolved["polygon_points"] = sum(
            len(ring) for ring in polygon if isinstance(ring, list)
        )
    if resolved:
        event_slots["resolved_place"] = resolved
    emit_execution_event(session.id, "checkpoint", {
        "slots": event_slots,
        "bbox": ctx.get("bbox"),
        "scene_id": ctx.get("scene_id"),
        "tool_count": len(tool_history),
        "facts": ctx.get("facts") or {},
        "hypotheses": ctx.get("hypotheses") or [],
        "evidence": ctx.get("evidence") or [],
    }, expected_claim=expected_claim)
    return True


def _build_messages(ctx, tool_history):
    slots = ctx["slots"]
    visual = ctx.get("visual_context") or {}
    visual_text = json.dumps(visual, ensure_ascii=False, default=str) if visual else "无可用视觉证据"
    preamble = (
        f"调查目标：{ctx['goal']}\n\n"
        f"已解析槽位：{json.dumps(slots, ensure_ascii=False, default=str)}\n"
        f"当前影像 file_name：{ctx.get('file_name') or '无'}\n"
        f"当前 scene_id：{ctx.get('scene_id') or '无'}\n"
        f"图像源：{slots.get('source', '未定')}\n"
        f"已保存证据：{json.dumps([{'evidence_id': e.get('evidence_id'), 'tool': e.get('tool'), 'scene_id': e.get('scene_id'), 'metric': e.get('metric')} for e in ctx.get('evidence', [])], ensure_ascii=False)}\n"
        f"视觉证据摘要（仅在本轮附图时可直接观察）：{visual_text}"
    )
    messages = [{"role": "user", "content": preamble}]
    for item in tool_history:
        role = item.get("role")
        if role == "assistant":
            text = item.get("content", "")
            tc = item.get("tool_call") or {}
            if tc.get("name"):
                text = f"{text}\n[调用工具 {tc['name']} 参数={json.dumps(tc.get('args', {}), ensure_ascii=False, default=str)}]"
            messages.append({"role": "assistant", "content": text})
        elif role == "tool_result":
            messages.append({"role": "user", "content": f"工具返回：{item.get('content', '')}"})
    return messages


def _deterministic_next_call(ctx):
    """根据本地工作记忆返回下一步最小工具调用。

    这是模型决策失败时的安全兜底，不依赖 LLM 是否正确复述参数。
    """
    slots = ctx.get("slots") or {}
    if not ctx.get("bbox") and slots.get("place_name"):
        return {"name": "geocode_place", "args": {"place_name": slots["place_name"]}, "step": "locate"}
    if not ctx.get("scene_id"):
        source = slots.get("source") or "sentinel2"
        if source in ("mapbox", "tianditu", "esri"):
            args = {} if source == "mapbox" else {"basemap_source": source}
            return {"name": "fetch_mapbox_imagery", "args": args, "step": "retrieve_imagery"}
        if source == "sentinel1":
            return {"name": "search_sentinel_imagery", "args": {"collection": "sentinel-1-grd"}, "step": "retrieve_imagery"}
        if source == "copdem":
            return {"name": "search_sentinel_imagery", "args": {"collection": "cop-dem-glo-30"}, "step": "retrieve_imagery"}
        return {"name": "search_sentinel_imagery", "args": {}, "step": "retrieve_imagery"}
    if slots.get("task") == "water" and slots.get("source") == "sentinel2" and ctx.get("ndwi") is None:
        return {"name": "compute_ndwi", "args": {}, "step": "ndwi"}
    if not ctx.get("vision_answer"):
        return {"name": "analyze_imagery", "args": {"question": ctx.get("goal")}, "step": "vl_analysis"}
    return None


def _fallback_final_answer(ctx, safe=False):
    """模型复核不可用时生成最小可交付结论。"""
    scene = _ctx_scene(ctx)
    ndwi = ctx.get("ndwi") or {}
    vision = (ctx.get("vision_answer") or "").strip()
    source = (scene.source_label if scene else None) or (ctx.get("slots") or {}).get("source", "影像")
    lines = ["复核结论：已完成本次遥感筛查。", f"影像来源：{source}。"]
    if vision and not safe:
        lines.append(f"视觉解译：{vision}")
    elif safe:
        lines.append("视觉解译：当前影像证据不足，已停止输出模型的具体地名、尺寸和成因推断。")
    if ndwi.get("available"):
        lines.append(f"NDWI 筛查：可能水体比例约 {ndwi.get('water_percent', '—')}%，仅作区域线索。")
    elif (ctx.get("slots") or {}).get("task") == "water":
        lines.append("NDWI 未能提供稳定量化结果，本次仅保留视觉解译。")
    lines.append("数据限制：结果受影像时相、云量、空间分辨率和行政区 bbox 影响，不应替代精确测绘或执法取证。")
    return "\n".join(lines)


def _quality_warning(ctx):
    scene = _ctx_scene(ctx)
    if not scene:
        return ""
    metadata = scene.metadata or {}
    coverage = metadata.get("target_coverage_ratio")
    valid = metadata.get("valid_image_ratio")
    cloud = scene.cloud_percent
    warnings = []
    if coverage is not None and float(coverage) < 0.85:
        warnings.append(f"有效覆盖约 {float(coverage):.1%}")
    if valid is not None and float(valid) < 0.80:
        warnings.append(f"有效像素约 {float(valid):.1%}")
    if cloud is not None and float(cloud) > 30:
        warnings.append(f"云量约 {float(cloud):.1f}%")
    if scene.gsd_m and float(scene.gsd_m) > 50:
        warnings.append(f"输出分辨率约 {float(scene.gsd_m):.0f}m/像素")
    if not warnings:
        return ""
    return "数据质量警告：" + "、".join(warnings) + "。以下内容只能作为区域筛查线索，不支持精确目标尺寸、数量或水质参数判断。\n"


def _quality_requires_safe_fallback(ctx):
    """证据能力严重不足时，不展示 VL 的具体地名/地物推断。"""
    scene = _ctx_scene(ctx)
    if not scene:
        return False
    metadata = scene.metadata or {}
    try:
        if scene.gsd_m and float(scene.gsd_m) > 150:
            return True
        if metadata.get("valid_image_ratio") is not None and float(metadata["valid_image_ratio"]) < 0.60:
            return True
        if metadata.get("target_coverage_ratio") is not None and float(metadata["target_coverage_ratio"]) < 0.60:
            return True
    except (TypeError, ValueError):
        return True
    return False


def _complete(session, ctx, final_answer, tool_history, expected_claim=None, *, evidence_refs=None, limitations=None):
    session.refresh_from_db(fields=["status", "cancel_requested", "artifacts"])
    if session.status == AgentSession.STATUS_FAILED or session.cancel_requested or (session.artifacts or {}).get("cancel_requested"):
        logger.info("skip Agent completion after cancellation: session=%s", session.id)
        return
    if (session.artifacts or {}).get("pause_requested"):
        _views._agent_wait(session, "当前步骤已停止，可以修改条件并重新规划。", [option("retry_step"), option("cancel")],
                           {"paused_by_user": True}, step_id="review", label="已暂停", expected_claim=expected_claim)
        return
    file_name = ctx.get("file_name")
    scene = _ctx_scene(ctx)
    bbox = ctx.get("bbox")
    if not file_name:
        # 优先保留最近一次工具的领域根因，避免把地理编码失败误报成影像缺失。
        last_error = ""
        for item in reversed(tool_history or []):
            if item.get("role") != "tool_result":
                continue
            try:
                payload = json.loads(item.get("content") or "{}")
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict) and payload.get("status") == "error":
                last_error = str(payload.get("message") or "").strip()
                if last_error:
                    break
        if last_error and ("行政区" in last_error or "地理" in last_error or "地点" in last_error):
            message = f"行政区无法解析：{last_error}。请改用真实行政区名称，或直接提供 bbox 后重试。"
        else:
            message = "Agent 未获取到可关联的影像，无法保存调查结果。"
        _views._agent_fail(session, message)
        return
    warning = _quality_warning(ctx)
    if _quality_requires_safe_fallback(ctx):
        _views._agent_wait(session, "影像有效覆盖或分辨率未满足任务要求，不能生成正式结论。",
                           [option("expand_dates"), option("cancel")], {"scene_id": ctx.get("scene_id")},
                           step_id="quality_check", label="质量门禁未通过", expected_claim=expected_claim)
        return
    if warning and not final_answer.startswith("数据质量警告："):
        final_answer = warning + final_answer
    refs = evidence_refs if isinstance(evidence_refs, list) else []
    available = {item["evidence_id"]: item for item in ctx.get("evidence", [])
                 if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
    valid_refs = bool(refs) and all(isinstance(ref, str) and ref in available for ref in refs)
    if not valid_refs:
        refs = []
    current_evidence = [available[ref] for ref in refs if ref in available and available[ref].get("scene_id") == ctx.get("scene_id")]
    task = (ctx.get("slots") or {}).get("task")
    required_metric = "ndwi" if task == "water" else ("ndvi" if task in {"vegetation", "agriculture"} else None)
    visual_ok = any(item.get("tool") == "analyze_imagery" for item in current_evidence) and bool(ctx.get("vision_answer"))
    metric_ok = not required_metric or any(item.get("metric") == required_metric and (item.get("summary") or {}).get("available") is True for item in current_evidence)
    limits_ok = isinstance(limitations, list) and bool(limitations) and all(isinstance(item, str) and item.strip() for item in limitations)
    if not (valid_refs and visual_ok and metric_ok and limits_ok):
        _views._agent_wait(session, "最终回答缺少当前场景的必需指标、视觉证据引用或限制说明，不能标记为完成。",
                           [option("retry_step"), option("cancel")], {"failed_phase": "final_review", "missing_metric": required_metric if not metric_ok else None},
                           step_id="review", label="最终证据核验未通过", expected_claim=expected_claim)
        return
    final_answer += "\n\n证据引用：" + "、".join(refs) + "\n数据限制：" + "；".join(limitations)
    follow_up = ctx.get("follow_up")
    if follow_up:
        # 多轮追问：追加到现有对话，更新 ChatHistory 全量消息
        new_exchange = [
            {"role": "user", "content": follow_up},
            {"role": "ai", "content": final_answer, "analysis_method": ctx.get("analysis_method")},
        ]
        with transaction.atomic():
            locked = AgentSession.objects.select_for_update().get(id=session.id)
            if locked.status == AgentSession.STATUS_FAILED or locked.cancel_requested or (locked.artifacts or {}).get("cancel_requested"):
                logger.info("skip Agent follow-up completion after cancellation: session=%s", session.id)
                return
            if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
                logger.info("skip stale Agent follow-up completion after lease takeover: session=%s", session.id)
                return
            existing = list(locked.messages or [])
            existing = [m for m in existing if not (m.get("role") == "assistant" and "调查已完成" in (m.get("content") or ""))]
            all_messages = existing + new_exchange
            history, _ = ChatHistory.objects.update_or_create(
                image_file=file_name,
                defaults={
                    "scene": scene,
                    "messages": all_messages,
                    "spatial_context": f"Agent 调查：{ctx['slots'].get('place_name', '当前区域')}",
                    "bbox": bbox,
                },
            )
            artifacts = {
                **(locked.artifacts or {}),
                "final_answer": final_answer,
                "final_evidence_refs": refs,
                "final_limitations": limitations,
                "analysis_method": ctx.get("analysis_method"),
                "tool_history": tool_history,
                "history_id": history.id,
                "report_available": True,
            }
            artifacts.pop("worker_claim", None)
            artifacts.pop("worker_claimed_at", None)
            locked.status = AgentSession.STATUS_COMPLETED
            locked.messages = all_messages + [{
                "role": "assistant",
                "content": "追问已回答。需要正式 Word 报告时，可继续发送“生成报告”。",
                "options": ["生成报告"],
            }]
            locked.artifacts = artifacts
            locked.save(update_fields=["status", "messages", "artifacts", "updated_at"])
            session = locked
        emit_execution_event(session.id, "task_completed", {
            "phase": "complete", "status": "done", "summary": "追问已完成并保存到调查记录",
            "why": ["最终回答已与当前影像和执行证据关联"],
        }, expected_claim=None)
        _views._agent_step(session, "complete", "整理结果", "done", "追问完成", {"history_id": history.id})
        return
    messages = [
        {"role": "user", "content": session.goal},
        {"role": "ai", "content": final_answer, "analysis_method": ctx.get("analysis_method")},
    ]
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if locked.status == AgentSession.STATUS_FAILED or locked.cancel_requested or (locked.artifacts or {}).get("cancel_requested"):
            logger.info("skip Agent completion after cancellation: session=%s", session.id)
            return
        if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
            logger.info("skip stale Agent completion after lease takeover: session=%s", session.id)
            return
        history, _ = ChatHistory.objects.update_or_create(
            image_file=file_name,
            defaults={
                "scene": scene,
                "messages": messages,
                "spatial_context": f"Agent 调查：{ctx['slots'].get('place_name', '当前区域')}",
                "bbox": bbox,
            },
        )
        artifacts = {
        **(locked.artifacts or {}),
        "file_name": file_name,
        "image_url": f"/api/satellite/show-img/?file={file_name}" if file_name else None,
        "scene": scene_payload(scene) if scene else None,
        "bbox": bbox,
        "ndwi": ctx.get("ndwi"),
        "vision_answer": ctx.get("vision_answer"),
        "final_answer": final_answer,
        "final_evidence_refs": refs,
        "final_limitations": limitations,
        "analysis_method": ctx.get("analysis_method"),
        "history_id": history.id,
        "report_available": True,
        "tool_history": tool_history,
    }
        artifacts.pop("worker_claim", None)
        artifacts.pop("worker_claimed_at", None)
        locked.status = AgentSession.STATUS_COMPLETED
        locked.history = history
        locked.messages = messages + [{
        "role": "assistant",
        "content": "调查已完成。需要正式 Word 报告时，可以继续发送“生成报告”。",
        "options": ["生成报告"],
    }]
        locked.artifacts = artifacts
        locked.save(update_fields=["status", "history", "messages", "artifacts", "updated_at"])
        session = locked
    emit_execution_event(session.id, "task_completed", {
        "phase": "complete", "status": "done", "summary": "调查完成，已保存最终结论和证据索引",
        "why": ["最终结论已通过质量门控并写入历史记录"],
    }, expected_claim=None)
    _views._agent_step(session, "complete", "整理结果", "done", "调查完成", {"history_id": history.id})


# ---------------- 主循环 ----------------

def _run_loop(session, ctx, tool_history, context):
    expected_claim = context.get("worker_claim") if isinstance(context, dict) else None
    iterations = 0
    tool_calls = 0
    vl_calls = 0
    agent_step_calls = 0
    empty_decisions = 0
    last_tool_signature = None
    repeated_tool_count = 0
    last_tool_error = ""
    completed_steps = set(ctx.get("completed_steps") or ["understand"])
    if ctx.get("scene_id"):
        completed_steps.update({"locate", "select_source"})

    while True:
        session.refresh_from_db(fields=["status", "cancel_requested", "artifacts"])
        if session.status == AgentSession.STATUS_FAILED or session.cancel_requested or (session.artifacts or {}).get("cancel_requested"):
            logger.info("Agent session cancelled: session=%s", session.id)
            return
        if not _touch_worker_lease(session, expected_claim):
            return
        if (session.artifacts or {}).get("pause_requested"):
            _views._agent_wait(session, "当前步骤已停止，可以修改条件并重新规划。", [option("retry_step"), option("cancel")],
                               {"paused_by_user": True}, step_id="review", label="已暂停", expected_claim=expected_claim)
            return
        if iterations >= MAX_ITERATIONS or agent_step_calls >= MAX_AGENT_STEPS:
            _views._agent_fail(session, "Agent 超过最大迭代次数或决策调用上限，已停止。")
            return
        iterations += 1
        agent_step_calls += 1

        refresh_ctx_scene(ctx)

        messages = _build_messages(ctx, tool_history)
        rule_only = (ctx.get("slots") or {}).get("decision_mode") == "rule"
        vision_trigger = ""
        max_vision = int(os.environ.get("AGENT_MAX_VISION_CALLS", "3"))
        max_final_review = int(os.environ.get("AGENT_MAX_FINAL_REVIEW_VISION_CALLS", "1"))
        image_digest = vision_image_key(ctx) if ctx.get("image_path") else None
        seen_keys = set(ctx.get("vision_call_keys") or [])
        vision_enabled = str(os.environ.get("AGENT_VISION_ASSIST", "1")).strip().lower() not in {"0", "false", "off", "no"}
        trigger_policy = str(os.environ.get("AGENT_VISION_TRIGGER", "key_checkpoints")).strip().lower()
        if vision_enabled and trigger_policy != "disabled" and ctx.get("image_path") and int(ctx.get("vision_calls", 0)) < max_vision:
            if ctx.get("slots", {}).get("source_switched"):
                candidate_trigger = "source_selection"
            elif (ctx.get("vision_answer") and not ctx.get("final_review_done")
                  and int(ctx.get("final_review_calls", 0)) < max_final_review):
                candidate_trigger = "final_review"
            else:
                candidate_trigger = "quality_check"
            image_key = f"{image_digest}|{candidate_trigger}"
            if image_key not in seen_keys:
                vision_trigger = candidate_trigger
            else:
                image_key = None
        ctx["vision_trigger"] = vision_trigger
        decision_kind = "model_decision"
        try:
            if rule_only:
                raise RuntimeError("已按用户选择进入规则流程")
            step = agent_step(messages, TOOL_SPECS, ctx)
            # 只有模型调用成功后才记账；失败时保留该图片键，允许恢复动作重试。
            if vision_trigger and image_key and image_key not in set(ctx.get("vision_call_keys") or []):
                ctx["vision_calls"] = int(ctx.get("vision_calls", 0)) + 1
                ctx.setdefault("vision_call_keys", []).append(image_key)
                if vision_trigger == "final_review":
                    ctx["final_review_calls"] = int(ctx.get("final_review_calls", 0)) + 1
                    ctx["final_review_done"] = True
                _persist(session, ctx, tool_history, expected_claim)
        except Exception as exc:
            logger.warning("agent decision failed: %s", exc)
            emit_execution_event(session.id, "model_unavailable", {
                "phase": "review", "status": "waiting_user",
                "summary": "主模型决策失败，任务已暂停",
                "error_type": type(exc).__name__,
                "retryable": _model_error_retryable(exc),
            }, expected_claim=expected_claim)
            _views._agent_wait(
                session, "主模型决策失败，当前任务已暂停。请重试或取消。",
                [option("retry_step"), option("cancel")],
                {"error_type": type(exc).__name__, "retryable": _model_error_retryable(exc)},
                step_id="review", label="等待模型服务恢复", expected_claim=expected_claim,
            )
            return
        thought = step["thought"]
        current_step = step["current_step"]
        plan = step["plan"]
        tool_call = step["tool_call"]
        final_answer = step["final_answer"]

        event_phase = (step.get("vision_trigger") or current_step) if step.get("vision_used") else current_step
        emit_execution_event(session.id, decision_kind, {
            "phase": event_phase,
            "step": current_step,
            "summary": thought,
            "action": tool_call,
            "why": (step.get("evidence_basis") or
                    (["已查看关联影像", "结合影像元数据与已完成工具结果进行公开复核"]
                     if step.get("vision_used") else
                     ["基于当前任务目标、已完成工具结果和质量门控"])),
            "tool_call": tool_call,
            "has_final_answer": bool(final_answer),
            "provider": "bigmodel",
            "model": os.environ.get("AGENT_MODEL", "glm-5.3-flash"),
            "vision_used": bool(step.get("vision_used")),
            "vision_trigger": step.get("vision_trigger"),
            "image_ref": ({
                "scene_id": ctx.get("scene_id"),
                "file_name": ctx.get("file_name"),
                "source": (ctx.get("slots") or {}).get("source"),
                "gsd_m": (ctx.get("visual_context") or {}).get("gsd_m"),
                "preview_scale_m": (ctx.get("visual_context") or {}).get("preview_scale_m"),
                "coverage_ratio": (ctx.get("visual_context") or {}).get("coverage_ratio"),
                "valid_pixel_ratio": (ctx.get("visual_context") or {}).get("valid_pixel_ratio"),
                "polygon_clipped": (ctx.get("visual_context") or {}).get("polygon_clipped"),
            } if step.get("vision_used") else None),
            "visual_observation": step.get("visual_observation") or "未提供独立视觉观察；本次仅将影像作为辅助证据输入。" if step.get("vision_used") else None,
            "next": step.get("next_action") or ("继续执行下一步工具" if tool_call else "整理最终复核结论"),
            "decision_confidence": step.get("decision_confidence"),
            "primary_interpreter": "qwen" if ctx.get("vision_answer") else None,
            "decision_reviewer": "glm" if step.get("vision_used") else None,
            "vision_reviewer": "glm" if step.get("vision_used") else None,
            "evidence_refs": [f"scene:{ctx.get('scene_id')}"] if step.get("vision_used") and ctx.get("scene_id") else [],
        }, expected_claim=expected_claim)

        if final_answer:
            _loop_set_observer(session, current_step or "review", thought, plan,
                               status="running", message=thought, completed_steps=completed_steps,
                               expected_claim=expected_claim)
            _complete(session, ctx, final_answer, tool_history, expected_claim,
                      evidence_refs=step.get("evidence_refs"), limitations=step.get("limitations"))
            return

        if not _loop_set_observer(session, current_step, thought, plan,
                                  status="running", message=thought, completed_steps=completed_steps,
                                  expected_claim=expected_claim):
            return

        if not tool_call:
            empty_decisions += 1
            if empty_decisions > MAX_EMPTY_DECISIONS:
                _views._agent_fail(session, "Agent 连续未给出有效工具调用或最终结论，已停止空转。")
                return
            tool_history.append({
                "role": "tool_result",
                "content": "你上一轮没有调用工具也没有给出最终结论。请调用下一个工具，或基于已有信息给出 final_answer 复核结论。",
            })
            _persist(session, ctx, tool_history, expected_claim)
            continue

        name = tool_call.get("name")
        args = tool_call.get("args") or {}
        empty_decisions = 0
        signature = json.dumps({"name": name, "args": args}, ensure_ascii=False, sort_keys=True, default=str)
        if signature == last_tool_signature:
            repeated_tool_count += 1
        else:
            repeated_tool_count = 0
            last_tool_signature = signature
        if repeated_tool_count >= 2:
            suffix = f"最近一次工具错误：{last_tool_error}" if last_tool_error else ""
            _views._agent_fail(session, f"Agent 重复调用工具 {name}，未产生新进展，已停止。{suffix}")
            return
        tool = REGISTRY.get(name)
        definition = DEFINITIONS.get(name)
        tool_history.append({
            "role": "assistant", "content": thought,
            "tool_call": {"name": name, "args": args},
        })
        emit_execution_event(session.id, "tool_started", {"phase": current_step, "name": name, "args": args, "summary": f"开始调用 {name}"}, expected_claim=expected_claim)

        if not tool or not definition:
            tool_history.append({
                "role": "tool_result",
                "content": json.dumps({"status": "error", "message": f"未知工具：{name}。可用：{list(REGISTRY)}"}, ensure_ascii=False),
            })
            _persist(session, ctx, tool_history, expected_claim)
            continue
        if name == "analyze_imagery" and vl_calls >= MAX_VL_TOOL_CALLS:
            tool_history.append({
                "role": "tool_result",
                "content": json.dumps({"status": "error", "message": "已达视觉解译调用上限（2 次），请直接给出 final_answer。"}, ensure_ascii=False),
            })
            _persist(session, ctx, tool_history, expected_claim)
            continue
        if tool_calls >= MAX_TOOL_CALLS:
            tool_history.append({
                "role": "tool_result",
                "content": json.dumps({"status": "error", "message": "已达工具调用上限，请直接给出 final_answer。"}, ensure_ascii=False),
            })
            _persist(session, ctx, tool_history, expected_claim)
            continue

        if name == "analyze_imagery":
            vl_calls += 1
        tool_calls += 1

        started_at = time.perf_counter()
        result = None
        try:
            from .durable_tools import invoke, ToolClaimLost
            result = invoke(session, definition, ctx, args, current_step, expected_claim)
            result_text = json.dumps(result, ensure_ascii=False, default=str)
            if isinstance(result, dict) and result.get("status") != "ok":
                last_tool_error = str(result.get("message") or "工具返回错误")[:300]
        except ToolClaimLost:
            return
        except WaitingForUser as w:
            tool_history.append({
                "role": "tool_result",
                "content": json.dumps({"status": "waiting", "message": w.message, "options": w.options, "data": w.data}, ensure_ascii=False, default=str),
            })
            if not _persist(session, ctx, tool_history, expected_claim):
                return
            wait_data = dict(w.data or {})
            wait_data.setdefault("failed_tool", name)
            wait_data.setdefault("failed_step", current_step)
            _views._agent_wait(session, w.message, w.options, wait_data, step_id=w.step_id, label=w.label,
                               expected_claim=expected_claim)
            return
        except Exception as exc:
            logger.exception("agent tool %s failed", name)
            last_tool_error = f"工具 {name} 执行异常：{str(exc)[:200]}"
            result = {
                "status": "error",
                "message": last_tool_error,
                "error_type": "timeout" if isinstance(exc, (TimeoutError, ConnectionError)) else "unknown",
                "retryable": isinstance(exc, (TimeoutError, ConnectionError)),
                "attempts": 1,
            }
            result_text = json.dumps(result, ensure_ascii=False)

        with transaction.atomic():
            locked = AgentSession.objects.select_for_update().get(id=session.id)
            if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
                return
            metrics = dict((locked.artifacts or {}).get("metrics") or {})
            tool_timings = list(metrics.get("tool_timings") or [])
            tool_timings.append({"tool": name, "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1)})
            metrics["tool_timings"] = tool_timings[-30:]
            locked.artifacts = {**(locked.artifacts or {}), "metrics": metrics}
            locked.save(update_fields=["artifacts", "updated_at"])
            session = locked

        tool_history.append({"role": "tool_result", "content": result_text})
        if isinstance(result, dict) and result.get("status") == "ok":
            result_body = result.get("result") if isinstance(result.get("result"), dict) else result
            ctx.setdefault("facts", {})[name] = result_body
            evidence = {"tool": name, "kind": name, "summary": result_body, "scene_id": ctx.get("scene_id")}
            if name in {"compute_ndwi", "compute_spectral_index"}:
                limits = result_body.get("limitations") or []
                evidence.update({"metric": result_body.get("index", "ndwi"), "method": result_body.get("method"),
                                 "aoi": result_body.get("aoi") or ctx.get("bbox"), "data_contract": result_body.get("data_contract") or {},
                                 "mask_statistics": {key: result_body.get(key) for key in ("sample_size_px", "valid_pixel_ratio", "aoi_pixel_count", "mask_source")},
                                 "limitations": [limits] if isinstance(limits, str) else limits})
            from ..run_kernel import evidence_key
            evidence["evidence_id"] = evidence_key(evidence)
            ctx.setdefault("evidence", []).append(evidence)
            ctx["evidence"] = ctx["evidence"][-40:]
        result_payload = result if isinstance(result, dict) else {"summary": result_text}
        nested = result_payload.get("result") if isinstance(result_payload.get("result"), dict) else {}
        result_summary = (
            result_payload.get("summary") or result_payload.get("message") or result_payload.get("reason")
            or nested.get("summary") or nested.get("message")
            or ("工具返回可用结果" if result_payload.get("status") == "ok" else "工具未返回可用结果")
        )
        emit_execution_event(session.id, "tool_result", {"phase": current_step, "name": name, "summary": result_summary, "result": result_payload}, expected_claim=expected_claim)
        if isinstance(result, dict) and result.get("status") == "error":
            emit_execution_event(session.id, "replan_required", {
                "failed_tool": name,
                "error": last_tool_error,
                "suggestion": "分析失败类型后选择重试、替代工具或请求用户",
            }, expected_claim=expected_claim)
        if isinstance(result, dict) and result.get("status") == "ok":
            completed_steps.add(current_step)
        ctx["completed_steps"] = sorted(completed_steps)
        _persist(session, ctx, tool_history, expected_claim)
        if not isinstance(result, dict) or result.get("status") != "ok":
            _views._agent_wait(session, last_tool_error or "工具没有返回完整可用结果，任务已暂停。",
                               [option("retry_step"), option("cancel")], {"failed_tool": name, "failed_step": current_step},
                               step_id=current_step, label="等待工具重试", expected_claim=expected_claim)
            return


def run_agent_loop(session_id, context=None):
    """替换旧 run_agent_session。"""
    close_old_connections()
    context = context or {}
    session = AgentSession.objects.get(id=session_id)
    # 直接调用（旧 API/测试）没有显式令牌时，在入口捕获一次当前令牌。
    # worker/后台线程会显式传入令牌，防止租约接管后旧执行者继续写入。
    if not context.get("worker_claim"):
        context["worker_claim"] = (session.artifacts or {}).get("worker_claim")
    emit_execution_event(session.id, "task_started", {
        "phase": "understand",
        "status": "running",
        "summary": "开始执行调查任务",
        "why": ["已接收用户目标，准备建立可恢复执行上下文"],
    }, expected_claim=context.get("worker_claim"))
    try:
        if session.status != AgentSession.STATUS_RUNNING or session.cancel_requested:
            return
        run_id = (session.artifacts or {}).get("run_id")
        if run_id and not context.get("tool_history") and not (session.artifacts or {}).get("retry_planning"):
            from ..models import RunCheckpoint
            from ..run_journal import load_checkpoint
            latest = RunCheckpoint.objects.filter(run_id=run_id).order_by("-sequence").first()
            if latest and latest.state_snapshot.get("schema_version") == 1:
                checkpoint = load_checkpoint(run_id)
                saved = checkpoint["context"]
                memory = saved.get("working_memory")
                if isinstance(memory, dict) and memory:
                    ctx = build_ctx(session, context)
                    ctx.update({key: value for key, value in memory.items() if key in RECOVERY_FIELDS})
                    ctx["slots"] = saved.get("slots") or session.slots
                    refresh_ctx_scene(ctx)
                    _run_loop(session, ctx, list(saved.get("tool_history") or []), context)
                    return
        # 恢复：context 直接提供 tool_history（已含指令 result）
        if context.get("tool_history") and not (session.artifacts or {}).get("retry_planning"):
            ctx = build_ctx(session, context)
            memory = (session.artifacts or {}).get("working_memory") or {}
            if isinstance(memory, dict):
                ctx.update({key: value for key, value in memory.items() if key in RECOVERY_FIELDS})
            # Current user input has priority over the checkpoint. Evidence from a
            # different scene must not become the basis of a follow-up answer.
            if context.get("scene_id") and context["scene_id"] != ctx.get("scene_id"):
                ctx = build_ctx(session, context)
            ctx["slots"] = dict(session.slots or {})
            refresh_ctx_scene(ctx)
            ctx["follow_up"] = context.get("follow_up")
            tool_history = list(context["tool_history"])
            _run_loop(session, ctx, tool_history, context)
            return

        # 全新会话：先解析槽位（保留确定性合并）
        expected_claim = context.get("worker_claim")
        if not _views._agent_step(session, "understand", "理解调查目标", "running", "正在解析地点、时间、任务类型和图像源",
                                  expected_claim=expected_claim):
            return
        existing_slots = dict(session.slots or {})
        try:
            plan = build_agent_plan(session.goal, mode=session.mode)
        except Exception as exc:
            _views._agent_wait(
                session, "主模型规划失败，任务已暂停。请重试或取消。",
                [option("retry_step"), option("cancel")],
                {"error_type": type(exc).__name__, "retryable": _model_error_retryable(exc), "failed_phase": "planning"},
                step_id="understand", label="等待规划服务恢复", expected_claim=expected_claim,
            )
            return
        slots = dict(plan.get("slots") or {})
        slots.update(existing_slots)
        if context.get("bbox"):
            slots["bbox"] = context["bbox"]
            slots["place_name"] = slots.get("place_name") or "当前框选区域"
        # 槽位/计划也属于 worker 受保护状态。旧 worker 在租约被接管后
        # 不能用未加锁的 save 覆盖新 worker 已解析出的计划。
        with transaction.atomic():
            locked = AgentSession.objects.select_for_update().get(id=session.id)
            if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
                return
            if locked.status in (AgentSession.STATUS_FAILED, AgentSession.STATUS_COMPLETED) or locked.cancel_requested:
                return
            locked.slots = slots
            locked.plan = plan
            artifacts = dict(locked.artifacts or {})
            artifacts.pop("retry_planning", None)
            locked.artifacts = artifacts
            locked.save(update_fields=["slots", "plan", "artifacts", "updated_at"])
            session = locked
        emit_execution_event(session.id, "plan_created", {
            "plan": plan,
            "summary": "已根据用户目标建立初始执行计划",
            "why": ["计划将随影像覆盖、云量和工具结果动态调整"],
        }, expected_claim=expected_claim)
        _views._agent_step(session, "understand", "理解调查目标", "done", "已完成任务槽位解析", slots)

        ctx = build_ctx(session, context)
        if ctx.get("scene_id"):
            _views._agent_step(session, "locate", "定位调查范围", "done", "使用当前已框选区域", ctx.get("bbox"))
        else:
            _views._agent_step(session, "locate", "定位调查范围", "running", "等待 geocode_place 定位")
        _run_loop(session, ctx, [], context)
    except Exception as exc:
        logger.exception("agent loop failed id=%s", session_id)
        _views._agent_fail(session, str(exc)[:500])
    finally:
        from ..run_kernel import acknowledge_cancel
        acknowledge_cancel(session_id, context.get("worker_claim"))
        close_old_connections()


def resume_waiting_agent_session(session, action):
    """替换 orchestrator 同名。结构化 action code 路由 + 幂等 + 指令注入。"""
    action_text = action or ""
    code = translate_action(action_text)
    try:
        emit_execution_event(session.id, "user_action_received", {
            "phase": "waiting_user",
            "status": "done",
            "summary": f"收到用户恢复操作：{code or action_text[:120]}",
            "action": code or "continue",
        })
    except Exception:
        logger.debug("unable to append user action event", exc_info=True)
    if code == "cancel" or "取消" in action_text:
        with transaction.atomic():
            locked = AgentSession.objects.select_for_update().get(id=session.id)
            if locked.status in (AgentSession.STATUS_COMPLETED, AgentSession.STATUS_FAILED):
                return
            latest_artifacts = dict(locked.artifacts or {})
            latest_artifacts.pop("waiting", None)
            latest_artifacts["cancel_requested"] = True
            locked.cancel_requested = True
            locked.status = AgentSession.STATUS_FAILED
            locked.error = "用户取消任务"
            locked.artifacts = latest_artifacts
            locked.messages = list(locked.messages or []) + [{"role": "assistant", "content": "Agent 调查任务已取消。"}]
            locked.save(update_fields=["status", "cancel_requested", "error", "messages", "artifacts", "updated_at"])
            session = locked
        _views._agent_set_observer(session, "failed", "任务已取消", "failed", "用户取消任务")
        return

    if not code:
        code = "continue"

    # 幂等：原子翻转 waiting_user -> running，双击第二次直接 no-op
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if locked.status != AgentSession.STATUS_WAITING_USER:
            return
        artifacts = dict(locked.artifacts or {})
        waiting_data = ((artifacts.get("waiting") or {}).get("data") or {})
        artifacts.pop("pause_requested", None)
        if code == "retry_step" and waiting_data.get("call_key"):
            from .durable_tools import authorize_retry
            if not authorize_retry(locked, waiting_data["call_key"]):
                return
        if waiting_data.get("failed_phase") == "planning":
            artifacts["retry_planning"] = True
        artifacts.pop("waiting", None)
        execution_mode = artifacts.get("execution_mode") or str(os.environ.get("AGENT_EXECUTION_MODE", "thread")).strip().lower()
        if execution_mode in {"queue", "worker", "persistent"}:
            artifacts.pop("worker_claim", None)
            artifacts.pop("worker_claimed_at", None)
            artifacts["queued_at"] = timezone.now().isoformat()
        else:
            artifacts["worker_claim"] = f"thread:{uuid.uuid4().hex[:10]}"
            artifacts["worker_claimed_at"] = timezone.now().isoformat()
        directive = apply_resume_action(locked, code)
        if code == "retry_step" and waiting_data.get("failed_tool"):
            directive = (
                f"{directive} 失败工具是 {waiting_data['failed_tool']}，本次恢复必须优先且仅重试该工具；"
                "不要重新执行已成功的工具。"
            )
        locked.status = AgentSession.STATUS_RUNNING
        locked.artifacts = artifacts
        locked.save(update_fields=["slots", "mode", "status", "artifacts", "updated_at"])
        session = locked
    session.refresh_from_db()

    tool_history = list(artifacts.get("tool_history") or [])
    tool_history.append({
        "role": "tool_result",
        "content": json.dumps({"status": "ok", "directive": directive}, ensure_ascii=False),
    })
    plan = (artifacts.get("observer") or {}).get("plan_steps")
    _loop_set_observer(session, resume_step_for(code), directive, plan,
                       status="running", message=directive)

    context = {
        "force_continue": code == "continue",
        "resume_with_scene": code in ("continue", "retry_fast"),
        "tool_history": tool_history,
        "bbox": session.slots.get("bbox"),
        "worker_claim": artifacts.get("worker_claim"),
    }
    _views._run_agent_background(session.id, context)
