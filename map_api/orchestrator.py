"""RemoteSensingAgent 编排层(Phase 7 从 views.py 拆出,M-4)。

受控而非全自主的调查流水线:槽位/计划解析 → 行政区定位 → 源选择 →
影像检索(Sentinel-2 多景拼接/Mapbox 高清)→ 质量门控(时相/云量,可等待用户确认)→
NDWI 轻量量化 → VL 解译 → DeepSeek 复核 → 结果整理与历史落库。
observer 只给公开推理摘要(current_label/public_thought/doing/next/plan_steps),
私有思维链一律不外泄。

可 patch 名(resolve_district_bbox / compute_ndwi_summary / compute_ndwi_mosaic_summary /
call_deepseek / ai_query_region / generate_report / ANALYSIS_MODES)经 views 模块【运行时】查找:
既有 patch("map_api.views.X") 全部继续生效,且规避与 views 的循环导入。
"""
import json
import logging
import os
import threading
import uuid
from datetime import date

import requests

from django.db import close_old_connections
from django.utils import timezone

from . import views as _views   # 运行时查找可 patch 名;仅调用期访问属性,无循环导入
from .geo_math import compute_image_plan
from .imagery_sources.earth_search import EarthSearchProvider
from .imagery_sources.mapbox import MapboxProvider
from .media_paths import SAVE_DIR
from .models import AgentSession, ChatHistory, DownloadTask, ImageryScene
from .payloads import (
    _scene_source_key, imagery_quality_payload, scene_brief_payload,
    scene_payload, sentinel_retrieval_timeline_payload,
)
from .sentinel_pipeline import sentinel_retrieval_result
from .utils.agent_tools import AGENT_MODEL, build_agent_plan
from .utils.get_satellite_image import _download_progress, fetch_satellite_image

logger = logging.getLogger(__name__)

AGENT_MODES = {
    "precise": {"agent_model": AGENT_MODEL, "vision_model": "qwen3-vl-plus"},
    "fast": {"agent_model": AGENT_MODEL, "vision_model": "qwen3-vl-flash"},
}
AGENT_STAGE_PUBLIC_THOUGHTS = {
    "understand": "我先把用户的一句话拆成地点、时间、任务类型、图像源和模型模式，避免后面盲目调用工具。",
    "locate": "我需要把自然语言地点转成可下载影像的经纬度范围；第一版使用行政区 bbox 做市域级筛查。",
    "select_source": "我会根据任务粒度选择图像源：近期宏观态势优先 Sentinel-2，建筑道路等细节优先高清底图。",
    "retrieve_imagery": "我正在把计划落到真实影像上：检索候选、排序、渲染，并记录影像来源和候选依据。",
    "quality_check": "我会先检查时相、云量、分辨率和证据等级，防止用不合适的影像做过度结论。",
    "ndwi": "水体任务需要一个轻量定量线索，所以我会计算 NDWI，但只把它作为筛查指标。",
    "vl_analysis": "我把影像和上下文交给视觉模型解译，让它给出可见地物、空间格局和风险线索。",
    "review": "我用 DeepSeek 对视觉结论做复核，重点检查证据边界、时效性和是否夸大。",
    "complete": "我正在把影像、量化结果、模型结论和限制条件整理成可保存、可报告的结果。",
    "failed": "我已经停止当前调查，并把失败原因保留下来，方便继续排查。",
}
AGENT_OBSERVER_DEFAULT_STEPS = [
    {"id": "understand", "label": "理解调查目标"},
    {"id": "locate", "label": "定位调查范围"},
    {"id": "select_source", "label": "选择图像源"},
    {"id": "retrieve_imagery", "label": "检索并生成影像"},
    {"id": "quality_check", "label": "检查影像质量"},
    {"id": "vl_analysis", "label": "视觉模型解译"},
    {"id": "review", "label": "DeepSeek 结论复核"},
    {"id": "complete", "label": "整理结果"},
]


