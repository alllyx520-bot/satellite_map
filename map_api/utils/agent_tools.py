import calendar
import json
import os
import re
import time
import base64
from datetime import date, timedelta
from io import BytesIO

import numpy as np
import requests
from PIL import Image

from .analysis_strategy import build_analysis_strategy
from ..remote_sensing_indices import ndwi as spectral_ndwi, summarize as summarize_index


AGENT_MODEL = "glm-5.3-flash"
GLM_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# 保留旧常量名/函数名作为兼容层，实际默认已切换到 GLM。
DEEPSEEK_CHAT_URL = GLM_CHAT_URL
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


def request_proxies():
    """默认遵循当前进程代理；仅显式要求时才强制直连。"""
    direct = os.environ.get("SATELLITESENSE_DIRECT_HTTP", "").strip().lower()
    return {"http": None, "https": None} if direct in {"1", "true", "yes"} else None


def glm_headers():
    api_key = os.environ.get("GLM_API_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少 GLM_API_KEY（兼容旧配置名 DEEPSEEK_API_KEY），请先在 .env 中配置")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def call_glm(messages, response_format=None, timeout=45):
    payload = {
        "model": os.environ.get("AGENT_MODEL", AGENT_MODEL),
        "messages": messages,
        "temperature": 1,
        "top_p": 0.95,
        "reasoning_effort": "max",
        "thinking": {"type": "enabled", "clear_thinking": False},
    }
    if response_format:
        payload["response_format"] = response_format
    url = os.environ.get("GLM_CHAT_URL", os.environ.get("DEEPSEEK_CHAT_URL", GLM_CHAT_URL))
    headers = glm_headers()
    # 只对网络瞬断、超时和服务端过载重试一次；鉴权/请求参数错误立即失败，
    # 避免把确定性错误放大成重复计费或更长等待。
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
                proxies=request_proxies(),
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
        else:
            if response.status_code not in (429, 500, 502, 503, 504):
                # 401/403/4xx 参数错误不重试，保留原始 HTTPError 给上层。
                response.raise_for_status()
                data = response.json()
                message = (data.get("choices") or [{}])[0].get("message") or {}
                content = message.get("content")
                # GLM 在开启 thinking/多模态时可能把 content 返回为块数组；
                # 只提取公开回答文本，绝不把 reasoning_content/hidden thinking 暴露给上层。
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, str):
                            text_parts.append(part)
                        elif isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                            value = part.get("text") or part.get("content")
                            if isinstance(value, str):
                                text_parts.append(value)
                    content = "\n".join(text_parts)
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("GLM 响应缺少公开 content")
                return content
            last_error = requests.HTTPError(f"GLM HTTP {response.status_code}", response=response)
        if attempt == 0:
            time.sleep(0.35)
    if last_error:
        raise last_error
    raise RuntimeError("GLM 请求失败")


def call_glm_json(messages, image_urls=None, timeout=45):
    """GLM-5.3-Flash JSON 调用，支持文本和 image_url 内容块。"""
    enriched = []
    pending_images = list(image_urls or [])[:3]
    for message in messages:
        item = dict(message)
        if pending_images and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                parts = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                parts = list(content)
            else:
                parts = []
            parts.extend({"type": "image_url", "image_url": {"url": url}} for url in pending_images)
            item["content"] = parts
            pending_images = []
        enriched.append(item)
    text = call_glm(enriched, response_format={"type": "json_object"}, timeout=timeout)
    return _parse_json_response(text)


def _parse_json_response(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError("GLM 未返回有效 JSON") from exc


# 兼容旧调用方；新代码统一使用 GLM 命名，避免把实际 provider 误标成 DeepSeek。
def deepseek_headers():
    return glm_headers()


def call_deepseek(messages, response_format=None, timeout=45):
    return call_glm(messages, response_format=response_format, timeout=timeout)


def call_deepseek_json(messages, timeout=45):
    # 旧测试/集成方可能 patch call_deepseek；保留该拦截点。
    return _parse_json_response(call_deepseek(messages, response_format={"type": "json_object"}, timeout=timeout))


def image_file_to_data_url(path, max_size=1536):
    """校验并压缩本地影像，生成可供 BigModel 读取的 data URL。"""
    from PIL import Image, ImageOps
    with Image.open(path) as probe:
        probe.verify()
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        out = BytesIO()
        image.save(out, format="JPEG", quality=86, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


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
    today = today or date.today()
    rule_slots = deterministic_extract_slots(goal, today=today)
    for key, value in rule_slots.items():
        if key in ("date_start", "date_end", "time_granularity") and ("今年" in (goal or "") or "去年" in (goal or "")):
            merged[key] = value
        elif merged.get(key) in (None, ""):
            merged[key] = value
    # “近期/最新/当前”没有明确年月时，以滚动窗口覆盖模型臆测的旧日期，
    # 避免模型返回任意历史月份导致 Sentinel-2 检索偏离用户真实意图。
    explicit_time = bool(re.search(r"\d{4}\s*年|\d{4}[-/]\d{1,2}|(?:春季|春天|夏季|夏天|秋季|秋天|冬季|冬天)", goal or ""))
    if merged.get("needs_timeliness") and not explicit_time:
        merged["date_start"] = (today - timedelta(days=90)).isoformat()
        merged["date_end"] = today.isoformat()
        merged["time_granularity"] = "rolling_90d"

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
    # 通过兼容入口调用，保证旧部署/测试对 call_deepseek_json 的拦截仍有效；
    # wrapper 内部实际已转发到 GLM provider。
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
            {"id": "review", "label": "GLM 结论复核"},
            {"id": "complete", "label": "整理结果"},
        ] if step],
        "task_strategy": task_strategy,
    }


