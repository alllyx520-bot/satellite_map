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
import tifffile
from PIL import Image

from .analysis_strategy import build_analysis_strategy
from .http import request_proxies  # noqa: F401  # 唯一实现；此处保留原名，agent/providers.py 等旧 import 不受影响
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
    "flood": "flood",
    "flood_monitoring": "flood",
    "inundation": "flood",
    "terrain": "terrain",
    "terrain_analysis": "terrain",
    "elevation": "terrain",
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
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("模型 JSON 含重复字段")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("模型 JSON 含非有限数值")

    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("模型 JSON 必须是对象")
        return value
    except (ValueError, TypeError) as exc:
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
    # flood/terrain 优先于通用 task 归类:它们决定专用传感器(SAR/DEM),不能被 water/land_use 抢走。
    if any(word in text for word in ("洪水", "洪涝", "淹没", "内涝", "汛情")):
        slots["task"] = "flood"
    elif any(word in text for word in ("地形", "坡度", "高程", "山地", "地势")):
        slots["task"] = "terrain"
    elif any(word in text for word in ("水体", "水域", "河流", "湖泊", "水库", "岸线", "湿地")):
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
    if slots["task"] == "flood":
        # SAR 全天候、对水体/淹没敏感,洪涝任务固定走 Sentinel-1。
        slots["source"] = "sentinel1"
    if slots["task"] == "terrain":
        slots["source"] = "copdem"
    if any(word in text for word in ("近期", "最新", "现在", "当前", "变化", "新增", "扩张", "退化")):
        slots["needs_timeliness"] = True
        # flood/terrain 的专用源优先级高于时效覆写。
        if slots["task"] not in ("small_target", "flood", "terrain"):
            slots["source"] = "sentinel2"
    # 多云/阴天/夜间等光学受限条件:SAR 全天候兜底,同样不抢 flood/terrain。
    if any(word in text for word in ("多云", "阴天", "夜间成像", "全天候")) and slots["task"] not in ("flood", "terrain"):
        slots["source"] = "sentinel1"
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
    if merged["task"] not in ("water", "vegetation", "agriculture", "small_target", "built_up", "land_use", "flood", "terrain"):
        merged["task"] = rule_slots.get("task") or "land_use"
    if merged.get("source") == "earth_search":
        merged["source"] = "sentinel2"
    if merged.get("source") not in ("sentinel2", "mapbox", "tianditu", "esri", "sentinel1", "copdem"):
        task = merged.get("task")
        if task == "flood":
            merged["source"] = "sentinel1"
        elif task == "terrain":
            merged["source"] = "copdem"
        else:
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
                "source 只能是 sentinel2/mapbox/tianditu/esri/sentinel1/copdem：sentinel2 适合近期光学态势，mapbox 适合建筑道路细节；tianditu(天地图)同为高清底图，中国区行政区调查优先选用(合规)；esri(Esri World Imagery)同为高清底图，适合全球范围细节；洪水/淹没/多云/夜间等全天候需求用 sentinel1(SAR)，地形/坡度/高程用 copdem(静态 DEM)；mode 只能是 precise 或 fast。"
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