def agent_session_payload(session):
    return {
        "id": session.id,
        "status": session.status,
        "goal": session.goal,
        "mode": session.mode,
        "slots": session.slots,
        "plan": session.plan,
        "timeline": session.timeline,
        "observer": (session.artifacts or {}).get("observer") or {},
        "messages": session.messages,
        "artifacts": session.artifacts,
        "error": session.error,
        "scene_id": session.scene_id,
        "history_id": session.history_id,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _agent_observer_payload(session, step_id, label, status, message, data=None):
    plan_steps = []
    raw_steps = (session.plan or {}).get("steps") or AGENT_OBSERVER_DEFAULT_STEPS
    timeline = session.timeline or []
    for step in raw_steps:
        if not step:
            continue
        step_done = any(t.get("id") == step.get("id") and t.get("status") == "done" for t in timeline)
        step_running = step.get("id") == step_id and status == "running"
        step_waiting = step.get("id") == step_id and status == "waiting_user"
        step_failed = step.get("id") == step_id and status == "failed"
        plan_steps.append({
            "id": step.get("id"),
            "label": step.get("label"),
            "status": "running" if step_running else (
                "waiting_user" if step_waiting else (
                    "failed" if step_failed else (
                        "done" if step_done else "pending"
                    )
                )
            ),
        })
    if status == "failed" and not any(step.get("id") == step_id for step in plan_steps):
        plan_steps.append({"id": step_id, "label": label, "status": "failed"})
    current_index = next((i for i, step in enumerate(plan_steps) if step.get("id") == step_id), -1)
    next_step = None
    for step in plan_steps[current_index + 1 if current_index >= 0 else 0:]:
        if step.get("status") == "pending":
            next_step = step
            break
    if status == "failed":
        next_label = "等待处理失败原因"
    elif status == "waiting_user":
        next_label = "等待用户确认"
    else:
        next_label = (next_step or {}).get("label") or ""
    return {
        "current_step": step_id,
        "current_label": label,
        "current_status": status,
        "public_thought": AGENT_STAGE_PUBLIC_THOUGHTS.get(step_id, "我正在按计划推进当前调查步骤。"),
        "doing": message or label,
        "next": next_label,
        "decision": data if isinstance(data, dict) else {},
        "plan_steps": plan_steps,
        "updated_at": timezone.now().isoformat(),
    }


def _agent_set_observer(session, step_id, label, status, message="", data=None):
    artifacts = dict(session.artifacts or {})
    artifacts["observer"] = _agent_observer_payload(session, step_id, label, status, message, data)
    session.artifacts = artifacts
    session.save(update_fields=["artifacts", "updated_at"])


def _agent_step(session, step_id, label, status="done", message="", data=None):
    timeline = list(session.timeline or [])
    item = {
        "id": step_id,
        "label": label,
        "status": status,
        "message": message,
        "time": timezone.now().isoformat(),
    }
    if data is not None:
        item["data"] = data
    timeline.append(item)
    session.timeline = timeline
    artifacts = dict(session.artifacts or {})
    artifacts["observer"] = _agent_observer_payload(session, step_id, label, status, message, data)
    session.artifacts = artifacts
    session.save(update_fields=["timeline", "artifacts", "updated_at"])


def _agent_fail(session, message):
    _agent_set_observer(session, "failed", "任务失败", "failed", message)
    session.status = AgentSession.STATUS_FAILED
    session.error = message
    messages = list(session.messages or [])
    messages.append({"role": "assistant", "content": f"任务失败：{message}"})
    session.messages = messages
    session.save(update_fields=["status", "error", "messages", "updated_at"])


def _agent_wait(session, message, options=None, data=None, step_id="waiting_user", label="等待用户确认"):
    session.status = AgentSession.STATUS_WAITING_USER
    artifacts = dict(session.artifacts or {})
    artifacts["waiting"] = {"message": message, "options": options or [], "data": data or {}}
    artifacts["observer"] = _agent_observer_payload(session, step_id, label, "waiting_user", message, data)
    session.artifacts = artifacts
    messages = list(session.messages or [])
    messages.append({"role": "assistant", "content": message, "options": options or []})
    session.messages = messages
    session.save(update_fields=["status", "artifacts", "messages", "updated_at"])


def _agent_store_scene_artifacts(session, scene, bbox):
    artifacts = dict(session.artifacts or {})
    artifacts.update({
        "file_name": scene.file_name,
        "image_url": f"/api/satellite/show-img/?file={scene.file_name}",
        "scene": scene_payload(scene),
        "bbox": bbox,
    })
    session.artifacts = artifacts
    session.save(update_fields=["artifacts", "updated_at"])


def _scene_matches_requested_dates(scene, slots):
    if not scene or not scene.acquired_at:
        return True, ""
    start = slots.get("date_start")
    end = slots.get("date_end")
    if not (start and end):
        return True, ""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError):
        return True, ""
    acquired_date = scene.acquired_at.date()
    if start_date <= acquired_date <= end_date:
        return True, ""
    return False, f"候选影像拍摄于 {acquired_date.isoformat()}，不在用户要求的 {start} 至 {end} 时间范围内。"