def parse_amap_boundary(polyline):
    geometry = parse_amap_boundary_geometry(polyline)
    points = [point for ring in geometry["polygon"] for point in ring]
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


def parse_amap_boundary_geometry(polyline):
    """解析高德 polyline，保留多环边界并同时返回 bbox。

    高德以 ``|`` 分隔外环/内环、以 ``;`` 分隔点；旧实现把两者一起拆开，
    只能得到 bbox，导致后续统计把行政区外部区域也算进去。
    """
    rings = []
    for raw_ring in str(polyline or "").split("|"):
        ring = []
        for pair in raw_ring.split(";"):
            raw = pair.split(",")
            if len(raw) != 2:
                continue
            try:
                point = (float(raw[0]), float(raw[1]))
            except (TypeError, ValueError):
                continue
            ring.append(point)
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
    # 兼容旧测试/部分高德返回：`|` 仅被用作普通点分隔，单段不足 3 点。
    if not rings:
        fallback = []
        for pair in re.split(r"[;|]", str(polyline or "")):
            raw = pair.split(",")
            if len(raw) != 2:
                continue
            try:
                fallback.append((float(raw[0]), float(raw[1])))
            except (TypeError, ValueError):
                continue
        if len(fallback) >= 3:
            if fallback[0] != fallback[-1]:
                fallback.append(fallback[0])
            rings.append(fallback)
    points = [point for ring in rings for point in ring]
    if not points:
        raise ValueError("行政区边界为空，无法生成 bbox")
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    return {
        "bbox": {
            "min_lng": min(lngs),
            "min_lat": min(lats),
            "max_lng": max(lngs),
            "max_lat": max(lats),
        },
        "polygon": [[[lng, lat] for lng, lat in ring] for ring in rings],
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
        proxies=request_proxies(),
    )
    response.raise_for_status()
    data = response.json()
    districts = data.get("districts") or []
    if not districts:
        raise ValueError(f"未找到行政区：{place_name}")
    first = districts[0]
    geometry = parse_amap_boundary_geometry(first.get("polyline", ""))
    bbox = geometry["bbox"]
    return {
        "name": first.get("name") or place_name,
        "adcode": first.get("adcode", ""),
        "level": first.get("level", ""),
        "bbox": bbox,
        "polygon": geometry["polygon"],
        "geometry_type": "MultiPolygon" if len(geometry["polygon"]) > 1 else "Polygon",
        "candidate_count": len(districts),
        "bbox_policy": "保留行政区 polygon；影像检索用 bbox，统计应按 polygon 裁剪",
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
        proxies=request_proxies(),
    )
    response.raise_for_status()
    image = Image.open(BytesIO(response.content))
    arr = np.asarray(image.convert("F"), dtype=np.float32)
    return arr


def compute_ndwi_from_arrays(green, nir, threshold=NDWI_THRESHOLD, min_valid_pixels=NDWI_MIN_VALID_PIXELS, valid_mask=None):
    green = np.asarray(green, dtype=np.float32)
    nir = np.asarray(nir, dtype=np.float32)
    if green.shape != nir.shape:
        raise ValueError("green 与 nir 数组尺寸不一致")
    ndwi, valid = spectral_ndwi(green, nir, valid_mask=valid_mask)
    if not np.any(valid):
        raise ValueError("NDWI 有效像元为空")
    valid_count = int(valid.sum())
    if valid_count < min_valid_pixels:
        raise ValueError(f"NDWI 有效像元过少（{valid_count}），疑似影像覆盖不足或 NoData 过多")
    water = valid & (ndwi > threshold)
    ratio = float(water.sum() / valid.sum())
    values = ndwi[valid]
    stats = summarize_index(ndwi, valid)
    return {
        "available": True,
        "method": "NDWI=(Green-NIR)/(Green+NIR)",
        "threshold": threshold,
        "water_ratio": round(ratio, 4),
        "water_percent": round(ratio * 100, 1),
        "mean_ndwi": stats["mean"],
        "max_ndwi": stats["max"],
        "sample_size_px": valid_count,
        "valid_pixel_ratio": stats.get("valid_pixel_ratio", 0.0),
        "limitations": "轻量 NDWI 仅用于 bbox 内水体线索筛查；已按 SCL 排除云、云影、卷云和雪，但未做行政边界精确裁剪或严格多景合成。",
    }