def fetch_cog_bbox_array(asset, bbox, titiler_endpoint, size=256, timeout=60, kind="reflectance", verify_grid=False):
    """读取 Sentinel COG bbox，并按资产语义返回数组。

    反射率资产使用 STAC raster:bands 的 scale/offset；SCL 是离散分类栅格，
    禁止使用连续值 rescale，避免云/阴影类别被压成 0/255。
    """
    asset_info = asset if isinstance(asset, dict) else {}
    asset_url = asset_info.get("href") if isinstance(asset, dict) else asset
    if not asset_url:
        raise ValueError("COG 资产缺少 href")
    endpoint = (
        f"{(titiler_endpoint or 'https://titiler.xyz').rstrip('/')}/cog/bbox/"
        f"{bbox['min_lng']},{bbox['min_lat']},{bbox['max_lng']},{bbox['max_lat']}/"
        f"{size}x{size}.tif"
    )
    params = {"url": asset_url}
    if verify_grid:
        params["dst_crs"] = "EPSG:4326"
    if kind == "scl":
        params["resampling"] = "nearest"
    else:
        params["resampling"] = "bilinear"
    # 反射率不能复用显示型 rescale；保留源 dtype 并在本地按 STAC scale/offset 转换。
    response = requests.get(
        endpoint,
        params=params,
        timeout=timeout,
        proxies=request_proxies(),
    )
    response.raise_for_status()
    with tifffile.TiffFile(BytesIO(response.content)) as image:
        page = image.pages[0]
        if verify_grid:
            scale = page.tags.get(33550)
            tie = page.tags.get(33922)
            keys = page.tags.get(34735)
            if not scale or not tie or not keys:
                raise ValueError("影像缺少可验证的地理网格元数据")
            key_values = keys.value
            geographic = {key_values[i]: key_values[i + 3] for i in range(4, len(key_values), 4) if key_values[i + 1] == 0}
            if geographic.get(2048) != 4326:
                raise ValueError("影像输出投影不是请求的 EPSG:4326")
            sx, sy = scale.value[:2]
            x0, y0 = tie.value[3:5]
            expected = [bbox['min_lng'], bbox['max_lat'], bbox['max_lng'], bbox['min_lat']]
            actual = [x0, y0, x0 + sx * page.imagewidth, y0 - sy * page.imagelength]
            if page.imagewidth != size or page.imagelength != size or not np.allclose(expected, actual, rtol=0, atol=1e-7):
                raise ValueError("影像输出地理网格与请求不一致")
        if kind == "palette":
            # 调色板 COG（如 GSW occurrence / ESA WorldCover）经 TiTiler 输出的是
            # RGB(A) 颜色而非原始值；这里返回 RGB(A) 数组，由调用方结合
            # /cog/colormap 反查数值。alpha==0 的像元整体置 NaN 表示无效。
            if len(image.pages) != 1 or int(page.photometric) != 2 or page.samplesperpixel not in (3, 4):
                raise ValueError("palette 读取必须返回 RGB/RGBA TIFF 栅格")
            arr = np.moveaxis(np.asarray(page.asarray(), dtype=np.float32), page.axes.index("S"), -1)
            if page.samplesperpixel == 4:
                arr[arr[..., 3] == 0] = np.nan
            return arr
        if len(image.pages) != 1 or int(page.photometric) not in (0, 1):
            raise ValueError("COG 读取必须返回单波段 TIFF 栅格（可含 alpha 有效掩膜）")
        arr = np.asarray(page.asarray(), dtype=np.float32)
        if arr.ndim == 3 and page.samplesperpixel == 2 and tuple(int(v) for v in page.extrasamples) in {(1,), (2,)}:
            # TiTiler's GeoTIFF carries data + uint16 alpha. Pillow cannot read
            # this TIFF layout; the alpha must also participate in validity.
            samples = np.moveaxis(arr, page.axes.index("S"), -1)
            arr = np.where(samples[..., 1] > 0, samples[..., 0], np.nan)
        elif arr.ndim != 2:
            raise ValueError("COG 读取必须返回单波段 TIFF 栅格（可含 alpha 有效掩膜）")
    bands = asset_info.get("raster:bands") or []
    band = bands[0] if bands and isinstance(bands[0], dict) else {}
    nodata = band.get("nodata")
    if nodata is not None:
        arr[arr == float(nodata)] = np.nan
    if kind != "scl":
        if "scale" not in band or "offset" not in band:
            raise ValueError("反射率资产缺少明确的 scale/offset")
        scale = float(band["scale"])
        offset = float(band["offset"])
        if not np.isfinite(scale) or not np.isfinite(offset) or scale <= 0:
            raise ValueError("反射率 scale/offset 无效")
        if scale != 1.0 or offset != 0.0:
            arr = arr * scale + offset
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
        "masked_pixel_ratio": round(1.0 - float(stats.get("valid_pixel_ratio", 0.0)), 4),
        "thresholded_water_pixel_ratio": round(ratio, 4),
        "thresholded_water_area_m2": None,
        "threshold_method": "fixed",
        "alternative_thresholds": [
            {"threshold": round(float(threshold) + delta, 4), "water_ratio": round(float(np.mean(values > float(threshold) + delta)), 4)}
            for delta in (-0.05, -0.02, 0.02, 0.05)
        ],
        "aoi_pixel_ratio": stats.get("valid_pixel_ratio", 0.0),
        "mask_source": "nodata_and_common_valid_mask",
        "measurement_grade": "screening",
        "change_detection_allowed": False,
        "limitations": "结果仅统计输入的共同有效像元；QA、行政区掩膜和反射率校准由上游数据读取步骤负责。",
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
    if isinstance(polygon, dict):
        geometry = polygon.get("geometry") if polygon.get("type") == "Feature" else polygon
        if geometry.get("type") == "Polygon":
            polygons = [geometry.get("coordinates") or []]
        elif geometry.get("type") == "MultiPolygon":
            polygons = geometry.get("coordinates") or []
        else:
            raise ValueError("AOI 必须是 Polygon 或 MultiPolygon")
        for rings in polygons:
            if not rings:
                continue
            part = ring_mask(rings[0])
            for hole in rings[1:]:
                part &= ~ring_mask(hole)
            mask |= part
    else:
        for ring in polygon:
            mask |= ring_mask(ring)
    return mask