def _candidate_from_mosaic_metadata(item):
    return type("Candidate", (), {
        "product_id": item.get("product_id") or item.get("item_id") or "",
        "item_id": item.get("item_id") or item.get("product_id") or "",
        "assets": item.get("assets") or {},
    })()


def _run_agent_background(session_id, context=None):
    threading.Thread(target=run_agent_session, args=(session_id, context or {}), daemon=True).start()


def _agent_internal_ai_query(payload):
    class Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    response = _views.ai_query_region(Request())
    data = json.loads(response.content.decode("utf-8"))
    return response.status_code, data


def _agent_internal_report(payload):
    class Request:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"

    response = _views.generate_report(Request())
    data = json.loads(response.content.decode("utf-8"))
    return response.status_code, data


def _agent_fetch_sentinel(bbox, slots):
    resolution = 1024
    plan = compute_image_plan(bbox["min_lng"], bbox["min_lat"], bbox["max_lng"], bbox["max_lat"], resolution)
    provider = EarthSearchProvider(titiler_endpoint=os.environ.get("TITILER_ENDPOINT", None) or None)
    candidates = provider.search(
        bbox,
        start_date=slots.get("date_start"),
        end_date=slots.get("date_end"),
        max_cloud=60,
        limit=int(os.environ.get("AGENT_SENTINEL_CANDIDATE_LIMIT", "15")),
        collection="sentinel-2-l2a",
    )
    if not candidates:
        return None, None, None
    retrieval = sentinel_retrieval_result(
        provider,
        candidates,
        bbox,
        plan,
        resolution,
        file_prefix="agent_sentinel",
    )
    return retrieval["scene"], retrieval["candidate"], retrieval


def _agent_fetch_mapbox(bbox):
    resolution = 1024
    min_lng = bbox["min_lng"]
    min_lat = bbox["min_lat"]
    max_lng = bbox["max_lng"]
    max_lat = bbox["max_lat"]
    plan = compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution)
    file_name = f"agent_mapbox_{uuid.uuid4().hex[:8]}.jpg"
    result_path = fetch_satellite_image(min_lng, min_lat, max_lng, max_lat, SAVE_DIR, file_name, target_resolution=resolution)
    if not result_path:
        raise ValueError("Mapbox 高清底图下载失败，请检查 MAPBOX_TOKEN、网络或配额")
    metadata = MapboxProvider().metadata_for_bbox(bbox).as_dict()
    scene = ImageryScene.objects.create(
        file_name=file_name,
        source=metadata["source"],
        source_label=metadata["source_label"],
        product_id=metadata.get("product_id", ""),
        min_lng=min_lng,
        min_lat=min_lat,
        max_lng=max_lng,
        max_lat=max_lat,
        gsd_m=round(plan["gsd_m"], 2),
        area_km2=round(plan["area_km2"], 4),
        cloud_percent=metadata.get("cloud_percent"),
        processing_level=metadata["processing_level"],
        license_type=metadata["license_type"],
        decision_grade=metadata["decision_grade"],
        limitations=metadata["limitations"],
        metadata=metadata.get("metadata") or {},
    )
    DownloadTask.objects.create(
        scene=scene,
        file_name=file_name,
        status="done",
        total=1,
        done=1,
        failed=0,
        min_lng=min_lng,
        min_lat=min_lat,
        max_lng=max_lng,
        max_lat=max_lat,
        gsd_m=round(plan["gsd_m"], 2),
        area_km2=round(plan["area_km2"], 4),
        resolution_px=resolution,
    )
    _download_progress[file_name] = {"total": 1, "done": 1, "failed": 0, "status": "done", "scene_id": scene.id}
    return scene, {"plan": plan}


