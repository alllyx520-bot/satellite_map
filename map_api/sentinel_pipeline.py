"""Sentinel-2 检索/渲染/拼接/缓存管线(Phase 7 从 views.py 拆出,M-3)。

STAC 候选筛选、覆盖率贪心选景、TiTiler 渲染回退链、多景马赛克拼接、
no-data 裁边、场景与下载任务落库、按渲染参数哈希缓存。
views.py 通过导入保持同名再导出(含 SENTINEL_* 常量),
既有 patch("map_api.views.EarthSearchProvider.*") 为类级 patch,跨模块依然生效。
"""
import hashlib
import json
import os
import uuid
from io import BytesIO

import numpy as np
import requests
from PIL import Image

from .geo_math import (
    bbox_intersection_ratio, bbox_union_coverage_ratio,
    image_valid_ratio, crop_sentinel_nodata_border, sentinel_nodata_crop_too_large,
)
from .media_paths import SAVE_DIR, safe_media_path
from .models import DownloadTask, ImageryScene
from .payloads import scene_payload
from .utils.get_satellite_image import _download_progress

SENTINEL_FALLBACK_MSG = "近期公开影像源暂时不可用，可切回高清底图继续分析"
SENTINEL_DEFAULT_MIN_COVERAGE_RATIO = 0.92
SENTINEL_DEFAULT_MIN_VALID_IMAGE_RATIO = 0.88


def postprocess_cached_sentinel_scene(scene):
    metadata = dict(scene.metadata or {})
    crop_meta = metadata.get("no_data_crop") or {}
    if crop_meta.get("applied"):
        return scene
    image_path = safe_media_path(SAVE_DIR, scene.file_name, (".jpg", ".jpeg", ".png"))
    if not image_path or not os.path.exists(image_path):
        return scene
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    bbox = {
        "min_lng": scene.min_lng,
        "min_lat": scene.min_lat,
        "max_lng": scene.max_lng,
        "max_lat": scene.max_lat,
    }
    processed = crop_sentinel_nodata_border(image_bytes, bbox)
    if not processed["metadata"].get("applied"):
        metadata["no_data_crop"] = processed["metadata"]
        scene.metadata = metadata
        scene.save(update_fields=["metadata", "updated_at"])
        return scene
    with open(image_path, "wb") as f:
        f.write(processed["image_bytes"])
    effective = processed["bbox"]
    effective_plan = processed["plan"]
    metadata["no_data_crop"] = processed["metadata"]
    scene.min_lng = effective["min_lng"]
    scene.min_lat = effective["min_lat"]
    scene.max_lng = effective["max_lng"]
    scene.max_lat = effective["max_lat"]
    scene.gsd_m = round(effective_plan["gsd_m"], 2)
    scene.area_km2 = round(effective_plan["area_km2"], 4)
    scene.metadata = metadata
    scene.save(update_fields=[
        "min_lng", "min_lat", "max_lng", "max_lat",
        "gsd_m", "area_km2", "metadata", "updated_at",
    ])
    DownloadTask.objects.filter(scene=scene).update(
        min_lng=scene.min_lng,
        min_lat=scene.min_lat,
        max_lng=scene.max_lng,
        max_lat=scene.max_lat,
        gsd_m=scene.gsd_m,
        area_km2=scene.area_km2,
    )
    return scene


def compose_sentinel_mosaic(rendered_items, width, height, threshold=8):
    if not rendered_items:
        raise ValueError("没有可拼接的 Sentinel-2 渲染结果")
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    filled = np.zeros((height, width), dtype=bool)
    item_summaries = []
    for item in rendered_items:
        image = Image.open(BytesIO(item["image_bytes"])).convert("RGB")
        if image.size != (width, height):
            image = image.resize((width, height))
        arr = np.asarray(image, dtype=np.uint8)
        valid = arr.max(axis=2) > threshold
        fill = valid & ~filled
        canvas[fill] = arr[fill]
        filled |= fill
        candidate = item["candidate"]
        item_summaries.append({
            "product_id": candidate.product_id,
            "item_id": candidate.item_id,
            "coverage_ratio": item.get("coverage_ratio", 0),
            "valid_image_ratio": round(float(valid.sum()) / float(width * height), 4),
            "used_pixel_ratio": round(float(fill.sum()) / float(width * height), 4),
        })
    output = Image.fromarray(canvas, mode="RGB")
    buf = BytesIO()
    output.save(buf, "JPEG", quality=92)
    return buf.getvalue(), round(float(filled.sum()) / float(width * height), 4), item_summaries