def _read_ndwi_inputs(candidate, bbox, titiler_endpoint):
    # 无 SCL 的 collection(如 sentinel-2-l1c 兜底)显式拒绝,不做无掩膜 NDWI。
    from ..imagery_sources.earth_search import get_collection_profile
    collection = getattr(candidate, "collection", None) or "sentinel-2-l2a"
    if get_collection_profile(collection).get("qa_band") is None:
        raise ValueError(f"{collection} 无 SCL 云掩膜，光谱指数不可用，请改用 L2A 影像")
    assets = candidate.assets or {}
    missing = [name for name in ("green", "nir", "scl") if not (assets.get(name) or {}).get("href")]
    if missing:
        raise ValueError("候选影像缺少 " + ", ".join("SCL" if name == "scl" else name for name in missing) + " 资产")
    green = fetch_cog_bbox_array(assets["green"], bbox, titiler_endpoint)
    nir = fetch_cog_bbox_array(assets["nir"], bbox, titiler_endpoint)
    scl = fetch_cog_bbox_array(assets["scl"], bbox, titiler_endpoint, kind="scl")
    if scl.shape != green.shape or nir.shape != green.shape:
        raise ValueError("波段与 SCL 网格尺寸不一致")
    return green, nir, np.isfinite(scl) & np.isin(scl, [4, 5, 6, 7])