def run_agent_session(session_id, context=None):
    close_old_connections()
    context = context or {}
    session = AgentSession.objects.get(id=session_id)
    try:
        existing_slots = dict(session.slots or {})
        resume_with_scene = bool(context.get("resume_with_scene"))
        _agent_step(session, "understand", "理解调查目标", "running", "正在解析地点、时间、任务类型和图像源")
        plan = build_agent_plan(session.goal, mode=session.mode)
        slots = dict(plan.get("slots") or {})
        slots.update(existing_slots)
        plan["slots"] = slots
        if context.get("bbox"):
            slots["bbox"] = context["bbox"]
            slots["place_name"] = slots.get("place_name") or "当前框选区域"
        session.slots = slots
        session.plan = plan
        session.save(update_fields=["slots", "plan", "updated_at"])
        _agent_step(session, "understand", "理解调查目标", "done", "已完成任务槽位解析", slots)

        scene = None
        candidate = None
        file_name = context.get("file_name")
        if context.get("scene_id") or (resume_with_scene and session.scene_id):
            scene_id = context.get("scene_id") or session.scene_id
            scene = ImageryScene.objects.filter(id=scene_id).first()
            if scene:
                file_name = scene.file_name
        elif context.get("file_name"):
            scene = ImageryScene.objects.filter(file_name=os.path.basename(context.get("file_name") or "")).first()
            if scene:
                file_name = scene.file_name
        if context.get("scene_id") and not scene:
            scene = ImageryScene.objects.filter(id=context["scene_id"]).first()
            if scene:
                file_name = scene.file_name

        if scene:
            bbox = {
                "min_lng": scene.min_lng,
                "min_lat": scene.min_lat,
                "max_lng": scene.max_lng,
                "max_lat": scene.max_lat,
            }
            slots["bbox"] = bbox
            slots["source"] = _scene_source_key(scene)
            session.slots = slots
            session.scene = scene
            session.save(update_fields=["slots", "scene", "updated_at"])
            _agent_step(session, "locate", "定位调查范围", "done", "使用当前已框选区域", bbox)
        else:
            _agent_step(session, "locate", "定位调查范围", "running", "正在查询行政区边界")
            if not slots.get("place_name"):
                _agent_wait(
                    session,
                    "我还缺少调查地点，请补充城市、区县或框选区域。",
                    ["补充地点", "取消任务"],
                    step_id="locate",
                    label="定位调查范围",
                )
                return
            location = _views.resolve_district_bbox(slots["place_name"])
            bbox = location["bbox"]
            slots["bbox"] = bbox
            slots["resolved_place"] = location
            session.slots = slots
            session.save(update_fields=["slots", "updated_at"])
            _agent_step(session, "locate", "定位调查范围", "done", f"已定位 {location['name']}，采用行政区 bbox 筛查", location)

        source = slots.get("source") or "sentinel2"
        _agent_step(session, "select_source", "选择图像源", "done", f"已选择 {source}", {"source": source, "mode": session.mode})

        if not scene:
            _agent_step(session, "retrieve_imagery", "检索并生成影像", "running", "正在获取影像")
            if source == "sentinel2":
                try:
                    scene, candidate, retrieval = _agent_fetch_sentinel(bbox, slots)
                except (requests.RequestException, ValueError) as exc:
                    _agent_wait(
                        session,
                        f"Sentinel-2 公开影像检索暂时不可用或无可渲染候选：{str(exc)[:160]}",
                        ["扩大时间范围", "切换高清底图", "取消任务"],
                        {"bbox": bbox, "slots": slots, "error": str(exc)[:300]},
                        step_id="retrieve_imagery",
                        label="检索并生成影像",
                    )
                    return
                if not scene:
                    _agent_wait(
                        session,
                        "未找到符合时间和云量条件的 Sentinel-2 影像。",
                        ["扩大时间范围", "切换高清底图", "取消任务"],
                        {"bbox": bbox, "slots": slots},
                        step_id="retrieve_imagery",
                        label="检索并生成影像",
                    )
                    return
            else:
                scene, retrieval = _agent_fetch_mapbox(bbox)
            file_name = scene.file_name
            session.scene = scene
            session.save(update_fields=["scene", "updated_at"])
            _agent_step(
                session,
                "retrieve_imagery",
                "检索并生成影像",
                "done",
                "影像已生成",
                {"scene": scene_brief_payload(scene), **sentinel_retrieval_timeline_payload(retrieval)},
            )
        else:
            metadata = scene.metadata or {}
            if source == "sentinel2" and metadata.get("assets"):
                candidate = type("Candidate", (), {"assets": metadata.get("assets")})()
            if source == "sentinel2" and metadata.get("mosaic_candidates"):
                candidate = _candidate_from_mosaic_metadata((metadata.get("mosaic_candidates") or [{}])[0])
            _agent_step(session, "retrieve_imagery", "检索并生成影像", "done", "复用当前区域影像", {"scene": scene_brief_payload(scene)})

        quality = imagery_quality_payload(scene)
        _agent_step(session, "quality_check", "检查影像质量", "done", quality.get("summary", "") if quality else "", quality)
        date_matched, date_issue = _scene_matches_requested_dates(scene, slots)
        if scene.source == "sentinel2" and not date_matched and not context.get("force_continue"):
            _agent_store_scene_artifacts(session, scene, bbox)
            _agent_wait(
                session,
                f"{date_issue} 继续分析会降低结论时效性，是否扩大时间范围或切换高清底图？",
                ["扩大时间范围", "切换高清底图", "继续分析", "取消任务"],
                {"scene": scene_brief_payload(scene), "date_issue": date_issue, "slots": slots},
                step_id="quality_check",
                label="检查影像质量",
            )
            return
        if scene.source == "sentinel2" and scene.cloud_percent is not None and scene.cloud_percent > 30 and not context.get("force_continue"):
            _agent_store_scene_artifacts(session, scene, bbox)
            _agent_wait(
                session,
                f"当前 Sentinel-2 候选云量为 {scene.cloud_percent:g}%，可能影响水体判读。是否继续？",
                ["继续分析", "扩大时间范围", "切换高清底图"],
                {"scene": scene_brief_payload(scene)},
                step_id="quality_check",
                label="检查影像质量",
            )
            return

        ndwi = None
        if slots.get("task") == "water" and scene.source == "sentinel2":
            _agent_step(session, "ndwi", "轻量 NDWI 水体量化", "running", "正在计算 NDWI")
            metadata = scene.metadata or {}
            if metadata.get("mosaic") and metadata.get("mosaic_candidates"):
                ndwi_candidates = [_candidate_from_mosaic_metadata(item) for item in metadata.get("mosaic_candidates") or []]
                ndwi = _views.compute_ndwi_mosaic_summary(
                    ndwi_candidates,
                    bbox,
                    os.environ.get("TITILER_ENDPOINT") or "https://titiler.xyz",
                )
            else:
                if not candidate:
                    candidate = type("Candidate", (), {"assets": metadata.get("assets") or {}})()
                ndwi = _views.compute_ndwi_summary(candidate, bbox, os.environ.get("TITILER_ENDPOINT") or "https://titiler.xyz")
            _agent_step(session, "ndwi", "轻量 NDWI 水体量化", "done", "NDWI 量化完成" if ndwi.get("available") else "NDWI 未能计算", ndwi)

        _agent_step(session, "vl_analysis", "视觉模型解译", "running", "正在调用视觉模型")
        analysis_question = session.goal
        if ndwi:
            analysis_question += (
                "\n\n## NDWI 辅助量化\n"
                + json.dumps(ndwi, ensure_ascii=False)
                + "\n请把 NDWI 作为辅助线索，不要把它表述为精确制图结果。"
            )
        ai_payload = {
            "file_name": file_name,
            "scene_id": scene.id,
            "question": analysis_question,
            "mode": session.mode,
            "active_perception": _views.ANALYSIS_MODES.get(session.mode, _views.ANALYSIS_MODES["precise"])["active_perception"],
            "history": [],
            "gsd": scene.gsd_m,
            "bbox": bbox,
        }
        status_code, ai_data = _agent_internal_ai_query(ai_payload)
        if status_code != 200 or ai_data.get("code") != 200:
            raise ValueError(ai_data.get("msg", "视觉模型解译失败"))
        vision_answer = ai_data["data"]["answer"]
        analysis_method = ai_data["data"].get("analysis_method")
        output_quality = (analysis_method or {}).get("output_quality") or {}
        if output_quality.get("fallback_used") and not context.get("force_continue"):
            artifacts = dict(session.artifacts or {})
            artifacts.update({
                "file_name": file_name,
                "image_url": f"/api/satellite/show-img/?file={file_name}",
                "scene": scene_payload(scene),
                "bbox": bbox,
                "ndwi": ndwi,
                "vision_answer": vision_answer,
                "analysis_method": analysis_method,
            })
            session.artifacts = artifacts
            session.save(update_fields=["artifacts", "updated_at"])
            _agent_wait(
                session,
                "视觉模型没有返回稳定的结构化解译结果。继续复核可能只是在总结限制条件，是否改用快速模式重试、切换高清底图或仍继续？",
                ["快速模式重试", "切换高清底图", "继续分析", "取消任务"],
                {"output_quality": output_quality, "scene": scene_brief_payload(scene)},
                step_id="vl_analysis",
                label="视觉模型解译",
            )
            return
        _agent_step(session, "vl_analysis", "视觉模型解译", "done", "视觉模型解译完成", {"analysis_method": analysis_method})

        _agent_step(session, "review", "DeepSeek 结论复核", "running", "正在复核证据边界和结论稳定性")
        review_prompt = (
            "请作为遥感调查 Agent 的结论复核器，基于以下信息输出面向政府决策辅助的最终中文结论。"
            "必须区分可见事实、模型推断、数据限制；不要夸大 bbox 筛查和 NDWI 的精度。\n\n"
            f"用户目标：{session.goal}\n"
            f"任务槽位：{json.dumps(slots, ensure_ascii=False)}\n"
            f"影像质量：{json.dumps(quality, ensure_ascii=False)}\n"
            f"NDWI：{json.dumps(ndwi, ensure_ascii=False)}\n"
            f"视觉模型结论：{vision_answer}"
        )
        reviewed_answer = _views.call_deepseek([
            {"role": "system", "content": "你是严谨的遥感智能解译复核 Agent。"},
            {"role": "user", "content": review_prompt},
        ])
        _agent_step(session, "review", "DeepSeek 结论复核", "done", "复核完成")

        messages = [
            {"role": "user", "content": session.goal},
            {"role": "ai", "content": reviewed_answer, "analysis_method": analysis_method},
        ]
        history, _ = ChatHistory.objects.update_or_create(
            image_file=file_name,
            defaults={
                "scene": scene,
                "messages": messages,
                "spatial_context": f"Agent 调查：{slots.get('place_name', '当前区域')}",
                "bbox": bbox,
            },
        )
        artifacts = {
            **(session.artifacts or {}),
            "file_name": file_name,
            "image_url": f"/api/satellite/show-img/?file={file_name}",
            "scene": scene_payload(scene),
            "bbox": bbox,
            "ndwi": ndwi,
            "vision_answer": vision_answer,
            "final_answer": reviewed_answer,
            "analysis_method": analysis_method,
            "history_id": history.id,
            "report_available": True,
        }
        session.status = AgentSession.STATUS_COMPLETED
        session.history = history
        session.messages = messages + [{
            "role": "assistant",
            "content": "调查已完成。需要正式 Word 报告时，可以继续发送“生成报告”。",
            "options": ["生成报告"],
        }]
        session.artifacts = artifacts
        session.save(update_fields=["status", "history", "messages", "artifacts", "updated_at"])
        _agent_step(session, "complete", "整理结果", "done", "调查完成", {"history_id": history.id})
    except Exception as exc:
        logger.exception("agent session failed id=%s", session_id)
        _agent_fail(session, str(exc)[:500])
    finally:
        close_old_connections()


