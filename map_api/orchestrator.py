"""RemoteSensingAgent 编排层(Phase 7 从 views.py 拆出,M-4)。

受控而非全自主的调查流水线:槽位/计划解析 → 行政区定位 → 源选择 →
影像检索(Sentinel-2 多景拼接/Mapbox 高清)→ 质量门控(时相/云量,可等待用户确认)→
NDWI 轻量量化 → Qwen VL 专业解译 → GLM-5.3-Flash 复核 → 结果整理与历史落库。
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

from django.db import close_old_connections, transaction
from django.utils import timezone

class _ViewsProxy:
    """惰性访问 views，避免 orchestrator/agent.tools/views 顶层循环导入。"""
    def __getattr__(self, name):
        from . import views
        return getattr(views, name)


_views = _ViewsProxy()
from .geo_math import compute_image_plan, bbox_intersection_ratio, bbox_union_coverage_ratio, crop_sentinel_nodata_border, image_valid_ratio
from .imagery_sources.earth_search import EarthSearchProvider
from .imagery_sources.mapbox import MapboxProvider
from .media_paths import SAVE_DIR
from .models import AgentSession, ChatHistory, DownloadTask, ImageryScene
from .payloads import (
    _scene_source_key, imagery_quality_payload, scene_brief_payload,
    scene_payload, sentinel_retrieval_timeline_payload,
)
from .sentinel_pipeline import (
    sentinel_retrieval_result, compose_sentinel_grid, scene_from_sentinel_mosaic,
    create_done_download_task, sentinel_mosaic_cache_key,
)
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
    "review": "我用 GLM 对视觉结论做复核，重点检查证据边界、时效性和是否夸大。",
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
    {"id": "review", "label": "GLM 结论复核"},
    {"id": "complete", "label": "整理结果"},
]


def agent_session_payload(session):
    artifacts = dict(session.artifacts or {})
    # 行政区 polygon 可能有数千点；完整几何保留在数据库供计算，公开轮询只返回摘要。
    public_scene = artifacts.get("scene")
    if isinstance(public_scene, dict):
        public_scene = dict(public_scene)
        metadata = dict(public_scene.get("metadata") or {})
        polygon = metadata.pop("district_polygon", None)
        if polygon is not None:
            metadata["district_polygon_points"] = sum(len(r) for r in polygon if isinstance(r, list))
        public_scene["metadata"] = metadata
        artifacts["scene"] = public_scene
    return {
        "id": session.id,
        "request_id": session.request_id,
        "status": session.status,
        "cancel_requested": bool(session.cancel_requested),
        "goal": session.goal,
        "mode": session.mode,
        "slots": session.slots,
        "plan": session.plan,
        "timeline": session.timeline,
        "observer": (session.artifacts or {}).get("observer") or {},
        "messages": session.messages,
        "artifacts": artifacts,
        "error": session.error,
        "scene_id": session.scene_id,
        "history_id": session.history_id,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def execution_events_payload(session, after=None, limit=100):
    """为断线重连 UI 提供单调游标事件流。"""
    from .models import ExecutionEvent
    qs = ExecutionEvent.objects.filter(session_id=session.id).order_by("sequence")
    if after is not None:
        try:
            qs = qs.filter(sequence__gt=max(-1, int(after)))
        except (TypeError, ValueError):
            pass
    rows = list(qs[: max(1, min(int(limit or 100), 200))])
    from .agent.events import event_dict
    return {
        "events": [event_dict(e) for e in rows],
        "next_cursor": rows[-1].sequence if rows else after,
    }


def _agent_observer_payload(session, step_id, label, status, message, data=None):
    plan_steps = []
    raw_steps = (session.plan or {}).get("steps") or []
    default_ids = [item["id"] for item in AGENT_OBSERVER_DEFAULT_STEPS]
    if (session.slots or {}).get("task") == "water" and "ndwi" not in default_ids:
        default_ids.insert(default_ids.index("vl_analysis"), "ndwi")
    raw_ids = [item.get("id") for item in raw_steps if isinstance(item, dict)]
    # 初始 deterministic 计划只是兼容数据，不是模型真实动态计划；
    # 若它完整等同固定阶段列表，则只投影已发生的 timeline/current 步骤。
    if raw_ids == default_ids:
        raw_steps = []
    if not raw_steps:
        # 没有模型计划时仅投影已经写入 timeline 的步骤和当前步骤，
        # 不再向用户展示未来固定 pending 阶段。
        seen = []
        for item in (session.timeline or []):
            sid = item.get("id") if isinstance(item, dict) else None
            if sid and sid not in seen:
                seen.append(sid)
        if step_id and step_id not in seen:
            seen.append(step_id)
        labels = {item["id"]: item["label"] for item in AGENT_OBSERVER_DEFAULT_STEPS}
        raw_steps = [{"id": sid, "label": labels.get(sid, sid)} for sid in seen]
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
    # 读-计算-写必须整体持有行锁；单独 refresh 后 save 仍有竞态窗口。
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        artifacts = dict(locked.artifacts or {})
        artifacts["observer"] = _agent_observer_payload(locked, step_id, label, status, message, data)
        locked.artifacts = artifacts
        locked.save(update_fields=["artifacts", "updated_at"])
        session = locked


def _agent_step(session, step_id, label, status="done", message="", data=None, expected_claim=None):
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
            return False
        timeline = list(locked.timeline or [])
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
        locked.timeline = timeline
        artifacts = dict(locked.artifacts or {})
        artifacts["observer"] = _agent_observer_payload(locked, step_id, label, status, message, data)
        locked.artifacts = artifacts
        locked.save(update_fields=["timeline", "artifacts", "updated_at"])
        session = locked
    try:
        from .agent.events import emit
        emit(session.id, "quality_check" if step_id == "quality_check" else "checkpoint", {
            "phase": step_id, "status": status, "summary": message or label,
            "facts": data if isinstance(data, dict) else {},
        }, expected_claim=expected_claim)
    except Exception:
        logger.debug("unable to append observer event", exc_info=True)
    return True


def _agent_fail(session, message):
    try:
        with transaction.atomic():
            locked = AgentSession.objects.select_for_update().get(id=session.id)
            if locked.status == AgentSession.STATUS_COMPLETED:
                return
            timeline = list(locked.timeline or [])
            artifacts = dict(locked.artifacts or {})
            artifacts["observer"] = _agent_observer_payload(locked, "failed", "任务失败", "failed", message)
            artifacts.pop("worker_claim", None)
            artifacts.pop("worker_claimed_at", None)
            locked.status = AgentSession.STATUS_FAILED
            locked.error = message
            locked.messages = list(locked.messages or []) + [{"role": "assistant", "content": f"任务失败：{message}"}]
            locked.timeline = timeline
            locked.artifacts = artifacts
            locked.save(update_fields=["status", "error", "messages", "artifacts", "timeline", "updated_at"])
            session = locked
        try:
            from .agent.events import emit
            emit(session.id, "task_failed", {"phase": "failed", "status": "failed", "summary": message, "error": message})
        except Exception:
            logger.debug("unable to append failure event", exc_info=True)
    except AgentSession.DoesNotExist:
        logger.info("Agent session deleted before failure persistence: session=%s", getattr(session, "id", None))
        return


def _agent_wait(session, message, options=None, data=None, step_id="waiting_user", label="等待用户确认", expected_claim=None):
    with transaction.atomic():
        locked = AgentSession.objects.select_for_update().get(id=session.id)
        if locked.status in (AgentSession.STATUS_COMPLETED, AgentSession.STATUS_FAILED):
            return
        if expected_claim and (locked.artifacts or {}).get("worker_claim") != expected_claim:
            return False
        locked.status = AgentSession.STATUS_WAITING_USER
        artifacts = dict(locked.artifacts or {})
        artifacts["waiting"] = {"message": message, "options": options or [], "data": data or {}}
        artifacts["observer"] = _agent_observer_payload(locked, step_id, label, "waiting_user", message, data)
        artifacts.pop("worker_claim", None)
        artifacts.pop("worker_claimed_at", None)
        locked.artifacts = artifacts
        locked.messages = list(locked.messages or []) + [{"role": "assistant", "content": message, "options": options or []}]
        locked.save(update_fields=["status", "artifacts", "messages", "updated_at"])
        session = locked
    try:
        from .agent.events import emit
        emit(session.id, "user_confirmation_required", {
            "phase": step_id, "status": "waiting_user", "summary": message,
            "options": options or [], "data": data or {},
        })
    except Exception:
        logger.debug("unable to append waiting event", exc_info=True)
    return True


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


def _run_agent_background(session_id, context=None, runner=None):
    """启动 Agent 后台执行。

    ``AGENT_EXECUTION_MODE=queue`` 时只保留数据库中的 running 租约，
    由持久化 run_agent_worker 接管，避免 Web 进程重启丢失长任务。
    默认 thread 模式保持本地开发兼容。
    """
    mode = str(os.environ.get("AGENT_EXECUTION_MODE", "thread")).strip().lower()
    if mode in {"queue", "worker", "persistent"}:
        return False
    threading.Thread(target=runner or run_agent_session, args=(session_id, context or {}), daemon=True).start()
    return True


def _cloud_sort_value(candidate):
    return 100.0 if candidate.cloud_percent is None else float(candidate.cloud_percent)


def _sort_sentinel_preview_candidates(candidates):
    """预判阶段按云量优先，其次新鲜度和综合分排序。"""
    return sorted(
        candidates,
        key=lambda c: (
            _cloud_sort_value(c),
            -(c.acquired_at.timestamp() if c.acquired_at else 0),
            -(c.suitability_score or 0),
        ),
    )


def _rank_sentinel_grid_candidates(candidates, tile):
    """网格选景先保证空间交集，再在同等覆盖下优先低云量。"""
    return sorted(
        candidates,
        key=lambda c: (
            bbox_intersection_ratio(tile, c.bbox),
            -_cloud_sort_value(c),
            c.acquired_at.timestamp() if c.acquired_at else 0,
            c.suitability_score or 0,
        ),
        reverse=True,
    )


def _agent_fetch_sentinel(bbox, slots, force_grid=False):
    resolution = 1024
    plan = compute_image_plan(bbox["min_lng"], bbox["min_lat"], bbox["max_lng"], bbox["max_lat"], resolution)
    provider = EarthSearchProvider(titiler_endpoint=os.environ.get("TITILER_ENDPOINT", None) or None)
    candidates = provider.search(
        bbox,
        start_date=slots.get("date_start"),
        end_date=slots.get("date_end"),
        max_cloud=60,
        limit=int(os.environ.get("AGENT_SENTINEL_CANDIDATE_LIMIT", "40")) if plan["area_km2"] >= 1000 else int(os.environ.get("AGENT_SENTINEL_CANDIDATE_LIMIT", "15")),
        collection="sentinel-2-l2a",
    )
    if not candidates:
        return None, None, None
    large_area = plan["area_km2"] >= 1000
    has_full_candidate = any(bbox_intersection_ratio(bbox, c.bbox) >= 0.85 for c in candidates)
    # 与 sentinel_pipeline 的正式选景保持一致：云量是首要质量信号，
    # 综合评分只用于同云量候选打平，避免预判阶段选入高云量影像。
    preview_candidates = _sort_sentinel_preview_candidates(candidates)[:4]
    union_coverage = bbox_union_coverage_ratio(bbox, [c.bbox for c in preview_candidates])
    # 普通大范围行政区使用同一 bbox 的多景 mosaic，避免网格切片后的 no-data
    # 边缘把完整行政区误判为覆盖不足；仅显式 force_grid 才走网格实验路径。
    if force_grid or (large_area and not has_full_candidate and union_coverage < 0.85 and os.environ.get("AGENT_SENTINEL_GRID", "0") == "1"):
        return _agent_fetch_sentinel_grid(provider, candidates, bbox, plan, resolution, polygon=(slots.get("resolved_place") or {}).get("polygon"))
    retrieval = sentinel_retrieval_result(
        provider,
        candidates,
        bbox,
        plan,
        resolution,
        file_prefix="agent_sentinel",
        # 大范围 bbox 不能用单景 60% 覆盖就算成功：那会把城市北/南侧
        # 直接裁掉，再错误降级成 Mapbox。优先要求多景拼接达到 98% 覆盖。
        min_coverage=0.98 if large_area else 0.85,
        min_valid_ratio=0.60 if large_area else 0.80,
        max_auto_crop_ratio=0.40 if large_area else 0.03,
        max_mosaic_candidates=int(os.environ.get("AGENT_SENTINEL_MAX_MOSAIC_CANDIDATES", "12")) if large_area else None,
        polygon=(slots.get("resolved_place") or {}).get("polygon"),
    )
    return retrieval["scene"], retrieval["candidate"], retrieval


def _agent_fetch_sentinel_grid(provider, candidates, bbox, plan, resolution, polygon=None):
    """大范围行政区按网格独立渲染，再进行空间拼接。"""
    # 2×2 已能覆盖大多数城市级 bbox；只有极大区域才升到 3×3，
    # 避免一次任务触发 9 次 TiTiler 网络请求。
    columns = rows = 2 if plan["area_km2"] < 100000 else 3
    lon_step = (bbox["max_lng"] - bbox["min_lng"]) / columns
    lat_step = (bbox["max_lat"] - bbox["min_lat"]) / rows
    errors = []
    used = []
    jobs = []
    for gy in range(rows):
        for gx in range(columns):
            tile = {
                "min_lng": bbox["min_lng"] + gx * lon_step,
                "max_lng": bbox["min_lng"] + (gx + 1) * lon_step,
                "min_lat": bbox["min_lat"] + gy * lat_step,
                "max_lat": bbox["min_lat"] + (gy + 1) * lat_step,
            }
            ranked = _rank_sentinel_grid_candidates(candidates, tile)
            candidate = next((c for c in ranked if bbox_intersection_ratio(tile, c.bbox) >= 0.20), None)
            if candidate:
                jobs.append((gx, gy, tile, candidate))
            else:
                errors.append(f"网格 {gx},{gy} 无覆盖候选")
    def render_job(job):
        gx, gy, tile, candidate = job
        tile_w = max(256, int(round(plan["total_w"] / columns)))
        tile_h = max(256, int(round(plan["total_h"] / rows)))
        image_bytes = provider.render_candidate_jpeg(candidate, tile, tile_w, tile_h)
        if image_valid_ratio(image_bytes) < 0.25:
            raise ValueError("网格有效像素率过低")
        return {"candidate": candidate, "image_bytes": image_bytes, "coverage_ratio": bbox_intersection_ratio(tile, candidate.bbox), "grid_x": gx, "grid_y": gy, "tile_bbox": tile}
    rendered = []
    with ThreadPoolExecutor(max_workers=min(3, len(jobs) or 1)) as pool:
        futures = {pool.submit(render_job, job): job for job in jobs}
        for future in as_completed(futures):
            gx, gy, tile, candidate = futures[future]
            try:
                item = future.result()
                rendered.append(item)
                if candidate not in used:
                    used.append(candidate)
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{candidate.product_id or candidate.item_id} 网格 {gx},{gy}: {str(exc)[:120]}")
    min_tiles = max(2, int((columns * rows) * 0.50 + 0.999))
    if len(rendered) < min_tiles:
        raise ValueError("Sentinel-2 候选覆盖不足：" + "；".join(errors))
    mosaic_bytes, valid_ratio, item_summaries = compose_sentinel_grid(
        rendered, plan["total_w"], plan["total_h"], columns, rows,
    )
    processed = crop_sentinel_nodata_border(mosaic_bytes, bbox)
    if polygon:
        from .geo_math import clip_image_to_polygon
        mosaic_bytes, polygon_coverage = clip_image_to_polygon(processed["image_bytes"], processed["bbox"], polygon)
        processed["image_bytes"] = mosaic_bytes
    if float(processed["metadata"].get("removed_pixel_ratio") or 0) > 0.40:
        raise ValueError("Sentinel-2 候选覆盖不足：网格拼接后有效区域过小")
    effective_bbox = processed["bbox"]
    effective_plan = processed["plan"]
    used_coverage = bbox_union_coverage_ratio(bbox, [c.bbox for c in used])
    if used_coverage < 0.60 or valid_ratio < 0.60:
        raise ValueError(f"Sentinel-2 候选覆盖不足：网格拼接质量不足，覆盖率 {used_coverage:.1%}、有效像素率 {valid_ratio:.1%}")
    # 所有质量门禁通过后才落盘，避免失败候选留下无法关联的孤儿图片。
    file_name = f"agent_sentinel_grid_{uuid.uuid4().hex[:8]}.jpg"
    full_path = os.path.join(SAVE_DIR, file_name)
    with open(full_path, "wb") as f:
        f.write(processed["image_bytes"])
    scene = scene_from_sentinel_mosaic(
        file_name, used, effective_bbox, effective_plan["area_km2"],
        effective_plan["gsd_m"], sentinel_mosaic_cache_key(used, bbox, plan["total_w"], plan["total_h"]),
        {"width": plan["total_w"], "height": plan["total_h"]},
        used_coverage, valid_ratio,
        render_errors=errors, item_summaries=item_summaries,
    )
    metadata = dict(scene.metadata or {})
    metadata.update({"grid_mosaic": True, "grid_shape": {"columns": columns, "rows": rows}, "requested_bbox": bbox, "no_data_crop": processed["metadata"]})
    scene.metadata = metadata
    scene.save(update_fields=["metadata", "updated_at"])
    create_done_download_task(scene, file_name, effective_bbox, effective_plan, resolution, total=len(rendered))
    return scene, used[0], {"scene": scene, "candidate": used[0], "selected_candidates": used, "plan": effective_plan, "candidate_count": len(candidates), "cache_hit": False, "mosaic": True, "selection_method": "grid_mosaic", "target_coverage_ratio": used_coverage, "valid_image_ratio": valid_ratio, "no_data_crop": processed["metadata"], "render_errors": errors}


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
    progress = _download_progress.get(file_name) or {}
    if progress.get("status") == "partial" or int(progress.get("failed", 0) or 0) > 0:
        # Agent 结果会直接进入 VL 解译和报告，不能把带深灰占位瓦片的拼接图
        # 当成完整证据。手动下载流程仍可保留 partial 结果供用户检查。
        try:
            os.remove(result_path)
        except OSError:
            pass
        raise ValueError(
            f"Mapbox 高清底图仅完成部分瓦片（失败 {progress.get('failed', 0)}），请重试或改用 Sentinel-2"
        )
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



# 模型驱动工具循环已移至 map_api/agent/loop.py。使用惰性包装保持旧导出名，
# 同时避免导入 orchestrator 时再次拉起 loop → views 的循环依赖。
def run_agent_session(*args, **kwargs):
    from .agent.loop import run_agent_loop
    return run_agent_loop(*args, **kwargs)


def resume_waiting_agent_session(*args, **kwargs):
    from .agent.loop import resume_waiting_agent_session as _resume
    return _resume(*args, **kwargs)
