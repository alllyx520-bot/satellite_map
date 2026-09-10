"""Agent 工具注册表。

每个工具包一个已验证的领域函数，所有领域调用经 ``_views.X`` 运行时查找，
保持既有 ``patch("map_api.views.X")`` 测试契约（与 orchestrator 同模式）。
工具在领域门控触发时抛 :class:`WaitingForUser`，由循环捕获并暂停。

工具接收 ``ctx``（循环工作记忆 dict）与 ``args``（模型给的参数），
返回结构化 dict（``{"status": "ok"|"error", ...}``）。
"""
import json
import logging
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

class _ViewsProxy:
    """惰性访问 views，允许独立导入工具注册表和运行 smoke。"""
    def __getattr__(self, name):
        from .. import views
        return getattr(views, name)


_views = _ViewsProxy()
from ..models import ImageryScene
from ..payloads import scene_brief_payload, sentinel_retrieval_timeline_payload
from .waiting import WaitingForUser, option
from .registry import definitions_from_registry

logger = logging.getLogger(__name__)


# ---------------- 工作记忆 ctx ----------------

def build_ctx(session, context):
    """从 session + 启动 context 构造循环工作记忆。

    复用旧 run_agent_session 的 scene 解析逻辑：优先 context.scene_id / file_name，
    其次 resume_with_scene + session.scene_id。
    """
    context = context or {}
    slots = dict(session.slots or {})
    ctx = {
        "slots": slots,
        "goal": session.goal,
        "mode": session.mode,
        "bbox": slots.get("bbox") or (context.get("bbox") if isinstance(context.get("bbox"), dict) else None),
        "scene_id": context.get("scene_id") or session.scene_id,
        "file_name": os.path.basename(context.get("file_name", "") or "") or None,
        "force_continue": bool(context.get("force_continue")),
        "ndwi": None,
        "vision_answer": None,
        "analysis_method": None,
        "facts": {},
        "hypotheses": [],
        "evidence": [],
        "image_path": None,
        "visual_context": None,
        "vision_calls": 0,
        "final_review_calls": 0,
        "vision_call_keys": [],
        "final_review_done": False,
    }
    if context.get("scene_id") or (context.get("resume_with_scene") and session.scene_id):
        sid = context.get("scene_id") or session.scene_id
        scene = ImageryScene.objects.filter(id=sid).first()
        if scene:
            _ctx_attach_scene(ctx, scene)
    elif ctx["file_name"]:
        scene = ImageryScene.objects.filter(file_name=ctx["file_name"]).first()
        if scene:
            _ctx_attach_scene(ctx, scene)
    persisted = dict(session.artifacts or {})
    ctx["vision_calls"] = int(persisted.get("vision_calls") or 0)
    ctx["final_review_calls"] = int(persisted.get("final_review_calls") or 0)
    ctx["vision_call_keys"] = list(persisted.get("vision_call_keys") or [])[:20]
    ctx["final_review_done"] = bool(persisted.get("final_review_done"))
    refresh_ctx_scene(ctx)
    return ctx


def _ctx_attach_scene(ctx, scene):
    ctx["scene_id"] = scene.id
    ctx["file_name"] = scene.file_name
    bbox = {"min_lng": scene.min_lng, "min_lat": scene.min_lat, "max_lng": scene.max_lng, "max_lat": scene.max_lat}
    ctx["bbox"] = bbox
    ctx["slots"]["bbox"] = bbox
    ctx["slots"]["source"] = _views._scene_source_key(scene)
    try:
        ctx["image_path"] = str(_views.safe_media_path(_views.SAVE_DIR, scene.file_name, ('.jpg', '.jpeg', '.png')) or "")
    except Exception:
        ctx["image_path"] = None


