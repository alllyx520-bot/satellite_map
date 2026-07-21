"""几何与影像测量纯函数(Phase 7 从 views.py 拆出,M-1)。

无 Django 依赖:bbox 归一化/相交/覆盖、影像计划与 GSD、
Sentinel no-data 黑边裁剪等。views.py 通过导入保持同名再导出,
既有 `patch("map_api.views.X")` 与 `from map_api.views import X` 全部不受影响。
"""
import math
from io import BytesIO

import numpy as np
from PIL import Image

# Sentinel 自动裁边允许的最大去边比例,超过视为渲染质量不足
SENTINEL_MAX_AUTO_CROP_RATIO = 0.03


def normalize_bbox(data):
    raw_min_lng = float(data['min_lng'])
    raw_min_lat = float(data['min_lat'])
    raw_max_lng = float(data['max_lng'])
    raw_max_lat = float(data['max_lat'])
    min_lng, max_lng = sorted((raw_min_lng, raw_max_lng))
    min_lat, max_lat = sorted((raw_min_lat, raw_max_lat))
    if max_lng - min_lng <= 0 or max_lat - min_lat <= 0:
        raise ValueError("框选区域过小，请重新选择")
    return min_lng, min_lat, max_lng, max_lat


def compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution):
    lon_span = max_lng - min_lng
    lat_span = max_lat - min_lat
    center_lat_rad = math.radians((min_lat + max_lat) / 2)
    target = min(4096, max(1, resolution))
    aspect_ratio = (lon_span * math.cos(center_lat_rad)) / lat_span if lat_span else 1
    if aspect_ratio >= 1:
        total_w = target
        total_h = max(1, int(target / aspect_ratio))
    else:
        total_h = target
        total_w = max(1, int(target * aspect_ratio))
    total_tiles = 1 if total_w <= 1280 and total_h <= 1280 else math.ceil(total_w / 1280) * math.ceil(total_h / 1280)
    gsd_lon = (lon_span * 111320 * math.cos(center_lat_rad)) / total_w
    gsd_lat_val = (lat_span * 110574) / total_h
    area_km2 = (lon_span * 111320 * math.cos(center_lat_rad)) * (lat_span * 110574) / 1e6
    return {
        "total_w": total_w,
        "total_h": total_h,
        "total_tiles": total_tiles,
        "gsd_m": (gsd_lon + gsd_lat_val) / 2,
        "area_km2": area_km2,
    }


def bbox_area_deg(bbox):
    if not bbox:
        return 0
    width = max(0, float(bbox.get("max_lng", 0)) - float(bbox.get("min_lng", 0)))
    height = max(0, float(bbox.get("max_lat", 0)) - float(bbox.get("min_lat", 0)))
    return width * height


def bbox_intersection_ratio(target_bbox, candidate_bbox):
    if not target_bbox or not candidate_bbox:
        return 0
    inter = bbox_intersection(target_bbox, candidate_bbox)
    target_area = bbox_area_deg(target_bbox)
    if target_area <= 0:
        return 0
    return round(bbox_area_deg(inter) / target_area, 4)


def bbox_intersection(target_bbox, candidate_bbox):
    if not target_bbox or not candidate_bbox:
        return None
    inter = {
        "min_lng": max(float(target_bbox["min_lng"]), float(candidate_bbox["min_lng"])),
        "min_lat": max(float(target_bbox["min_lat"]), float(candidate_bbox["min_lat"])),
        "max_lng": min(float(target_bbox["max_lng"]), float(candidate_bbox["max_lng"])),
        "max_lat": min(float(target_bbox["max_lat"]), float(candidate_bbox["max_lat"])),
    }
    if inter["max_lng"] <= inter["min_lng"] or inter["max_lat"] <= inter["min_lat"]:
        return None
    return inter


def bbox_union_coverage_ratio(target_bbox, candidate_bboxes):
    target_area = bbox_area_deg(target_bbox)
    if target_area <= 0:
        return 0
    rects = []
    for candidate_bbox in candidate_bboxes or []:
        inter = bbox_intersection(target_bbox, candidate_bbox)
        if inter:
            rects.append(inter)
    if not rects:
        return 0

    xs = sorted({float(target_bbox["min_lng"]), float(target_bbox["max_lng"]), *[r["min_lng"] for r in rects], *[r["max_lng"] for r in rects]})
    ys = sorted({float(target_bbox["min_lat"]), float(target_bbox["max_lat"]), *[r["min_lat"] for r in rects], *[r["max_lat"] for r in rects]})
    covered_area = 0
    for xi in range(len(xs) - 1):
        x1, x2 = xs[xi], xs[xi + 1]
        if x2 <= x1:
            continue
        cx = (x1 + x2) / 2
        for yi in range(len(ys) - 1):
            y1, y2 = ys[yi], ys[yi + 1]
            if y2 <= y1:
                continue
            cy = (y1 + y2) / 2
            if any(r["min_lng"] <= cx <= r["max_lng"] and r["min_lat"] <= cy <= r["max_lat"] for r in rects):
                covered_area += (x2 - x1) * (y2 - y1)
    return round(covered_area / target_area, 4)