def resume_waiting_agent_session(session, action):
    action_text = action or ""
    slots = dict(session.slots or {})
    context = {"force_continue": "继续" in action_text}
    artifacts = {k: v for k, v in (session.artifacts or {}).items() if k != "waiting"}
    if "取消" in action_text:
        session.status = AgentSession.STATUS_FAILED
        session.error = "用户取消任务"
        session.artifacts = artifacts
        _agent_set_observer(session, "failed", "任务已取消", "failed", "用户取消任务")
        session.messages = list(session.messages or []) + [{"role": "assistant", "content": "Agent 调查任务已取消。"}]
        session.save(update_fields=["status", "error", "messages", "updated_at"])
        return
    if "切换高清底图" in action_text:
        slots["source"] = "mapbox"
        session.slots = slots
        session.status = AgentSession.STATUS_RUNNING
        session.artifacts = artifacts
        session.save(update_fields=["slots", "status", "artifacts", "updated_at"])
        _agent_set_observer(session, "select_source", "选择图像源", "running", "已切换为高清底图，准备重新获取影像", {"source": "mapbox"})
        context["force_continue"] = True
        _run_agent_background(session.id, context)
        return
    if "快速模式重试" in action_text:
        session.mode = "fast"
        session.status = AgentSession.STATUS_RUNNING
        session.artifacts = artifacts
        session.save(update_fields=["mode", "status", "artifacts", "updated_at"])
        _agent_set_observer(session, "vl_analysis", "视觉模型解译", "running", "已切换快速视觉模型，准备重新解译")
        context["resume_with_scene"] = True
        _run_agent_background(session.id, context)
        return
    if "扩大时间范围" in action_text:
        slots.pop("date_start", None)
        slots.pop("date_end", None)
        session.slots = slots
        session.status = AgentSession.STATUS_RUNNING
        session.artifacts = artifacts
        session.save(update_fields=["slots", "status", "artifacts", "updated_at"])
        _agent_set_observer(session, "retrieve_imagery", "检索并生成影像", "running", "已扩大时间范围，准备重新检索 Sentinel-2 候选")
        _run_agent_background(session.id, {"force_continue": True})
        return
    if "继续" in action_text:
        session.status = AgentSession.STATUS_RUNNING
        session.artifacts = artifacts
        session.save(update_fields=["status", "artifacts", "updated_at"])
        _agent_set_observer(session, "quality_check", "检查影像质量", "running", "已收到确认，将继续进入模型解译")
        context["resume_with_scene"] = True
        _run_agent_background(session.id, context)