def sentinel_cache_key(candidate, bbox, width, height):
    payload = {
        "source": "sentinel2",
        "collection": candidate.collection,
        "item_id": candidate.item_id,
        "visual_asset": (candidate.assets.get("visual") or {}).get("href", ""),
        "bbox": {key: round(float(bbox[key]), 6) for key in ("min_lng", "min_lat", "max_lng", "max_lat")},
        "width": int(width),
        "height": int(height),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sentinel_mosaic_cache_key(candidates, bbox, width, height):
    payload = {
        "source": "sentinel2_mosaic",
        "items": [
            {
                "collection": candidate.collection,
                "item_id": candidate.item_id,
                "visual_asset": (candidate.assets.get("visual") or {}).get("href", ""),
            }
            for candidate in candidates
        ],
        "bbox": {key: round(float(bbox[key]), 6) for key in ("min_lng", "min_lat", "max_lng", "max_lat")},
        "width": int(width),
        "height": int(height),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_cached_sentinel_scene(candidate, bbox, width, height):
    key = sentinel_cache_key(candidate, bbox, width, height)
    scenes = ImageryScene.objects.filter(
        source="sentinel2",
        product_id=candidate.product_id,
    ).order_by("-updated_at")
    for scene in scenes[:30]:
        metadata = scene.metadata or {}
        if metadata.get("sentinel_cache_key") != key:
            continue
        image_path = safe_media_path(SAVE_DIR, scene.file_name, (".jpg", ".jpeg", ".png"))
        if image_path and os.path.exists(image_path):
            scene = postprocess_cached_sentinel_scene(scene)
            if sentinel_nodata_crop_too_large((scene.metadata or {}).get("no_data_crop")):
                continue
            return scene
    return None


def find_cached_sentinel_mosaic_scene(candidates, bbox, width, height):
    key = sentinel_mosaic_cache_key(candidates, bbox, width, height)
    scenes = ImageryScene.objects.filter(source="sentinel2").order_by("-updated_at")
    for scene in scenes[:50]:
        metadata = scene.metadata or {}
        if metadata.get("sentinel_mosaic_cache_key") != key:
            continue
        image_path = safe_media_path(SAVE_DIR, scene.file_name, (".jpg", ".jpeg", ".png"))
        if image_path and os.path.exists(image_path):
            scene = postprocess_cached_sentinel_scene(scene)
            if sentinel_nodata_crop_too_large((scene.metadata or {}).get("no_data_crop")):
                continue
            return scene
    return None


def select_best_sentinel_candidate(candidates):
    if not candidates:
        return None
    return sorted_sentinel_candidates(candidates)[0]


def sorted_sentinel_candidates(candidates):
    return sorted(
        candidates or [],
        key=lambda c: (c.suitability_score or 0, c.acquired_at.timestamp() if c.acquired_at else 0),
        reverse=True,
    )


def greedy_cover_sentinel_candidates(candidates, bbox, max_count=6):
    selected = []
    current_coverage = 0
    remaining = list(candidates or [])
    while remaining and len(selected) < max_count:
        best = None
        best_coverage = current_coverage
        for candidate in remaining:
            coverage = bbox_union_coverage_ratio(bbox, [c.bbox for c in selected] + [candidate.bbox])
            if coverage > best_coverage or (
                coverage == best_coverage and best and (candidate.suitability_score or 0) > (best.suitability_score or 0)
            ):
                best = candidate
                best_coverage = coverage
        if not best or best_coverage <= current_coverage:
            break
        selected.append(best)
        remaining.remove(best)
        current_coverage = best_coverage
    return selected, current_coverage


def select_sentinel_scene_candidates(candidates, bbox, min_coverage=0.6, max_mosaic_candidates=6):
    ordered = sorted_sentinel_candidates(candidates)
    if not ordered:
        return [], 0, "no_candidate"

    for candidate in ordered:
        coverage = bbox_intersection_ratio(bbox, candidate.bbox)
        if coverage >= min_coverage:
            return [candidate], coverage, "single_scene"

    groups = {}
    for candidate in ordered:
        key = candidate.acquired_at.date().isoformat() if candidate.acquired_at else "unknown"
        groups.setdefault(key, []).append(candidate)

    best_group_selection = []
    best_group_coverage = 0
    for group in groups.values():
        selection, coverage = greedy_cover_sentinel_candidates(group, bbox, max_count=max_mosaic_candidates)
        if coverage > best_group_coverage:
            best_group_selection = selection
            best_group_coverage = coverage
        if coverage >= min_coverage and len(selection) > 1:
            return selection, coverage, "same_day_mosaic"

    selection, coverage = greedy_cover_sentinel_candidates(ordered, bbox, max_count=max_mosaic_candidates)
    if coverage >= min_coverage and len(selection) > 1:
        return selection, coverage, "multi_date_mosaic"
    if best_group_coverage > coverage:
        return best_group_selection, best_group_coverage, "coverage_insufficient"
    return selection, coverage, "coverage_insufficient"


def sentinel_response_data(scene, candidate, plan, resolution, cache_hit=False, candidate_count=1, retrieval=None):
    retrieval = retrieval or {}
    plan = retrieval.get("plan") or plan
    candidates = retrieval.get("selected_candidates") or []
    data = {
        "file_name": scene.file_name,
        "total_tiles": max(1, len(candidates) or int(retrieval.get("rendered_count") or 1)),
        "scene_id": scene.id,
        "scene": scene_payload(scene),
        "candidate": candidate.as_dict(),
        "candidate_count": candidate_count,
        "selection_score": candidate.suitability_score,
        "selection_reasons": candidate.score_reasons,
        "cache_hit": cache_hit,
        "gsd_m": scene.gsd_m,
        "area_km2": scene.area_km2,
        "resolution_px": resolution,
        "render_width": plan["total_w"],
        "render_height": plan["total_h"],
    }
    if candidates:
        data["candidates"] = [c.as_dict() for c in candidates]
    for key in (
        "mosaic",
        "target_coverage_ratio",
        "valid_image_ratio",
        "coverage_filtered_count",
        "render_errors",
        "selection_method",
        "no_data_crop",
    ):
        if key in retrieval:
            data[key] = retrieval[key]
    return data


def scene_from_candidate(
    file_name,
    candidate,
    bbox,
    area_km2,
    rendered_gsd_m=None,
    cache_key="",
    render_size=None,
    candidate_count=1,
    selection_rank=1,
    selection_method="single_scene",
    render_errors=None,
):
    rendered_gsd_m = rendered_gsd_m if rendered_gsd_m is not None else candidate.gsd_m
    metadata = {
        **candidate.metadata,
        "collection": candidate.collection,
        "item_id": candidate.item_id,
        "candidate_count": candidate_count,
        "selection_rank": selection_rank,
        "selection_method": selection_method,
        "suitability_score": candidate.suitability_score,
        "score_reasons": candidate.score_reasons,
        "candidate_bbox": candidate.bbox,
        "assets": candidate.assets,
        "links": candidate.links,
        "rendered_by": "titiler",
        "source_asset_gsd_m": candidate.gsd_m,
        "rendered_gsd_m": round(rendered_gsd_m, 2),
    }
    if cache_key:
        metadata["sentinel_cache_key"] = cache_key
    if render_size:
        metadata["render_size_px"] = render_size
    if render_errors:
        metadata["render_fallback_errors"] = render_errors
    return ImageryScene.objects.create(
        file_name=file_name,
        source="sentinel2",
        source_label="Sentinel-2 L2A",
        product_id=candidate.product_id,
        acquired_at=candidate.acquired_at,
        published_at=candidate.published_at,
        min_lng=bbox["min_lng"],
        min_lat=bbox["min_lat"],
        max_lng=bbox["max_lng"],
        max_lat=bbox["max_lat"],
        gsd_m=round(rendered_gsd_m, 2),
        area_km2=round(area_km2, 4),
        cloud_percent=candidate.cloud_percent,
        processing_level=candidate.processing_level,
        license_type=candidate.license_type,
        decision_grade=candidate.decision_grade,
        limitations=candidate.limitations,
        metadata=metadata,
    )


def scene_from_sentinel_mosaic(
    file_name,
    candidates,
    bbox,
    area_km2,
    rendered_gsd_m,
    cache_key,
    render_size,
    target_coverage_ratio,
    valid_image_ratio,
    render_errors=None,
    item_summaries=None,
):
    primary = candidates[0]
    clouds = [c.cloud_percent for c in candidates if c.cloud_percent is not None]
    cloud_percent = round(sum(clouds) / len(clouds), 2) if clouds else None
    acquired_values = [c.acquired_at for c in candidates if c.acquired_at]
    published_values = [c.published_at for c in candidates if c.published_at]
    score_values = [c.suitability_score for c in candidates if c.suitability_score is not None]
    metadata = {
        **primary.metadata,
        "collection": primary.collection,
        "item_id": primary.item_id,
        "candidate_count": len(candidates),
        "selection_rank": 1,
        "selection_method": "coverage_mosaic",
        "suitability_score": round(sum(score_values) / len(score_values), 1) if score_values else primary.suitability_score,
        "score_reasons": primary.score_reasons,
        "candidate_bbox": primary.bbox,
        "assets": primary.assets,
        "links": primary.links,
        "rendered_by": "titiler",
        "source_asset_gsd_m": primary.gsd_m,
        "rendered_gsd_m": round(rendered_gsd_m, 2),
        "render_size_px": render_size,
        "mosaic": True,
        "mosaic_candidate_count": len(candidates),
        "mosaic_candidates": [
            {
                "product_id": c.product_id,
                "item_id": c.item_id,
                "acquired_at": c.acquired_at.isoformat() if c.acquired_at else None,
                "cloud_percent": c.cloud_percent,
                "bbox": c.bbox,
                "coverage_ratio": bbox_intersection_ratio(bbox, c.bbox),
                "assets": c.assets,
            }
            for c in candidates
        ],
        "mosaic_items": item_summaries or [],
        "target_coverage_ratio": target_coverage_ratio,
        "valid_image_ratio": valid_image_ratio,
        "sentinel_mosaic_cache_key": cache_key,
    }
    if render_errors:
        metadata["render_fallback_errors"] = render_errors
    product_id = "MOSAIC:" + ",".join(c.product_id or c.item_id for c in candidates[:6])
    if len(product_id) > 255:
        product_id = product_id[:252] + "..."
    limitations = (
        primary.limitations
        + " 本图为多景 Sentinel-2 真彩色拼接结果，用于市域级近期态势筛查；"
        "不同景之间可能存在拍摄日期、云量和色彩差异，不等同于严格辐射一致的月度合成产品。"
    )
    return ImageryScene.objects.create(
        file_name=file_name,
        source="sentinel2",
        source_label="Sentinel-2 L2A 多景拼接",
        product_id=product_id,
        acquired_at=max(acquired_values) if acquired_values else primary.acquired_at,
        published_at=max(published_values) if published_values else primary.published_at,
        min_lng=bbox["min_lng"],
        min_lat=bbox["min_lat"],
        max_lng=bbox["max_lng"],
        max_lat=bbox["max_lat"],
        gsd_m=round(rendered_gsd_m, 2),
        area_km2=round(area_km2, 4),
        cloud_percent=cloud_percent,
        processing_level=primary.processing_level,
        license_type=primary.license_type,
        decision_grade=primary.decision_grade,
        limitations=limitations,
        metadata=metadata,
    )


def create_done_download_task(scene, file_name, bbox, plan, resolution, total=1):
    DownloadTask.objects.create(
        scene=scene,
        file_name=file_name,
        status="done",
        total=total,
        done=total,
        failed=0,
        min_lng=bbox["min_lng"],
        min_lat=bbox["min_lat"],
        max_lng=bbox["max_lng"],
        max_lat=bbox["max_lat"],
        gsd_m=round(plan["gsd_m"], 2),
        area_km2=round(plan["area_km2"], 4),
        resolution_px=resolution,
    )


def sentinel_retrieval_result(
    provider,
    candidates,
    bbox,
    plan,
    resolution,
    file_prefix="sentinel",
    min_coverage=None,
    min_valid_ratio=None,
    max_mosaic_candidates=None,
    allow_mosaic=True,
):
    if not candidates:
        return None
    min_coverage = min_coverage if min_coverage is not None else float(
        os.environ.get("SENTINEL_MIN_COVERAGE_RATIO", str(SENTINEL_DEFAULT_MIN_COVERAGE_RATIO))
    )
    min_valid_ratio = min_valid_ratio if min_valid_ratio is not None else float(
        os.environ.get("SENTINEL_MIN_VALID_IMAGE_RATIO", str(SENTINEL_DEFAULT_MIN_VALID_IMAGE_RATIO))
    )
    max_mosaic_candidates = max_mosaic_candidates if max_mosaic_candidates is not None else int(os.environ.get("SENTINEL_MAX_MOSAIC_CANDIDATES", "6"))
    selected, target_coverage, selection_method = select_sentinel_scene_candidates(
        candidates,
        bbox,
        min_coverage=min_coverage,
        max_mosaic_candidates=max_mosaic_candidates,
    )
    coverage_errors = []
    for candidate in sorted_sentinel_candidates(candidates):
        coverage = bbox_intersection_ratio(bbox, candidate.bbox)
        if coverage < min_coverage:
            coverage_errors.append(
                f"{candidate.product_id or candidate.item_id}: 覆盖率 {coverage:.1%} 低于 {min_coverage:.0%}"
            )
    if not selected or target_coverage < min_coverage:
        raise ValueError(
            f"Sentinel-2 候选覆盖不足：多景预计覆盖 {target_coverage:.1%}，低于 {min_coverage:.0%}；"
            "请缩小范围、扩大时间范围，或切换高清底图。"
        )
    if len(selected) > 1 and not allow_mosaic:
        raise ValueError("当前候选需要多景拼接，但此入口未启用拼接")

    candidate_count = len(candidates)
    render_errors = []
    if len(selected) == 1:
        single_candidates = [
            candidate for candidate in sorted_sentinel_candidates(candidates)
            if bbox_intersection_ratio(bbox, candidate.bbox) >= min_coverage
        ]
        for idx, candidate in enumerate(single_candidates, start=1):
            current_coverage = bbox_intersection_ratio(bbox, candidate.bbox)
            cached = find_cached_sentinel_scene(candidate, bbox, plan["total_w"], plan["total_h"])
            if cached:
                metadata = cached.metadata or {}
                return {
                    "scene": cached,
                    "candidate": candidate,
                    "selected_candidates": [candidate],
                    "plan": plan,
                    "candidate_count": candidate_count,
                    "cache_hit": True,
                    "mosaic": False,
                    "selection_method": selection_method,
                    "target_coverage_ratio": metadata.get("target_coverage_ratio") or current_coverage,
                    "valid_image_ratio": metadata.get("valid_image_ratio"),
                    "coverage_filtered_count": len(single_candidates),
                    "render_errors": metadata.get("render_fallback_errors") or [],
                }
            try:
                image_bytes = provider.render_candidate_jpeg(candidate, bbox, plan["total_w"], plan["total_h"])
            except (requests.RequestException, ValueError) as exc:
                render_errors.append(f"{candidate.product_id or candidate.item_id}: {str(exc)[:120]}")
                continue
            valid_ratio = image_valid_ratio(image_bytes)
            if valid_ratio < min_valid_ratio:
                render_errors.append(
                    f"{candidate.product_id or candidate.item_id}: 有效像素率 {valid_ratio:.1%} 低于 {min_valid_ratio:.0%}"
                )
                continue
            processed = crop_sentinel_nodata_border(image_bytes, bbox)
            image_bytes = processed["image_bytes"]
            effective_bbox = processed["bbox"]
            effective_plan = processed["plan"]
            crop_metadata = processed["metadata"]
            if sentinel_nodata_crop_too_large(crop_metadata):
                render_errors.append(
                    f"{candidate.product_id or candidate.item_id}: 边缘 no-data 占比 "
                    f"{float(crop_metadata.get('removed_pixel_ratio') or 0):.1%}，疑似覆盖不足或拼接不完整"
                )
                continue
            file_name = f"{file_prefix}_{uuid.uuid4().hex[:8]}.jpg"
            full_path = os.path.join(SAVE_DIR, file_name)
            with open(full_path, "wb") as f:
                f.write(image_bytes)
            scene = None
            try:
                scene = scene_from_candidate(
                    file_name,
                    candidate,
                    effective_bbox,
                    effective_plan["area_km2"],
                    rendered_gsd_m=effective_plan["gsd_m"],
                    cache_key=sentinel_cache_key(candidate, bbox, plan["total_w"], plan["total_h"]),
                render_size={"width": effective_plan["total_w"], "height": effective_plan["total_h"]},
                candidate_count=candidate_count,
                selection_rank=idx,
                selection_method=selection_method,
                render_errors=coverage_errors + render_errors,
            )
                metadata = dict(scene.metadata or {})
                metadata["requested_bbox"] = bbox
                metadata["target_coverage_ratio"] = current_coverage
                metadata["valid_image_ratio"] = valid_ratio
                metadata["no_data_crop"] = crop_metadata
                metadata["mosaic"] = False
                scene.metadata = metadata
                scene.save(update_fields=["metadata", "updated_at"])
                create_done_download_task(scene, file_name, effective_bbox, effective_plan, resolution, total=1)
            except Exception:
                if os.path.exists(full_path):
                    os.remove(full_path)
                if scene:
                    scene.delete()
                raise
            _download_progress[file_name] = {"total": 1, "done": 1, "failed": 0, "status": "done", "scene_id": scene.id}
            return {
                "scene": scene,
                "candidate": candidate,
                "selected_candidates": [candidate],
                "plan": effective_plan,
                "candidate_count": candidate_count,
                "cache_hit": False,
                "mosaic": False,
                "selection_method": selection_method,
                "target_coverage_ratio": current_coverage,
                "valid_image_ratio": valid_ratio,
                "no_data_crop": crop_metadata,
                "coverage_filtered_count": len(single_candidates),
                "render_errors": coverage_errors + render_errors,
            }
        if render_errors:
            raise ValueError("Sentinel-2 候选渲染失败或有效信息不足：" + "；".join(render_errors))
        raise ValueError("Sentinel-2 候选渲染后有效信息不足；请缩小范围、扩大时间范围，或切换高清底图。")

    cached = find_cached_sentinel_mosaic_scene(selected, bbox, plan["total_w"], plan["total_h"])
    if cached:
        metadata = cached.metadata or {}
        return {
            "scene": cached,
            "candidate": selected[0],
            "selected_candidates": selected,
            "plan": plan,
            "candidate_count": candidate_count,
            "cache_hit": True,
            "mosaic": True,
            "selection_method": metadata.get("selection_method") or selection_method,
            "target_coverage_ratio": metadata.get("target_coverage_ratio") or target_coverage,
            "valid_image_ratio": metadata.get("valid_image_ratio"),
            "coverage_filtered_count": len(selected),
            "render_errors": metadata.get("render_fallback_errors") or [],
        }

    rendered_items = []
    for candidate in selected:
        try:
            image_bytes = provider.render_candidate_jpeg(candidate, bbox, plan["total_w"], plan["total_h"])
        except (requests.RequestException, ValueError) as exc:
            render_errors.append(f"{candidate.product_id or candidate.item_id}: {str(exc)[:120]}")
            continue
        valid_ratio = image_valid_ratio(image_bytes)
        if valid_ratio <= 0:
            render_errors.append(f"{candidate.product_id or candidate.item_id}: 渲染结果无有效像素")
            continue
        rendered_items.append({
            "candidate": candidate,
            "image_bytes": image_bytes,
            "coverage_ratio": bbox_intersection_ratio(bbox, candidate.bbox),
        })
    if not rendered_items:
        raise ValueError("Sentinel-2 多景候选均无法渲染：" + "；".join(render_errors))
    mosaic_bytes, valid_ratio, item_summaries = compose_sentinel_mosaic(
        rendered_items,
        plan["total_w"],
        plan["total_h"],
    )
    if valid_ratio < min_valid_ratio:
        raise ValueError(
            f"Sentinel-2 多景拼接后有效像素率 {valid_ratio:.1%}，低于 {min_valid_ratio:.0%}；"
            "请缩小范围、扩大时间范围，或切换高清底图。"
        )
    processed = crop_sentinel_nodata_border(mosaic_bytes, bbox)
    mosaic_bytes = processed["image_bytes"]
    effective_bbox = processed["bbox"]
    effective_plan = processed["plan"]
    crop_metadata = processed["metadata"]
    if sentinel_nodata_crop_too_large(crop_metadata):
        raise ValueError(
            f"Sentinel-2 多景拼接后仍存在大面积 no-data 边缘 "
            f"({float(crop_metadata.get('removed_pixel_ratio') or 0):.1%})；"
            "当前候选没有完整覆盖框选范围，请缩小范围、扩大时间范围，或切换高清底图。"
        )
    used_candidates = [item["candidate"] for item in rendered_items]
    used_coverage = bbox_union_coverage_ratio(bbox, [candidate.bbox for candidate in used_candidates])
    if used_coverage < min_coverage:
        raise ValueError(
            f"Sentinel-2 可渲染多景覆盖 {used_coverage:.1%}，低于 {min_coverage:.0%}；"
            "请缩小范围、扩大时间范围，或切换高清底图。"
        )
    file_name = f"{file_prefix}_mosaic_{uuid.uuid4().hex[:8]}.jpg"
    full_path = os.path.join(SAVE_DIR, file_name)
    with open(full_path, "wb") as f:
        f.write(mosaic_bytes)
    scene = None
    try:
        scene = scene_from_sentinel_mosaic(
            file_name,
            used_candidates,
            effective_bbox,
            effective_plan["area_km2"],
            rendered_gsd_m=effective_plan["gsd_m"],
            cache_key=sentinel_mosaic_cache_key(used_candidates, bbox, plan["total_w"], plan["total_h"]),
            render_size={"width": effective_plan["total_w"], "height": effective_plan["total_h"]},
            target_coverage_ratio=used_coverage,
            valid_image_ratio=valid_ratio,
            render_errors=coverage_errors + render_errors,
            item_summaries=item_summaries,
        )
        metadata = dict(scene.metadata or {})
        metadata["requested_bbox"] = bbox
        metadata["no_data_crop"] = crop_metadata
        scene.metadata = metadata
        scene.save(update_fields=["metadata", "updated_at"])
        create_done_download_task(scene, file_name, effective_bbox, effective_plan, resolution, total=len(used_candidates))
    except Exception:
        if os.path.exists(full_path):
            os.remove(full_path)
        if scene:
            scene.delete()
        raise
    _download_progress[file_name] = {
        "total": len(used_candidates),
        "done": len(used_candidates),
        "failed": 0,
        "status": "done",
        "scene_id": scene.id,
    }
    return {
        "scene": scene,
        "candidate": used_candidates[0],
        "selected_candidates": used_candidates,
        "plan": effective_plan,
        "candidate_count": candidate_count,
        "cache_hit": False,
        "mosaic": True,
        "selection_method": selection_method,
        "target_coverage_ratio": used_coverage,
        "valid_image_ratio": valid_ratio,
        "no_data_crop": crop_metadata,
        "coverage_filtered_count": len(used_candidates),
        "render_errors": coverage_errors + render_errors,
    }
