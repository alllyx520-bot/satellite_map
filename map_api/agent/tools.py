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


def _auto_source_fallback_enabled():
    """是否允许 Sentinel 失败后自动切换 Mapbox；生产可显式开启。"""
    return str(os.environ.get("AGENT_AUTO_SOURCE_FALLBACK", "0")).strip().lower() in {"1", "true", "yes", "on"}


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
    retrieval = None
    try:
        scene, candidate, retrieval = _views._agent_fetch_sentinel(bbox, {
            "date_start": date_start, "date_end": date_end,
        })
    except (requests.RequestException, ValueError) as exc:
        # Sentinel 失败不应直接把整个 Agent 挂起。先自动切换到可用的
        # Mapbox 高清底图，让模型继续完成空间判读；最终结果明确标注
        # “非近期影像”，只有两种来源都失败才需要用户介入。
        sentinel_error = str(exc)[:300]
        if not _auto_source_fallback_enabled():
            raise WaitingForUser(
                f"Sentinel-2 检索失败：{sentinel_error}",
                [option("expand_dates"), option("switch_source"), option("cancel")],
                step_id="retrieve_imagery", label="检索并生成影像",
                data={"bbox": bbox, "error": sentinel_error},
            )
        try:
            map_scene, map_retrieval = _views._agent_fetch_mapbox(bbox)
        except Exception as map_exc:
            raise WaitingForUser(
                f"Sentinel-2 与高清底图均不可用：Sentinel-2：{sentinel_error}；高清底图：{str(map_exc)[:160]}",
                [option("expand_dates"), option("cancel")],
                step_id="retrieve_imagery", label="检索并生成影像",
                data={"bbox": bbox, "error": sentinel_error, "fallback_error": str(map_exc)[:300]},
            )
        ctx["scene_id"] = map_scene.id
        ctx["file_name"] = map_scene.file_name
        ctx["slots"]["source"] = "mapbox"
        metadata = dict(map_scene.metadata or {})
        metadata["sentinel_fallback"] = True
        metadata["sentinel_error"] = sentinel_error
        metadata["temporal_limitations"] = "Sentinel-2 近期影像不可用，已自动切换 Mapbox 高清底图；不适合作为近期变化证据。"
        map_scene.metadata = metadata
        map_scene.save(update_fields=["metadata", "updated_at"])
        return {
            "status": "ok",
            "result": {"scene": scene_brief_payload(map_scene), "source_fallback": "mapbox", "sentinel_error": sentinel_error},
            "quality": _views.imagery_quality_payload(map_scene),
        }
    if not scene:
        # 某些 provider 会用空 scene 表示覆盖/渲染质量不足，而不是抛异常；
        # 该分支同样自动走高清底图，避免所有任务停在同一个确认弹窗。
        sentinel_error = "Sentinel-2 候选覆盖不足或无可渲染影像。"
        if not _auto_source_fallback_enabled():
            raise WaitingForUser(
                sentinel_error,
                [option("expand_dates"), option("switch_source"), option("cancel")],
                step_id="retrieve_imagery", label="检索并生成影像",
                data={"bbox": bbox, "retrieval": retrieval or {}},
            )
        try:
            map_scene, map_retrieval = _views._agent_fetch_mapbox(bbox)
        except Exception as map_exc:
            raise WaitingForUser(
                f"Sentinel-2 与高清底图均不可用：{sentinel_error} 高清底图：{str(map_exc)[:160]}",
                [option("expand_dates"), option("cancel")],
                step_id="retrieve_imagery", label="检索并生成影像",
                data={"bbox": bbox, "render_fallback_errors": (retrieval or {}).get("render_errors", []) if retrieval else [], "fallback_error": str(map_exc)[:300]},
            )
        ctx["scene_id"] = map_scene.id
        ctx["file_name"] = map_scene.file_name
        ctx["slots"]["source"] = "mapbox"
        metadata = dict(map_scene.metadata or {})
        metadata["sentinel_fallback"] = True
        metadata["sentinel_error"] = sentinel_error
        metadata["temporal_limitations"] = "Sentinel-2 近期影像不可用，已自动切换 Mapbox 高清底图；不适合作为近期变化证据。"
        map_scene.metadata = metadata
        map_scene.save(update_fields=["metadata", "updated_at"])
        return {
            "status": "ok",
            "result": {"scene": scene_brief_payload(map_scene), "source_fallback": "mapbox", "sentinel_error": sentinel_error},
            "quality": _views.imagery_quality_payload(map_scene),
        }
    # 挂载场景
    ctx["scene_id"] = scene.id
    ctx["file_name"] = scene.file_name
    ctx["slots"]["source"] = "sentinel2"
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
    if not date_matched and not ctx.get("force_continue"):
        raise WaitingForUser(
            f"{date_issue} 继续分析会降低结论时效性，是否扩大时间范围或切换高清底图？",
            [option("expand_dates"), option("switch_source"), option("continue"), option("cancel")],
            step_id="quality_check", label="检查影像质量",
            data={"scene": scene_brief_payload(scene), "date_issue": date_issue},
        )
    if scene.cloud_percent is not None and scene.cloud_percent > 30 and not ctx.get("force_continue"):
        raise WaitingForUser(
            f"当前 Sentinel-2 候选云量为 {scene.cloud_percent:g}%，可能影响水体判读。是否继续？",
            [option("continue"), option("expand_dates"), option("switch_source")],
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
    try:
        scene, retrieval = _views._agent_fetch_mapbox(bbox)
    except Exception as exc:
        return {"status": "error", "message": f"Mapbox 高清底图下载失败：{str(exc)[:200]}"}
    ctx["scene_id"] = scene.id
    ctx["file_name"] = scene.file_name
    ctx["slots"]["source"] = "mapbox"
    return {"status": "ok", "result": {"scene": scene_brief_payload(scene)}}


def _tool_compute_ndwi(ctx, args):
    args = _with_ctx_defaults(ctx, args)
    scene = _resolve_scene(ctx, args)
    if not scene:
        return {"status": "error", "message": "未找到影像，请先 search_sentinel_imagery 或 fetch_mapbox_imagery"}
    bbox = args.get("bbox") or ctx.get("bbox") or _scene_bbox(scene)
    if scene.source != "sentinel2":
        return {"status": "ok", "result": {"available": False, "reason": "NDWI 仅适用于 Sentinel-2 多光谱影像。"}}
    metadata = scene.metadata or {}
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
                "status": "ok",
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
                    logger.warning("NDWI 网格计算失败，保留其它网格结果: %s", str(exc)[:200])
                    continue
                if summary.get("available"):
                    summaries.append(summary)
        if summaries:
            sample_total = sum(int(s.get("sample_size_px") or 0) for s in summaries)
            water_ratio = sum(float(s.get("water_ratio") or 0) * int(s.get("sample_size_px") or 0) for s in summaries) / max(1, sample_total)
            mean_ndwi = sum(float(s.get("mean_ndwi") or 0) * int(s.get("sample_size_px") or 0) for s in summaries) / max(1, sample_total)
            expected_grid_count = len(jobs)
            min_grid_count = max(2, int(expected_grid_count * 0.5 + 0.999))
            if len(summaries) < min_grid_count:
                ndwi = {"available": False, "reason": f"仅 {len(summaries)}/{expected_grid_count} 个网格获得有效 NDWI 样本，低于最低覆盖要求。", "grid_count": len(summaries), "grid_expected_count": expected_grid_count}
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
        cand = type("Candidate", (), {"assets": metadata.get("assets") or {}, "product_id": "", "item_id": ""})()
        ndwi = _views.compute_ndwi_summary(cand, bbox, titiler, polygon=district_polygon)
    ctx["ndwi"] = ndwi
    return {"status": "ok", "result": ndwi}


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
    if output_quality.get("fallback_used") and not ctx.get("force_continue"):
        raise WaitingForUser(
            "视觉模型没有返回稳定的结构化解译结果。继续复核可能只是在总结限制条件，是否改用快速模式重试、切换高清底图或仍继续？",
            [option("retry_fast"), option("switch_source"), option("continue"), option("cancel")],
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


# ---------------- 注册表 ----------------

REGISTRY = {
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
        "description": "检索近期 Sentinel-2 L2A 公开影像并渲染。适合水体/植被/农业/宏观土地利用/时效变化。内含云量与时相质量门控，可能需要用户确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"},
                "date_start": {"type": "string", "description": "ISO 日期，可选"},
                "date_end": {"type": "string", "description": "ISO 日期，可选"},
                "max_cloud": {"type": "number", "description": "云量上限百分比，默认 60"},
            },
            "required": [],
        },
        "fn": _tool_search_sentinel_imagery,
    },
    "fetch_mapbox_imagery": {
        "name": "fetch_mapbox_imagery",
        "description": "下载 Mapbox 高清底图。适合建筑/道路/设施/小目标细节判读。",
        "parameters": {
            "type": "object",
            "properties": {"bbox": {"type": "object", "description": "可选；未提供则用已定位的 bbox"}},
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
}

# 统一的、可测试的工具定义视图；保留 REGISTRY 兼容现有调用方。
DEFINITIONS = definitions_from_registry(REGISTRY)

TOOL_SPECS = [
    {"name": v["name"], "description": v["description"], "parameters": v["parameters"]}
    for v in REGISTRY.values()
]