def refresh_ctx_scene(ctx):
    """刷新当前场景的安全视觉上下文。

    工具执行后 scene_id/file_name 才可能出现；每轮决策前重新挂载，保证
    GLM 看到的元数据与实际图片一致，同时不把 polygon 或本地绝对路径送入模型。
    """
    scene = _resolve_scene(ctx, {})
    if not scene:
        return None
    _ctx_attach_scene(ctx, scene)
    metadata = dict(scene.metadata or {})
    quality = _views.imagery_quality_payload(scene) or {}
    ctx["visual_context"] = {
        "scene_id": scene.id,
        "file_name": scene.file_name,
        "source": _views._scene_source_key(scene),
        "source_label": scene.source_label,
        "acquired_at": scene.acquired_at.isoformat() if scene.acquired_at else None,
        "gsd_m": metadata.get("source_asset_gsd_m") or scene.gsd_m,
        "preview_scale_m": metadata.get("preview_scale_m") or metadata.get("rendered_gsd_m") or scene.gsd_m,
        "cloud_percent": scene.cloud_percent,
        "coverage_ratio": metadata.get("target_coverage_ratio"),
        "valid_pixel_ratio": metadata.get("valid_image_ratio"),
        "polygon_clipped": bool(metadata.get("district_polygon")),
        "ndwi": ctx.get("ndwi") if isinstance(ctx.get("ndwi"), dict) else None,
        "quality_summary": quality.get("summary"),
        "quality_cautions": quality.get("cautions") or [],
    }
    return scene


def vision_image_key(ctx):
    """返回稳定图片内容键；失败时退回 scene/file 标识。"""
    path = (ctx or {}).get("image_path")
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except (OSError, TypeError):
        return f"scene:{(ctx or {}).get('scene_id')}|file:{(ctx or {}).get('file_name')}"


def _resolve_scene(ctx, args):
    sid = args.get("scene_id") or ctx.get("scene_id")
    if sid:
        s = ImageryScene.objects.filter(id=sid).first()
        if s:
            return s
    fn = args.get("file_name") or ctx.get("file_name")
    if fn:
        return ImageryScene.objects.filter(file_name=os.path.basename(fn)).first()
    return None


def _scene_bbox(scene):
    if not scene:
        return None
    return {"min_lng": scene.min_lng, "min_lat": scene.min_lat, "max_lng": scene.max_lng, "max_lat": scene.max_lat}


def _with_ctx_defaults(ctx, args, *, include_bbox=True):
    """给模型工具参数补齐工作记忆中的确定性值。"""
    result = dict(args or {})
    slots = ctx.get("slots") or {}
    if include_bbox and not result.get("bbox") and ctx.get("bbox"):
        result["bbox"] = ctx["bbox"]
    for key in ("place_name", "date_start", "date_end", "mode", "file_name", "scene_id"):
        if not result.get(key):
            value = slots.get(key) if key in slots else ctx.get(key)
            if value:
                result[key] = value
    return result


# ---------------- 工具实现 ----------------

def _tool_geocode_place(ctx, args):
    args = _with_ctx_defaults(ctx, args, include_bbox=False)
    place_name = (args.get("place_name") or "").strip()
    if not place_name:
        return {"status": "error", "message": "place_name 不能为空"}
    # 高德对带完整省/市前缀的区名偶尔返回空结果；按确定性顺序
    # 尝试全称、末级区县名，避免让模型浪费多轮猜测地名。
    candidates = [place_name]
    for suffix in ("区", "县", "市", "州", "旗"):
        idx = place_name.rfind(suffix)
        if idx >= 1:
            short = place_name[max(0, place_name.rfind("市", 0, idx) + 1):idx + 1]
            if short and short not in candidates:
                candidates.append(short)
    last_error = None
    location = None
    resolved_query = place_name
    for candidate in candidates:
        try:
            location = _views.resolve_district_bbox(candidate)
            resolved_query = candidate
            break
        except ValueError as exc:
            last_error = exc
    if location is None:
        raise last_error or ValueError(f"未找到行政区：{place_name}")
    ctx["bbox"] = location["bbox"]
    ctx["slots"]["place_name"] = location.get("name") or resolved_query
    ctx["slots"]["resolved_place"] = {
        "name": location["name"], "adcode": location["adcode"],
        "level": location["level"], "bbox_policy": location["bbox_policy"],
        "polygon": location.get("polygon") or [],
        "geometry_type": location.get("geometry_type") or "Polygon",
    }
    return {
        "status": "ok",
        "result": {
            "name": location["name"], "bbox": location["bbox"],
            "adcode": location["adcode"], "level": location["level"],
            "bbox_policy": location["bbox_policy"],
            "polygon_points": sum(len(ring) for ring in (location.get("polygon") or []) if isinstance(ring, list)),
            "geometry_type": location.get("geometry_type") or "Polygon",
            "query": resolved_query,
            "query_fallback": resolved_query != place_name,
        },
    }


