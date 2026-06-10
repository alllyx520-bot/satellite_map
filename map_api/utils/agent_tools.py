import calendar
import json
import os
import re
from datetime import date
from io import BytesIO

import numpy as np
import requests
from PIL import Image

from .analysis_strategy import build_analysis_strategy


AGENT_MODEL = "deepseek-v4-flash"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
NDWI_THRESHOLD = 0.1
NDWI_MIN_VALID_PIXELS = 1024
ZH_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}
TASK_ALIASES = {
    "water_body_monitoring": "water",
    "water_monitoring": "water",
    "waterbody": "water",
    "water_body": "water",
    "water": "water",
    "vegetation_monitoring": "vegetation",
    "vegetation": "vegetation",
    "agriculture_monitoring": "agriculture",
    "crop_monitoring": "agriculture",
    "agriculture": "agriculture",
    "built_up_area": "built_up",
    "urban": "built_up",
    "built_up": "built_up",
    "small_targets": "small_target",
    "small_target": "small_target",
    "landcover": "land_use",
    "land_cover": "land_use",
    "land_use": "land_use",
}
SEASONS = {
    "春季": (3, 5),
    "春天": (3, 5),
    "夏季": (6, 8),
    "夏天": (6, 8),
    "秋季": (9, 11),
    "秋天": (9, 11),
    "冬季": (12, 2),
    "冬天": (12, 2),
}