def _ndwi_product(green, nir, qa, bbox, polygon, threshold):
    polygon_mask = polygon_mask_for_bbox(polygon, bbox, green.shape) if polygon else None
    valid_mask = qa.copy()
    if polygon_mask is not None:
        valid_mask &= polygon_mask
    calibrated = np.isfinite(green) & np.isfinite(nir) & (green >= 0) & (nir >= 0) & (green <= 1) & (nir <= 1)
    reflectance_rejected = int((valid_mask & ~calibrated).sum())
    valid_mask &= calibrated
    aoi_count = int(polygon_mask.sum()) if polygon_mask is not None else green.size
    usable = valid_mask & (np.abs(green + nir) > 1e-6)
    if aoi_count == 0 or int(usable.sum()) / aoi_count < 0.6:
        raise ValueError("AOI 内有效像元低于 60%，不能输出水体比例")
    result = compute_ndwi_from_arrays(green, nir, threshold=threshold, valid_mask=valid_mask)
    result.update({
        "mask_source": "sentinel-2-scl+aoi+nodata" if polygon_mask is not None else "sentinel-2-scl+nodata",
        "masked_pixel_count": int((~usable).sum()), "aoi_pixel_count": aoi_count,
        "invalid_reflectance_pixel_count": reflectance_rejected,
        "valid_pixel_ratio": round(int(usable.sum()) / aoi_count, 4),
        "data_contract": {"source": "sentinel-2-l2a", "reflectance_scale_applied": True,
                          "scl_resampling": "nearest", "measurement_grade": "screening"},
        "limitations": "仅统计共同有效像元；排除校准反射率不在 [0,1] 的像元；输出网格可能经过重采样，不代表原生分辨率测量。",
    })
    if polygon_mask is not None:
        result["polygon_clipped"] = True
        result["limitations"] += "已按行政区 polygon 裁剪。"
    return result


def _ndwi_error(exc):
    response = getattr(exc, "response", None)
    return {
        "available": False, "method": "NDWI=(Green-NIR)/(Green+NIR)",
        "reason": str(exc)[:180] if isinstance(exc, ValueError) else "外部 COG/TiTiler 资产读取失败，请检查服务状态后重试。",
        "diagnostics": {"error_type": type(exc).__name__, "http_status": getattr(response, "status_code", None)},
        "limitations": "指标未完成，不能输出水体比例或正式结论。",
    }


def compute_ndwi_summary(candidate, bbox, titiler_endpoint=None, threshold=NDWI_THRESHOLD, polygon=None):
    try:
        return _ndwi_product(*_read_ndwi_inputs(candidate, bbox, titiler_endpoint), bbox, polygon, threshold)
    except Exception as exc:
        return _ndwi_error(exc)


def compute_ndwi_mosaic_summary(candidates, bbox, titiler_endpoint=None, threshold=NDWI_THRESHOLD, polygon=None):
    """Compose calibrated bands on one requested grid; overlapping pixels count once."""
    try:
        candidates = list(candidates or [])
        dates = [getattr(candidate, "acquired_at", None) for candidate in candidates]
        if not dates or any(value is None for value in dates):
            raise ValueError("多景统计必须提供每个场景的明确拍摄日期")
        date_ids = sorted({value.date().isoformat() for value in dates})
        if len(date_ids) != 1:
            raise ValueError("禁止跨日期拼接后输出单期水体比例")
        green = nir = covered = None
        contributions = []
        for candidate in candidates:
            g, n, qa = _read_ndwi_inputs(candidate, bbox, titiler_endpoint)
            if green is None:
                green = np.full(g.shape, np.nan, dtype=np.float32)
                nir = np.full(g.shape, np.nan, dtype=np.float32)
                covered = np.zeros(g.shape, dtype=bool)
            if g.shape != green.shape:
                raise ValueError("多景输出网格尺寸不一致")
            usable = qa & np.isfinite(g) & np.isfinite(n) & (g >= 0) & (n >= 0) & (g <= 1) & (n <= 1) & (np.abs(g+n) > 1e-6)
            new = usable & ~covered
            green[new], nir[new] = g[new], n[new]
            covered |= new
            contributions.append({"product_id": getattr(candidate, "product_id", ""), "contributed_pixels": int(new.sum())})
        result = _ndwi_product(green, nir, covered, bbox, polygon, threshold)
        result.update({"aggregation": "同一目标网格逐像元合成，重叠像元只统计一次",
                       "candidate_count": len(candidates), "candidate_summaries": contributions,
                       "acquisition_dates": date_ids, "temporal_consistency": "same_date",
                       "change_detection_allowed": False, "failed_candidates": []})
        return result
    except Exception as exc:
        return _ndwi_error(exc)