def polygon_mask_for_bbox(polygon, bbox, shape):
    """把经纬度多环 polygon 栅格化为 bbox 数组掩膜。

    高德行政区 polyline 的 ``|`` 通常表示多个独立 polygon，不能把后续环
    误当成孔洞；这里按多环并集处理，避免把真实行政区内部大片区域排除。
    """
    if not polygon or not isinstance(bbox, dict) or len(shape) != 2:
        return None
    h, w = int(shape[0]), int(shape[1])
    xs = np.linspace(float(bbox["min_lng"]), float(bbox["max_lng"]), w, endpoint=False)
    ys = np.linspace(float(bbox["max_lat"]), float(bbox["min_lat"]), h, endpoint=False)
    x_step = (xs[1] - xs[0]) if w > 1 else 0
    y_step = (ys[0] - ys[1]) if h > 1 else 0
    xx, yy = np.meshgrid(xs + x_step / 2, ys - y_step / 2)

    def ring_mask(ring):
        if not ring or len(ring) < 3:
            return np.zeros((h, w), dtype=bool)
        inside = np.zeros((h, w), dtype=bool)
        x0, y0 = ring[-1]
        for x1, y1 in ring:
            crosses = ((y1 > yy) != (y0 > yy))
            denom = (y0 - y1) if y0 != y1 else 1e-12
            x_intersect = (x0 - x1) * (yy - y1) / denom + x1
            inside ^= crosses & (xx < x_intersect)
            x0, y0 = x1, y1
        return inside

    mask = np.zeros((h, w), dtype=bool)
    for ring in polygon:
        mask |= ring_mask(ring)
    return mask


def compute_ndwi_summary(candidate, bbox, titiler_endpoint=None, threshold=NDWI_THRESHOLD, polygon=None):
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
        scl_url = (assets.get("scl") or {}).get("href")
        valid_mask = None
        if scl_url:
            scl = fetch_cog_bbox_array(scl_url, bbox, titiler_endpoint)
            # Sentinel-2 SCL: 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow/ice
            valid_mask = ~np.isin(np.rint(scl).astype(np.int16), [3, 8, 9, 10, 11])
        polygon_mask = polygon_mask_for_bbox(polygon, bbox, green.shape) if polygon else None
        if polygon_mask is not None:
            valid_mask = polygon_mask if valid_mask is None else (valid_mask & polygon_mask)
        result = compute_ndwi_from_arrays(green, nir, threshold=threshold, valid_mask=valid_mask)
        if polygon_mask is not None:
            result["limitations"] = result["limitations"].replace("未做行政边界精确裁剪", "已按行政区 polygon 裁剪")
            result["polygon_clipped"] = True
        return result
    except Exception as exc:
        return {
            "available": False,
            "method": "NDWI=(Green-NIR)/(Green+NIR)",
            "reason": str(exc)[:180],
            "limitations": "NDWI 计算失败时仅保留视觉解译结论，不输出水体比例量化。",
        }


def compute_ndwi_mosaic_summary(
    candidates,
    bbox,
    titiler_endpoint=None,
    threshold=NDWI_THRESHOLD,
    polygon=None,
):
    summaries = []
    weighted_water = 0
    weighted_mean = 0
    weighted_max = None
    sample_total = 0
    failed = []
    for candidate in candidates or []:
        summary = compute_ndwi_summary(
            candidate,
            bbox,
            titiler_endpoint=titiler_endpoint,
            threshold=threshold,
            polygon=polygon,
        )
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
            "polygon_clipped": bool(polygon),
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
        "polygon_clipped": bool(polygon),
        "limitations": (
            "多景 NDWI 为行政区 polygon 内有效像元加权结果，已按 SCL 排除云、云影、卷云和雪；"
            "仍不等同于严格辐射一致化月度合成。"
            if polygon
            else
            "多景 NDWI 为 bbox 内有效像元加权结果，已按 SCL 排除云、云影、卷云和雪；"
            "未做行政边界精确裁剪，仍不等同于严格辐射一致化月度合成。"
        ),
    }