def _tool_search_sentinel_imagery(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    slots = ctx["slots"]
    date_start = args.get("date_start") or slots.get("date_start")
    date_end = args.get("date_end") or slots.get("date_end")
    max_cloud = args.get("max_cloud", 60)
    collection = args.get("collection") or "sentinel-2-l2a"
    retrieval = None
    try:
        scene, candidate, retrieval = _views._agent_fetch_sentinel(bbox, {
            "date_start": date_start, "date_end": date_end,
        }, collection=collection)
    except (requests.RequestException, ValueError) as exc:
        raise WaitingForUser(
            f"Sentinel-2 检索失败：{str(exc)[:300]}",
            [option("retry_step"), option("expand_dates"), option("switch_source"), option("cancel")],
            step_id="retrieve_imagery", label="检索并生成影像",
            data={"bbox": bbox, "error": str(exc)[:300]},
        )
    if not scene:
        raise WaitingForUser(
            "Sentinel-2 候选覆盖不足或无可渲染影像。",
            [option("retry_step"), option("expand_dates"), option("switch_source"), option("cancel")],
            step_id="retrieve_imagery", label="检索并生成影像",
            data={"bbox": bbox, "retrieval": retrieval or {}},
        )
    if (scene.metadata or {}).get("temporal_consistency") in {"mixed_dates", "cross_date_mosaic"}:
        raise WaitingForUser("候选覆盖由不同日期影像拼接，不能作为单期调查证据。请修改条件重新检索。",
                             [option("expand_dates"), option("retry_step"), option("cancel")],
                             step_id="quality_check", label="日期一致性未通过", data={"bbox": bbox})
    # 挂载场景
    ctx["scene_id"] = scene.id
    ctx["file_name"] = scene.file_name
    ctx["slots"]["source"] = _views._scene_source_key(scene)
    resolved_place = ctx["slots"].get("resolved_place") or {}
    polygon = resolved_place.get("polygon")
    if polygon:
        metadata = dict(scene.metadata or {})
        metadata["district_polygon"] = polygon
        metadata["district_geometry_type"] = resolved_place.get("geometry_type") or "Polygon"
        scene.metadata = metadata
        scene.save(update_fields=["metadata", "updated_at"])
    # 质量门控（与旧 run_agent_session 一致）
    date_matched, date_issue = _views._scene_matches_requested_dates(scene, slots)
    if not date_matched:
        raise WaitingForUser(
            f"{date_issue} 请修改日期条件或重新检索。",
            [option("expand_dates"), option("retry_step"), option("cancel")],
            step_id="quality_check", label="检查影像质量",
            data={"scene": scene_brief_payload(scene), "date_issue": date_issue},
        )
    if scene.cloud_percent is not None and scene.cloud_percent > min(max_cloud, 30):
        raise WaitingForUser(
            f"当前 Sentinel-2 候选云量为 {scene.cloud_percent:g}%，未通过云量门禁。请重新检索。",
            [option("retry_step"), option("expand_dates"), option("cancel")],
            step_id="quality_check", label="检查影像质量",
            data={"scene": scene_brief_payload(scene)},
        )
    quality = _views.imagery_quality_payload(scene)
    return {
        "status": "ok",
        "result": {"scene": scene_brief_payload(scene), **sentinel_retrieval_timeline_payload(retrieval)},
        "quality": quality,
    }


def _tool_fetch_mapbox_imagery(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    basemap_source = str(args.get("basemap_source") or "mapbox").strip().lower()
    if basemap_source not in ("mapbox", "tianditu", "esri"):
        return {"status": "error", "message": f"不支持的底图影像源: {basemap_source}"}
    try:
        scene, retrieval = _views._agent_fetch_mapbox(bbox, basemap_source=basemap_source)
    except Exception as exc:
        return {"status": "error", "message": f"高清底图({basemap_source})下载失败：{str(exc)[:200]}"}
    ctx["scene_id"] = scene.id
    ctx["file_name"] = scene.file_name
    ctx["slots"]["source"] = basemap_source
    return {"status": "ok", "result": {"scene": scene_brief_payload(scene)}}


def _tool_compute_ndwi(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    scene = _resolve_scene(ctx, args)
    if not scene:
        return {"status": "error", "message": "未找到影像，请先 search_sentinel_imagery 或 fetch_mapbox_imagery"}
    bbox = args.get("bbox") or ctx.get("bbox") or _scene_bbox(scene)
    if scene.source != "sentinel2":
        return {"status": "failed", "message": "该影像源不支持光谱指数（仅 Sentinel-2 光学）。", "result": {"available": False}, "diagnostics": {"code": "not_supported"}}
    metadata = scene.metadata or {}
    if metadata.get("temporal_consistency") in {"mixed_dates", "cross_date_mosaic"}:
        return {"status": "failed", "message": "跨日期拼接不能用于单期指标计算", "result": {"available": False}}
    district_polygon = metadata.get("district_polygon")
    titiler = os.environ.get("TITILER_ENDPOINT") or "https://titiler.xyz"
    if metadata.get("grid_mosaic") and metadata.get("mosaic_items") and metadata.get("mosaic_candidates"):
        try:
            scene_valid_ratio = float(metadata.get("valid_image_ratio")) if metadata.get("valid_image_ratio") is not None else None
            scene_coverage = float(metadata.get("target_coverage_ratio")) if metadata.get("target_coverage_ratio") is not None else None
        except (TypeError, ValueError):
            scene_valid_ratio = scene_coverage = None
        if (scene_valid_ratio is not None and scene_valid_ratio < 0.60) or (scene_coverage is not None and scene_coverage < 0.60):
            return {
                "status": "failed",
                "result": {
                    "available": False,
                    "reason": "网格影像整体覆盖或有效像素率低于 60%，拒绝输出 NDWI 数值。",
                    "valid_image_ratio": scene_valid_ratio,
                    "target_coverage_ratio": scene_coverage,
                    "limitations": "低质量网格只能保留定性线索，不能形成区域量化结论。",
                },
            }
        candidates = {
            item.get("product_id") or item.get("item_id"): _views._candidate_from_mosaic_metadata(item)
            for item in (metadata.get("mosaic_candidates") or [])
        }
        summaries = []
        failed_grid_count = 0
        jobs = []
        for item in metadata.get("mosaic_items") or []:
            candidate = candidates.get(item.get("product_id") or item.get("item_id"))
            tile_bbox = item.get("tile_bbox")
            if candidate and tile_bbox:
                jobs.append((candidate, tile_bbox))
        with ThreadPoolExecutor(max_workers=min(3, len(jobs) or 1)) as pool:
            futures = [pool.submit(_views.compute_ndwi_summary, candidate, tile_bbox, titiler, polygon=district_polygon) for candidate, tile_bbox in jobs]
            for future in as_completed(futures):
                try:
                    summary = future.result()
                except Exception as exc:
                    failed_grid_count += 1
                    logger.warning("NDWI 网格计算失败，拒绝不完整汇总: %s", str(exc)[:200])
                    continue
                if summary.get("available"):
                    summaries.append(summary)
        if summaries:
            sample_total = sum(int(s.get("sample_size_px") or 0) for s in summaries)
            water_ratio = sum(float(s.get("water_ratio") or 0) * int(s.get("sample_size_px") or 0) for s in summaries) / max(1, sample_total)
            mean_ndwi = sum(float(s.get("mean_ndwi") or 0) * int(s.get("sample_size_px") or 0) for s in summaries) / max(1, sample_total)
            expected_grid_count = len(metadata.get("mosaic_items") or [])
            if len(summaries) != expected_grid_count or sample_total <= 0:
                ndwi = {"available": False, "reason": f"仅 {len(summaries)}/{expected_grid_count} 个网格获得有效 NDWI 样本，不能汇总不完整区域。", "grid_count": len(summaries), "grid_expected_count": expected_grid_count, "grid_failed_count": expected_grid_count - len(summaries)}
            else:
                ndwi = {"available": True, "method": "NDWI=(Green-NIR)/(Green+NIR)", "threshold": summaries[0].get("threshold", 0.1), "water_ratio": round(water_ratio, 4), "water_percent": round(water_ratio * 100, 1), "mean_ndwi": round(mean_ndwi, 4), "max_ndwi": max((float(s.get("max_ndwi")) for s in summaries if s.get("max_ndwi") is not None), default=None), "sample_size_px": sample_total, "grid_count": len(summaries), "grid_expected_count": expected_grid_count, "grid_failed_count": max(expected_grid_count - len(summaries), failed_grid_count), "limitations": "按网格独立计算并汇总；已按 SCL 排除云、云影、卷云和雪，仍属于区域筛查结果。"}
        else:
            ndwi = {"available": False, "reason": "网格候选均未能提供可用 NDWI 样本。"}
    elif metadata.get("mosaic") and metadata.get("mosaic_candidates"):
        cands = [_views._candidate_from_mosaic_metadata(item) for item in (metadata.get("mosaic_candidates") or [])]
        ndwi = _views.compute_ndwi_mosaic_summary(
            cands,
            bbox,
            titiler,
            polygon=district_polygon,
        )
    else:
        cand = type("Candidate", (), {"assets": metadata.get("assets") or {}, "product_id": "", "item_id": "", "collection": metadata.get("collection") or "sentinel-2-l2a"})()
        ndwi = _views.compute_ndwi_summary(cand, bbox, titiler, polygon=district_polygon)
    ctx["ndwi"] = ndwi
    return {"status": "ok" if ndwi.get("available") else "failed", "result": ndwi, "message": ndwi.get("reason", "")}


def _tool_analyze_imagery(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    file_name = args.get("file_name") or ctx.get("file_name")
    if not file_name:
        return {"status": "error", "message": "缺少 file_name，请先获取影像"}
    question = (args.get("question") or ctx.get("goal") or "").strip()
    if not question:
        return {"status": "error", "message": "question 不能为空"}
    mode = args.get("mode") or ctx.get("mode") or "precise"
    if mode not in _views.ANALYSIS_MODES:
        mode = "precise"
    scene = _resolve_scene(ctx, args)
    gsd = args.get("gsd") or (scene.gsd_m if scene else None)
    bbox = args.get("bbox") or ctx.get("bbox") or _scene_bbox(scene)
    payload = {
        "file_name": file_name,
        "scene_id": ctx.get("scene_id"),
        "question": question,
        "mode": mode,
        "active_perception": _views.ANALYSIS_MODES.get(mode, _views.ANALYSIS_MODES["precise"])["active_perception"],
        "history": [],
        "gsd": gsd,
        "bbox": bbox,
    }
    resp = _views.run_vl_analysis(payload)
    try:
        body = json.loads(resp.content)
    except Exception:
        return {"status": "error", "message": "视觉模型返回无法解析"}
    if resp.status_code != 200 or body.get("code") != 200:
        return {"status": "error", "message": body.get("msg", "视觉模型解译失败")}
    data = body["data"]
    analysis_method = data.get("analysis_method") or {}
    output_quality = (analysis_method.get("output_quality") or {}) if isinstance(analysis_method, dict) else {}
    ctx["vision_answer"] = data.get("answer")
    ctx["analysis_method"] = analysis_method
    # 输出不稳门控（与旧 run_agent_session 一致）
    if output_quality.get("fallback_used"):
        raise WaitingForUser(
            "视觉模型没有返回稳定的结构化解译结果，无法继续复核。请选择重试。",
            [option("retry_step"), option("retry_fast"), option("cancel")],
            step_id="vl_analysis", label="视觉模型解译",
            data={"output_quality": output_quality, "scene": scene_brief_payload(scene) if scene else None},
        )
    return {
        "status": "ok",
        "result": {
            "answer": data.get("answer"),
            "targets": data.get("targets", []),
            "active_stages": data.get("active_stages"),
            "analysis_method": analysis_method,
        },
    }


def _tool_compute_spectral_index(ctx, args):
    from ..spectral_products import compute_spectral_summary
    scene = _resolve_scene(ctx, args)
    if not scene or scene.source != "sentinel2":
        return {"status": "failed", "message": "该影像源不支持光谱指数（仅 Sentinel-2 光学）。", "result": {"available": False}}
    metadata = scene.metadata or {}
    if metadata.get("mosaic") or metadata.get("grid_mosaic"):
        return {"status": "failed", "message": "当前通用指数入口需要独立单景；多景 NDWI 请使用专用同日合成工具", "result": {"available": False}}
    index = args["index"]
    candidate = type("Candidate", (), {"assets": metadata.get("assets") or {}, "collection": metadata.get("collection") or "sentinel-2-l2a"})()
    result = compute_spectral_summary(candidate, args.get("bbox") or ctx.get("bbox") or _scene_bbox(scene),
                                      index=index, threshold=args.get("threshold"),
                                      titiler_endpoint=os.environ.get("TITILER_ENDPOINT"), polygon=metadata.get("district_polygon"))
    if result.get("available"):
        result["scene_id"] = scene.id
        result["product_id"] = scene.product_id
        result["acquired_at"] = scene.acquired_at.isoformat() if scene.acquired_at else None
        ctx.setdefault("spectral_indices", {})[index] = result
    return {"status": "ok" if result.get("available") else "failed", "message": result.get("reason", ""), "result": result}


# ---------------- 证据型外部数据工具 ----------------

from ..utils import external_data as _external


def _tool_query_fire_detections(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    if not os.environ.get("FIRMS_MAP_KEY", "").strip():
        return {"status": "error", "message": "缺少 FIRMS_MAP_KEY 环境变量（NASA FIRMS Map Key，免费申请后配置即可）"}
    try:
        data = _external.query_firms_fires(bbox, days=args.get("days") or 3, source=args.get("source") or _external.FIRMS_DEFAULT_SOURCE)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": f"FIRMS 火点查询失败：{str(exc)[:300]}"}
    ctx.setdefault("facts", {})["firms_fires"] = {"count": data["count"], "days": data["days"], "source": data["source"]}
    return {"status": "ok", "result": _external.summarize_firms_fires(data)}


def _tool_query_osm_context(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    try:
        data = _external.query_osm_context(bbox)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": f"OSM 地物上下文查询失败：{str(exc)[:300]}"}
    data["attribution"] = _external.OSM_ATTRIBUTION
    ctx.setdefault("facts", {})["osm_context"] = {"building_count": data["building_count"], "road_count": data["road_count"]}
    return {"status": "ok", "result": data}


def _tool_query_weather_context(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    lat, lng = args.get("lat"), args.get("lng")
    bbox = args.get("bbox") or ctx.get("bbox")
    if (lat is None or lng is None) and bbox:
        lat = (float(bbox["min_lat"]) + float(bbox["max_lat"])) / 2
        lng = (float(bbox["min_lng"]) + float(bbox["max_lng"])) / 2
    if lat is None or lng is None:
        return {"status": "error", "message": "缺少经纬度或 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    date = args.get("date") or (ctx.get("slots") or {}).get("date_start") or None
    try:
        data = _external.query_weather_context(lat, lng, date=date)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": f"气象上下文查询失败：{str(exc)[:300]}"}
    ctx.setdefault("facts", {})["weather_context"] = {"mode": data["mode"], "days": len(data["daily"])}
    return {"status": "ok", "result": data}


def _tool_query_water_baseline(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    ndwi_ratio = args.get("ndwi_ratio")
    if ndwi_ratio is None and isinstance(ctx.get("ndwi"), dict) and ctx["ndwi"].get("available"):
        ndwi_ratio = ctx["ndwi"].get("water_ratio")
    try:
        data = _external.query_water_baseline(bbox, ndwi_ratio=ndwi_ratio)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": f"GSW 水体基线查询失败：{str(exc)[:300]}"}
    if not data.get("available"):
        return {"status": "failed", "message": data.get("reason", "无 GSW 覆盖"), "result": data}
    ctx.setdefault("facts", {})["water_baseline"] = {"permanent_water_ratio": data["permanent_water_ratio"]}
    return {"status": "ok", "result": data}


def _tool_query_landcover_context(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    bbox = args.get("bbox") or ctx.get("bbox")
    if not bbox:
        return {"status": "error", "message": "缺少 bbox，请先调用 geocode_place 或由用户提供框选区域"}
    try:
        data = _external.query_landcover_context(bbox)
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "message": f"WorldCover 土地覆盖查询失败：{str(exc)[:300]}"}
    if not data.get("available"):
        return {"status": "failed", "message": data.get("reason", "无 WorldCover 覆盖"), "result": data}
    ctx.setdefault("facts", {})["landcover_context"] = {"top": (data["landcover_top"] or [{}])[0].get("class")}
    return {"status": "ok", "result": data}


# ---------------- 注册表 ----------------

REGISTRY = {
    "compute_spectral_index": {
        "name": "compute_spectral_index", "description": "读取 Sentinel-2 原始波段与 SCL，应用 scale/offset、AOI 和 QA 后计算 NDVI/NDWI/MNDWI。vegetation/agriculture 任务必须使用 NDVI。",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["index"],
                       "properties": {"index": {"enum": ["ndvi", "ndwi", "mndwi"]},
                                      "threshold": {"type": "number", "minimum": -1, "maximum": 1},
                                      "scene_id": {"type": "integer", "minimum": 1}, "bbox": {"type": "object"}}},
        "fn": _tool_compute_spectral_index,
    },
    "geocode_place": {
        "name": "geocode_place",
        "description": "把自然语言地点（城市/区县名）解析成经纬度 bbox。调查开始时定位用。",
        "parameters": {
            "type": "object",
            "properties": {"place_name": {"type": "string", "description": "中文地点名，如 南宁市"}},
            "required": ["place_name"],
        },
        "fn": _tool_geocode_place,
    },
    "search_sentinel_imagery": {
        "name": "search_sentinel_imagery",
        "description": "检索公开卫星影像并渲染。collection 默认 sentinel-2-l2a(Sentinel-2 光学，适合水体/植被/农业/宏观土地利用/时效变化)；sentinel-2-c1-l2a 为新版基线等价产品；L2A 无覆盖时可试 sentinel-2-l1c(无 SCL 云掩膜，指数不可用)；洪水/淹没/多云/夜间等全天候需求用 sentinel-1-grd(SAR)；地形/坡度/高程用 cop-dem-glo-30(静态 DEM)。内含云量与时相质量门控，可能需要用户确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"},
                "date_start": {"type": "string", "description": "ISO 日期，可选"},
                "date_end": {"type": "string", "description": "ISO 日期，可选"},
                "max_cloud": {"type": "number", "description": "云量上限百分比，默认 60"},
                "collection": {"type": "string", "default": "sentinel-2-l2a",
                               "enum": ["sentinel-2-l2a", "sentinel-2-c1-l2a", "sentinel-2-l1c", "sentinel-1-grd", "cop-dem-glo-30"],
                               "description": "影像 collection；洪水/全天候→sentinel-1-grd，地形→cop-dem-glo-30，L2A 无覆盖→sentinel-2-l1c"},
            },
            "required": [],
        },
        "fn": _tool_search_sentinel_imagery,
    },
    "fetch_mapbox_imagery": {
        "name": "fetch_mapbox_imagery",
        "description": "下载高清底图(Mapbox/天地图/Esri)。适合建筑/道路/设施/小目标细节判读。",
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"},
                "basemap_source": {"type": "string", "default": "mapbox",
                                   "enum": ["mapbox", "tianditu", "esri"],
                                   "description": "高清底图来源：中国区行政区调查优先 tianditu(合规)；全球细节可用 esri；默认 mapbox"},
            },
            "required": [],
        },
        "fn": _tool_fetch_mapbox_imagery,
    },
    "compute_ndwi": {
        "name": "compute_ndwi",
        "description": "对当前 Sentinel-2 影像计算轻量 NDWI 水体量化（水体比例/均值）。仅 Sentinel-2 可用，作为筛查线索而非精确制图。",
        "parameters": {
            "type": "object",
            "properties": {
                "scene_id": {"type": "integer", "description": "可选；未提供则用当前影像"},
                "bbox": {"type": "object", "description": "可选"},
            },
            "required": [],
        },
        "fn": _tool_compute_ndwi,
    },
    "analyze_imagery": {
        "name": "analyze_imagery",
        "description": "调用 Qwen VL 视觉模型解译当前影像，可选主动感知放大。返回解译结论与目标测量。同一调查最多调用 2 次。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "可选；未提供则用调查目标"},
                "mode": {"type": "string", "enum": ["precise", "fast"], "description": "可选"},
                "file_name": {"type": "string", "description": "可选；未提供则用当前影像"},
            },
            "required": [],
        },
        "fn": _tool_analyze_imagery,
    },
    "query_fire_detections": {
        "name": "query_fire_detections",
        "description": "查询 bbox 内近 1-5 天 NASA FIRMS 活跃火点（位置/FRP/置信度/昼夜）。用户问火灾、火点、焚烧、疑似烟点时用于取证。需要 FIRMS_MAP_KEY。",
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"},
                "days": {"type": "integer", "minimum": 1, "maximum": 5, "description": "回溯天数，默认 3，最多 5"},
                "source": {"type": "string", "default": "VIIRS_SNPP_NRT", "description": "FIRMS 数据源，默认 VIIRS_SNPP_NRT"},
            },
            "required": [],
        },
        "fn": _tool_query_fire_detections,
    },
    "query_osm_context": {
        "name": "query_osm_context",
        "description": "统计 bbox 内 OpenStreetMap 建筑物数量、道路条数、水系要素与主要 landuse 类别。需要语义佐证时（如“变化发生在住宅区旁”“周边是什么地物”）使用。",
        "parameters": {
            "type": "object",
            "properties": {"bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"}},
            "required": [],
        },
        "fn": _tool_query_osm_context,
    },
    "query_weather_context": {
        "name": "query_weather_context",
        "description": "查询拍摄日或近 7 天天气（降水/气温/天气现象，Open-Meteo，免 key）。需要拍摄日或近期天气佐证云、烟、洪水、积雪判别时使用；lat/lng 省略时用 bbox 中心。",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "可选；未提供则用 bbox 中心"},
                "lng": {"type": "number", "description": "可选；未提供则用 bbox 中心"},
                "date": {"type": "string", "description": "ISO 日期（YYYY-MM-DD），提供则查该日历史天气，否则查近 7 天"},
            },
            "required": [],
        },
        "fn": _tool_query_weather_context,
    },
    "query_water_baseline": {
        "name": "query_water_baseline",
        "description": "读取 JRC Global Surface Water 历史水体基线（1984-2021 occurrence），给出常年/季节性水体占比，并可与当期 NDWI 对照。判别洪水淹没、水面异常变化时作为历史参照。",
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"},
                "ndwi_ratio": {"type": "number", "description": "可选；当期 NDWI 水体占比（0-1），省略且有 NDWI 结果时自动带入"},
            },
            "required": [],
        },
        "fn": _tool_query_water_baseline,
    },
    "query_landcover_context": {
        "name": "query_landcover_context",
        "description": "读取 ESA WorldCover 2021 土地覆盖分类（10m，11 类），给出 bbox 内各类占比 top5。需要地类背景佐证（耕地/建成区/林地等）时使用；单年产品，不做跨年比较。",
        "parameters": {
            "type": "object",
            "properties": {"bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"}},
            "required": [],
        },
        "fn": _tool_query_landcover_context,
    },
}

# 统一的、可测试的工具定义视图；保留 REGISTRY 兼容现有调用方。
DEFINITIONS = definitions_from_registry(REGISTRY)

TOOL_SPECS = [
    {"name": v["name"], "description": v["description"], "parameters": v["parameters"]}
    for v in REGISTRY.values()
]