def image_valid_ratio(image_bytes, threshold=8):
    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return 0
    total = image.width * image.height
    if total <= 0:
        return 0
    arr = np.asarray(image, dtype=np.uint8)
    valid = int((arr.max(axis=2) > threshold).sum())
    return round(valid / total, 4)


def image_plan_for_bbox_and_size(bbox, width, height):
    min_lng = float(bbox["min_lng"])
    min_lat = float(bbox["min_lat"])
    max_lng = float(bbox["max_lng"])
    max_lat = float(bbox["max_lat"])
    width = max(1, int(width))
    height = max(1, int(height))
    lon_span = max_lng - min_lng
    lat_span = max_lat - min_lat
    center_lat_rad = math.radians((min_lat + max_lat) / 2)
    gsd_lon = (lon_span * 111320 * math.cos(center_lat_rad)) / width if width else 0
    gsd_lat_val = (lat_span * 110574) / height if height else 0
    area_km2 = (lon_span * 111320 * math.cos(center_lat_rad)) * (lat_span * 110574) / 1e6
    return {
        "total_w": width,
        "total_h": height,
        "total_tiles": 1,
        "gsd_m": (gsd_lon + gsd_lat_val) / 2,
        "area_km2": area_km2,
    }


def bbox_for_image_crop(bbox, crop_box, original_size):
    left, top, right, bottom = crop_box
    width, height = original_size
    min_lng = float(bbox["min_lng"])
    min_lat = float(bbox["min_lat"])
    max_lng = float(bbox["max_lng"])
    max_lat = float(bbox["max_lat"])
    lon_span = max_lng - min_lng
    lat_span = max_lat - min_lat
    return {
        "min_lng": min_lng + lon_span * (left / width),
        "max_lng": min_lng + lon_span * (right / width),
        "max_lat": max_lat - lat_span * (top / height),
        "min_lat": max_lat - lat_span * (bottom / height),
    }


def crop_sentinel_nodata_border(image_bytes, bbox, threshold=8, min_removed_ratio=0.015):
    """去掉 titiler/Sentinel 渲染结果四周的 no-data 黑边，并返回裁剪后的地理 bbox。"""
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    arr = np.asarray(image, dtype=np.uint8)
    mask = arr.max(axis=2) > threshold
    total = width * height
    valid_pixels = int(mask.sum())
    valid_ratio = round(valid_pixels / total, 4) if total else 0
    metadata = {
        "applied": False,
        "threshold": threshold,
        "valid_image_ratio_before_crop": valid_ratio,
        "original_size_px": {"width": width, "height": height},
    }
    if valid_pixels <= 0:
        return {
            "image_bytes": image_bytes,
            "bbox": bbox,
            "plan": image_plan_for_bbox_and_size(bbox, width, height),
            "metadata": metadata,
        }

    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    left = int(cols[0])
    right = int(cols[-1]) + 1
    top = int(rows[0])
    bottom = int(rows[-1]) + 1
    crop_area = (right - left) * (bottom - top)
    removed_ratio = round(1 - (crop_area / total), 4) if total else 0
    metadata["removed_pixel_ratio"] = removed_ratio

    if (left, top, right, bottom) == (0, 0, width, height) or removed_ratio < min_removed_ratio:
        metadata["cropped_size_px"] = {"width": width, "height": height}
        return {
            "image_bytes": image_bytes,
            "bbox": bbox,
            "plan": image_plan_for_bbox_and_size(bbox, width, height),
            "metadata": metadata,
        }

    cropped = image.crop((left, top, right, bottom))
    buf = BytesIO()
    cropped.save(buf, "JPEG", quality=92)
    cropped_bytes = buf.getvalue()
    cropped_bbox = bbox_for_image_crop(bbox, (left, top, right, bottom), (width, height))
    cropped_plan = image_plan_for_bbox_and_size(cropped_bbox, cropped.width, cropped.height)
    metadata.update({
        "applied": True,
        "crop_box_px": {"left": left, "top": top, "right": right, "bottom": bottom},
        "cropped_size_px": {"width": cropped.width, "height": cropped.height},
        "effective_bbox": cropped_bbox,
        "valid_image_ratio_after_crop": image_valid_ratio(cropped_bytes, threshold=threshold),
    })
    return {
        "image_bytes": cropped_bytes,
        "bbox": cropped_bbox,
        "plan": cropped_plan,
        "metadata": metadata,
    }


def sentinel_nodata_crop_too_large(crop_metadata, max_removed_ratio=None):
    if not crop_metadata or not crop_metadata.get("applied"):
        return False
    max_removed_ratio = (
        SENTINEL_MAX_AUTO_CROP_RATIO
        if max_removed_ratio is None
        else float(max_removed_ratio)
    )
    return float(crop_metadata.get("removed_pixel_ratio") or 0) > max_removed_ratio