def deepseek_headers():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少 DEEPSEEK_API_KEY，请先在 .env 中配置")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def call_deepseek(messages, response_format=None, timeout=45):
    payload = {
        "model": os.environ.get("AGENT_MODEL", AGENT_MODEL),
        "messages": messages,
        "temperature": 0.1,
    }
    if response_format:
        payload["response_format"] = response_format
    response = requests.post(
        os.environ.get("DEEPSEEK_CHAT_URL", DEEPSEEK_CHAT_URL),
        headers=deepseek_headers(),
        json=payload,
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def call_deepseek_json(messages, timeout=45):
    text = call_deepseek(messages, response_format={"type": "json_object"}, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("DeepSeek 未返回有效 JSON") from exc


def _date_range_for_month(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last_day).isoformat()


def _date_range_for_season(year, start_month, end_month):
    if start_month <= end_month:
        start_year = end_year = year
    else:
        start_year = year
        end_year = year + 1
    end_day = calendar.monthrange(end_year, end_month)[1]
    return date(start_year, start_month, 1).isoformat(), date(end_year, end_month, end_day).isoformat()


def deterministic_extract_slots(goal, today=None):
    text = goal or ""
    slots = {}
    today = today or date.today()

    month_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
    zh_month_match = None
    if not month_match:
        month_match = re.search(r"(\d{4})[-/](\d{1,2})(?![-/\d])", text)
    if not month_match:
        zh_month_match = re.search(r"(\d{4})\s*年\s*(十二|十一|十|九|八|七|六|五|四|三|二|一)\s*月", text)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        if 1 <= month <= 12:
            slots["date_start"], slots["date_end"] = _date_range_for_month(year, month)
            slots["time_granularity"] = "month"
    elif zh_month_match:
        year = int(zh_month_match.group(1))
        month = ZH_MONTHS[zh_month_match.group(2)]
        slots["date_start"], slots["date_end"] = _date_range_for_month(year, month)
        slots["time_granularity"] = "month"
    else:
        year = None
        if "今年" in text:
            year = today.year
        elif "去年" in text:
            year = today.year - 1
        else:
            year_match = re.search(r"(\d{4})\s*年", text)
            if year_match:
                year = int(year_match.group(1))
        if year:
            for season, (start_month, end_month) in SEASONS.items():
                if season in text:
                    slots["date_start"], slots["date_end"] = _date_range_for_season(year, start_month, end_month)
                    slots["time_granularity"] = "season"
                    break

    place_match = re.search(r"([\u4e00-\u9fa5]{2,20}?(?:市|县|区|州|盟|旗))", text)
    if not place_match:
        place_match = re.search(r"([\u4e00-\u9fa5]{2,20})", text)
    if place_match:
        place = place_match.group(1)
        for prefix in ("帮我调查", "调查", "分析", "看看", "请"):
            if place.startswith(prefix):
                place = place[len(prefix):]
        slots["place_name"] = place

    lower_task = text.lower()
    if any(word in text for word in ("水体", "水域", "河流", "湖泊", "水库", "岸线", "湿地")):
        slots["task"] = "water"
    elif any(word in text for word in ("植被", "绿地", "森林", "生态")):
        slots["task"] = "vegetation"
    elif any(word in text for word in ("农田", "农业", "耕地", "作物", "长势")):
        slots["task"] = "agriculture"
    elif any(word in text for word in ("车辆", "停车", "船舶", "飞机", "小目标")):
        slots["task"] = "small_target"
    elif any(word in text for word in ("建筑", "道路", "城市", "建设用地")):
        slots["task"] = "built_up"
    else:
        slots["task"] = "land_use"

    if slots["task"] in ("water", "vegetation", "agriculture", "land_use"):
        slots["source"] = "sentinel2"
    if slots["task"] in ("small_target", "built_up"):
        slots["source"] = "mapbox"
    if any(word in text for word in ("近期", "最新", "现在", "当前", "变化", "新增", "扩张", "退化")):
        slots["needs_timeliness"] = True
        if slots["task"] != "small_target":
            slots["source"] = "sentinel2"
    if "flash" in lower_task or "快速" in text:
        slots["mode"] = "fast"

    return slots


def normalize_agent_task(value):
    task = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return TASK_ALIASES.get(task, task)


def merge_agent_slots(model_slots, goal, defaults=None, today=None):
    merged = {}
    if defaults:
        merged.update(defaults)
    if isinstance(model_slots, dict):
        for key in (
            "place_name",
            "date_start",
            "date_end",
            "time_granularity",
            "task",
            "source",
            "needs_timeliness",
            "mode",
        ):
            value = model_slots.get(key)
            if value not in (None, ""):
                merged[key] = value
    rule_slots = deterministic_extract_slots(goal, today=today)
    for key, value in rule_slots.items():
        if key in ("date_start", "date_end", "time_granularity") and ("今年" in (goal or "") or "去年" in (goal or "")):
            merged[key] = value
        elif merged.get(key) in (None, ""):
            merged[key] = value

    merged["task"] = normalize_agent_task(merged.get("task"))
    if merged["task"] not in ("water", "vegetation", "agriculture", "small_target", "built_up", "land_use"):
        merged["task"] = rule_slots.get("task") or "land_use"
    if merged.get("source") == "earth_search":
        merged["source"] = "sentinel2"
    if merged.get("source") not in ("sentinel2", "mapbox"):
        task = merged.get("task")
        merged["source"] = "sentinel2" if task in ("water", "vegetation", "agriculture", "land_use") else "mapbox"
    if merged.get("mode") not in ("fast", "precise"):
        merged["mode"] = "precise"
    return merged


def build_agent_plan(goal, mode="precise", today=None):
    messages = [
        {
            "role": "system",
            "content": (
                "你是遥感智能调查 Agent 的任务规划器。"
                "只返回 JSON，不要解释。字段包含 place_name,date_start,date_end,"
                "time_granularity,task,source,needs_timeliness,mode。"
                "source 只能是 sentinel2 或 mapbox；mode 只能是 precise 或 fast。"
            ),
        },
        {"role": "user", "content": goal},
    ]
    model_slots = call_deepseek_json(messages)
    slots = merge_agent_slots(model_slots, goal, defaults={"mode": mode}, today=today)
    task_strategy = build_analysis_strategy(goal, scene=type("Scene", (), {"source": slots["source"], "gsd_m": 10 if slots["source"] == "sentinel2" else 1.2})())
    return {
        "slots": slots,
        "steps": [step for step in [
            {"id": "understand", "label": "理解调查目标"},
            {"id": "locate", "label": "定位调查范围"},
            {"id": "select_source", "label": "选择图像源"},
            {"id": "retrieve_imagery", "label": "检索并生成影像"},
            {"id": "quality_check", "label": "检查影像质量"},
            {"id": "ndwi", "label": "轻量 NDWI 水体量化"} if slots.get("task") == "water" else None,
            {"id": "vl_analysis", "label": "视觉模型解译"},
            {"id": "review", "label": "DeepSeek 结论复核"},
            {"id": "complete", "label": "整理结果"},
        ] if step],
        "task_strategy": task_strategy,
    }


def parse_amap_boundary(polyline):
    points = []
    for pair in re.split(r"[;|]", polyline or ""):
        raw = pair.split(",")
        if len(raw) != 2:
            continue
        try:
            lng = float(raw[0])
            lat = float(raw[1])
        except ValueError:
            continue
        points.append((lng, lat))
    if not points:
        raise ValueError("行政区边界为空，无法生成 bbox")
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return {
        "min_lng": min(lngs),
        "min_lat": min(lats),
        "max_lng": max(lngs),
        "max_lat": max(lats),
    }


def resolve_district_bbox(place_name, timeout=12):
    api_key = os.environ.get("AMAP_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少 AMAP_KEY，无法自动解析行政区范围")
    response = requests.get(
        "https://restapi.amap.com/v3/config/district",
        params={
            "key": api_key,
            "keywords": place_name,
            "subdistrict": 0,
            "extensions": "all",
        },
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    data = response.json()
    districts = data.get("districts") or []
    if not districts:
        raise ValueError(f"未找到行政区：{place_name}")
    first = districts[0]
    bbox = parse_amap_boundary(first.get("polyline", ""))
    return {
        "name": first.get("name") or place_name,
        "adcode": first.get("adcode", ""),
        "level": first.get("level", ""),
        "bbox": bbox,
        "candidate_count": len(districts),
        "bbox_policy": "行政区 bbox 筛查，不做精确行政边界裁剪",
    }


def fetch_cog_bbox_array(asset_url, bbox, titiler_endpoint, size=256, timeout=60):
    endpoint = (
        f"{(titiler_endpoint or 'https://titiler.xyz').rstrip('/')}/cog/bbox/"
        f"{bbox['min_lng']},{bbox['min_lat']},{bbox['max_lng']},{bbox['max_lat']}/"
        f"{size}x{size}.tif"
    )
    response = requests.get(
        endpoint,
        params={"url": asset_url, "rescale": "0,10000"},
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    response.raise_for_status()
    image = Image.open(BytesIO(response.content))
    arr = np.asarray(image.convert("F"), dtype=np.float32)
    return arr


def compute_ndwi_from_arrays(green, nir, threshold=NDWI_THRESHOLD, min_valid_pixels=NDWI_MIN_VALID_PIXELS):
    green = np.asarray(green, dtype=np.float32)
    nir = np.asarray(nir, dtype=np.float32)
    if green.shape != nir.shape:
        raise ValueError("green 与 nir 数组尺寸不一致")
    denom = green + nir
    valid = np.isfinite(green) & np.isfinite(nir) & (denom > 1e-6)
    if not np.any(valid):
        raise ValueError("NDWI 有效像元为空")
    valid_count = int(valid.sum())
    if valid_count < min_valid_pixels:
        raise ValueError(f"NDWI 有效像元过少（{valid_count}），疑似影像覆盖不足或 NoData 过多")
    ndwi = np.zeros_like(green, dtype=np.float32)
    ndwi[valid] = (green[valid] - nir[valid]) / denom[valid]
    water = valid & (ndwi > threshold)
    ratio = float(water.sum() / valid.sum())
    values = ndwi[valid]
    return {
        "available": True,
        "method": "NDWI=(Green-NIR)/(Green+NIR)",
        "threshold": threshold,
        "water_ratio": round(ratio, 4),
        "water_percent": round(ratio * 100, 1),
        "mean_ndwi": round(float(values.mean()), 4),
        "max_ndwi": round(float(values.max()), 4),
        "sample_size_px": valid_count,
        "limitations": "轻量 NDWI 仅用于 bbox 内水体线索筛查，未做云掩膜、阴影剔除、行政边界精确裁剪或多景合成。",
    }


def compute_ndwi_summary(candidate, bbox, titiler_endpoint=None, threshold=NDWI_THRESHOLD):
    assets = candidate.assets or {}
    green_url = (assets.get("green") or {}).get("href")
    nir_url = (assets.get("nir") or {}).get("href")
    if not green_url or not nir_url:
        return {
            "available": False,
            "method": "NDWI=(Green-NIR)/(Green+NIR)",
            "reason": "候选影像缺少 green 或 nir 资产，无法计算 NDWI。",
            "limitations": "仍可使用真彩色影像做视觉解译，但水体比例不提供量化结果。",
        }
    try:
        green = fetch_cog_bbox_array(green_url, bbox, titiler_endpoint)
        nir = fetch_cog_bbox_array(nir_url, bbox, titiler_endpoint)
        return compute_ndwi_from_arrays(green, nir, threshold=threshold)
    except Exception as exc:
        return {
            "available": False,
            "method": "NDWI=(Green-NIR)/(Green+NIR)",
            "reason": str(exc)[:180],
            "limitations": "NDWI 计算失败时仅保留视觉解译结论，不输出水体比例量化。",
        }


def compute_ndwi_mosaic_summary(candidates, bbox, titiler_endpoint=None, threshold=NDWI_THRESHOLD):
    summaries = []
    weighted_water = 0
    weighted_mean = 0
    weighted_max = None
    sample_total = 0
    failed = []
    for candidate in candidates or []:
        summary = compute_ndwi_summary(candidate, bbox, titiler_endpoint=titiler_endpoint, threshold=threshold)
        product_id = getattr(candidate, "product_id", "") or getattr(candidate, "item_id", "")
        if summary.get("available"):
            sample_size = int(summary.get("sample_size_px") or 0)
            if sample_size <= 0:
                failed.append({"product_id": product_id, "reason": "NDWI 缺少有效样本数"})
                continue
            summaries.append({"product_id": product_id, **summary})
            weighted_water += float(summary.get("water_ratio") or 0) * sample_size
            weighted_mean += float(summary.get("mean_ndwi") or 0) * sample_size
            sample_total += sample_size
            max_ndwi = summary.get("max_ndwi")
            if max_ndwi is not None:
                weighted_max = max(float(max_ndwi), weighted_max) if weighted_max is not None else float(max_ndwi)
        else:
            failed.append({"product_id": product_id, "reason": summary.get("reason") or "NDWI 不可用"})
    if sample_total <= 0:
        return {
            "available": False,
            "method": "NDWI=(Green-NIR)/(Green+NIR)",
            "reason": "多景候选均未能提供可用 NDWI 样本。",
            "failed_candidates": failed,
            "limitations": "NDWI 计算失败时仅保留视觉解译结论，不输出水体比例量化。",
        }
    water_ratio = weighted_water / sample_total
    return {
        "available": True,
        "method": "NDWI=(Green-NIR)/(Green+NIR)",
        "aggregation": "按每景有效像元数加权汇总",
        "threshold": threshold,
        "water_ratio": round(water_ratio, 4),
        "water_percent": round(water_ratio * 100, 1),
        "mean_ndwi": round(weighted_mean / sample_total, 4),
        "max_ndwi": round(weighted_max, 4) if weighted_max is not None else None,
        "sample_size_px": sample_total,
        "candidate_count": len(summaries),
        "candidate_summaries": summaries,
        "failed_candidates": failed,
        "limitations": "多景 NDWI 为 bbox 筛查级加权结果，未做云掩膜、阴影剔除、辐射一致化、严格镶嵌或行政边界精确裁剪。",
    }
