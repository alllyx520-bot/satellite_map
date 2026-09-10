"""Agent 证据型外部开放数据只读接口。

所有外部 HTTP 调用集中在本模块，测试统一 patch 这里的 ``requests``
与 ``fetch_cog_bbox_array``。熔断复用 service_health 三件套，
重试/阈值 env 命名 ``<SRC>_RETRIES/_CIRCUIT_FAILURES/_CIRCUIT_SECONDS``。

实测（2026-09-10，本机默认代理策略）：
- FIRMS area csv、Open-Meteo forecast/archive、Overpass(需带 User-Agent)、
  GCS GSW occurrence COG、AWS ESA WorldCover COG 均可直连；
  GSW/WorldCover 经公共 TiTiler(titiler.xyz) 按 bbox 读取已验证。
"""
import csv
import io
import math
import os
import time

import numpy as np
import requests

from .agent_tools import fetch_cog_bbox_array
from .http import request_proxies
from .service_health import check_service, record_failure, record_success, service_key

FIRMS_API_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_DEFAULT_SOURCE = "VIIRS_SNPP_NRT"
FIRMS_MAX_DAYS = 5
FIRMS_ATTRIBUTION = "We acknowledge the use of data and imagery from LANCE FIRMS operated by NASA GSFC/ESDIS"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass 对缺失/无意义 UA 直接 406/429（实测 2026-09-10）。
OVERPASS_USER_AGENT = "SatelliteSense/1.0 (remote-sensing QA evidence toolkit)"
OVERPASS_TIMEOUT = 25
OSM_ATTRIBUTION = "OSM 数据 © OpenStreetMap contributors, ODbL"

OPENMETEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPENMETEO_ATTRIBUTION = "气象数据来自 Open-Meteo(预报 IFS / 历史 ERA5),CC-BY-4.0"

# 实测 2026-09-10:GCS 公开桶，10°x10° 瓦片，按西边缘经度+北边缘纬度命名，零填充无。
GSW_OCCURRENCE_URL = (
    "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/"
    "occurrence_{lon_token}_{lat_token}v1_4_2021.tif"
)
GSW_ATTRIBUTION = "JRC Global Surface Water v1.4 (Pekel et al., 2016), Copernicus Programme"
# 实测 2026-09-10:AWS 公开桶，3°x3° 瓦片，按西南角 3 的倍数命名，lon 三位零填充。
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{lat_token}{lon_token}_Map.tif"
)
WORLDCOVER_ATTRIBUTION = "ESA WorldCover 2021 v200 (10m, 单年产品，不做跨年比较)"

WORLDCOVER_CLASSES = {
    10: "林地", 20: "灌丛", 30: "草地", 40: "耕地", 50: "建成区",
    60: "裸地", 70: "雪冰", 80: "水体", 90: "湿地", 95: "红树林", 100: "苔藓",
}

WMO_WEATHER_CODES = {
    0: "晴", 1: "晴间多云", 2: "部分多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _request_with_circuit(kind, url, *, method="get", timeout=20, retries_default=1, **kwargs):
    """带熔断与有限重试的外部 GET/POST；网络与 5xx/429 失败计入熔断。"""
    health_key = service_key(kind, url)
    check_service(health_key)
    retries = max(0, min(3, _env_int(f"{kind.upper()}_RETRIES", retries_default)))
    threshold = _env_int(f"{kind.upper()}_CIRCUIT_FAILURES", 2)
    cooldown = _env_int(f"{kind.upper()}_CIRCUIT_SECONDS", 30)
    caller = requests.get if method == "get" else requests.post
    kwargs.setdefault("proxies", request_proxies())
    response = None
    for attempt in range(retries + 1):
        try:
            response = caller(url, timeout=timeout, **kwargs)
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= retries:
                record_failure(health_key, exc, threshold=threshold, cooldown_seconds=cooldown)
                raise
            time.sleep(min(2 ** attempt, 4))
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 500 or status in (408, 425, 429):
            record_failure(health_key, exc, threshold=threshold, cooldown_seconds=cooldown)
        raise
    record_success(health_key)
    return response


def _bbox_corners(bbox):
    return (
        float(bbox["min_lng"]), float(bbox["min_lat"]),
        float(bbox["max_lng"]), float(bbox["max_lat"]),
    )


# ---------------- T1 FIRMS 火点 ----------------

def query_firms_fires(bbox, days=3, source=FIRMS_DEFAULT_SOURCE, api_key=None, timeout=30):
    """查询 bbox 内近 N 天 FIRMS 活跃火点 CSV，返回结构化火点列表。"""
    key = api_key if api_key is not None else os.environ.get("FIRMS_MAP_KEY", "")
    if not key.strip():
        raise ValueError("缺少 FIRMS_MAP_KEY 环境变量（NASA FIRMS Map Key,https://firms.modaps.eosdis.nasa.gov/api/area/ 免费申请）")
    days = max(1, min(FIRMS_MAX_DAYS, int(days or 3)))
    west, south, east, north = _bbox_corners(bbox)
    url = f"{FIRMS_API_URL}/{key}/{source}/{west},{south},{east},{north}/{days}"
    response = _request_with_circuit("firms", url, timeout=timeout)
    text = response.text or ""
    first_line = text.split("\n", 1)[0]
    if "latitude" not in first_line:
        raise ValueError(f"FIRMS 返回非 CSV 内容（可能 key 无效或配额耗尽）: {first_line[:120]}")
    fires = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            fires.append({
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "bright_ti4": float(row.get("bright_ti4") or 0),
                "frp": float(row.get("frp") or 0),
                "confidence": (row.get("confidence") or "").strip(),
                "acq_date": (row.get("acq_date") or "").strip(),
                "daynight": (row.get("daynight") or "").strip(),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return {"fires": fires, "count": len(fires), "source": source, "days": days, "bbox": bbox}


def summarize_firms_fires(data):
    """把 query_firms_fires 结果压缩成 Agent 友好的摘要。"""
    fires = data.get("fires") or []
    confidence_dist = {}
    for fire in fires:
        label = {"n": "nominal", "h": "high", "l": "low"}.get(fire.get("confidence", "").lower(), fire.get("confidence") or "unknown")
        confidence_dist[label] = confidence_dist.get(label, 0) + 1
    top_frp = sorted(fires, key=lambda item: item.get("frp") or 0, reverse=True)[:3]
    return {
        "fire_count": data.get("count", 0),
        "confidence_distribution": confidence_dist,
        "top_frp_fires": [
            {"latitude": f["latitude"], "longitude": f["longitude"], "frp": f["frp"],
             "acq_date": f["acq_date"], "daynight": "昼" if f.get("daynight") == "D" else "夜"}
            for f in top_frp
        ],
        "source": data.get("source"),
        "days": data.get("days"),
        "latency_note": "FIRMS NRT 火点通常在卫星过境后约 3 小时内发布，非实时；VIIRS_SNPP 每日昼/夜各约一次过境。",
        "attribution": FIRMS_ATTRIBUTION,
    }


# ---------------- T2 OSM Overpass ----------------

def query_osm_context(bbox, timeout=OVERPASS_TIMEOUT):
    """统计 bbox 内建筑物/道路/水系数量与主要 landuse 类别。"""
    west, south, east, north = _bbox_corners(bbox)
    area = f"({south},{west},{north},{east})"
    query = (
        "[out:json][timeout:25];"
        f'way["building"]{area}->.b; .b out count;'
        f'way["highway"]{area}->.h; .h out count;'
        f'(way["waterway"]{area}; way["natural"="water"]{area}; relation["natural"="water"]{area};)->.w; .w out count;'
        f'way["landuse"]{area}->.l; .l out tags 1000;'
    )
    response = _request_with_circuit(
        "overpass", OVERPASS_URL, method="post", timeout=timeout + 5,
        retries_default=0,  # 单实例串行纪律，不重试轰炸
        data={"data": query},
        headers={"User-Agent": OVERPASS_USER_AGENT},
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Overpass 返回了无效 JSON") from exc
    counts = []
    landuse_tally = {}
    landuse_sampled = 0
    for element in payload.get("elements") or []:
        if element.get("type") == "count":
            counts.append(int((element.get("tags") or {}).get("total") or 0))
        elif element.get("type") == "way":
            value = (element.get("tags") or {}).get("landuse")
            if value:
                landuse_tally[value] = landuse_tally.get(value, 0) + 1
                landuse_sampled += 1
    counts += [0] * (3 - len(counts))
    landuse_top = sorted(landuse_tally.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "building_count": counts[0],
        "road_count": counts[1],
        "water_feature_count": counts[2],
        "landuse_top": [{"landuse": name, "count": n} for name, n in landuse_top],
        "landuse_sampled": landuse_sampled,
        "bbox": bbox,
    }


# ---------------- T3 Open-Meteo ----------------

def query_weather_context(lat, lng, date=None, timeout=20):
    """无 date 返回近 7 天+今日预报 daily 序列；有 date 返回该日 ERA5 历史天气。"""
    daily_vars = "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weathercode"
    params = {"latitude": float(lat), "longitude": float(lng), "daily": daily_vars, "timezone": "auto"}
    if date:
        url = OPENMETEO_ARCHIVE_URL
        params.update({"start_date": date, "end_date": date})
        mode = "archive"
    else:
        url = OPENMETEO_FORECAST_URL
        params.update({"past_days": 7, "forecast_days": 1})
        mode = "forecast"
    response = _request_with_circuit("openmeteo", url, params=params, timeout=timeout)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Open-Meteo 返回了无效 JSON") from exc
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    series = []
    for idx, day in enumerate(times):
        code = (daily.get("weathercode") or [None] * len(times))[idx]
        series.append({
            "date": day,
            "temperature_2m_max": (daily.get("temperature_2m_max") or [None] * len(times))[idx],
            "temperature_2m_min": (daily.get("temperature_2m_min") or [None] * len(times))[idx],
            "precipitation_sum": (daily.get("precipitation_sum") or [None] * len(times))[idx],
            "precipitation_probability_max": (daily.get("precipitation_probability_max") or [None] * len(times))[idx],
            "weathercode": code,
            "weather_desc": WMO_WEATHER_CODES.get(code, f"未知({code})"),
        })
    return {
        "mode": mode,
        "latitude": payload.get("latitude", lat),
        "longitude": payload.get("longitude", lng),
        "timezone": payload.get("timezone"),
        "daily": series,
        "attribution": OPENMETEO_ATTRIBUTION,
    }


# ---------------- 公共 COG 瓦片定位 ----------------

def _cog_colormap(asset_url, titiler_endpoint=None, timeout=30):
    """读取调色板 COG 的数值→颜色表（TiTiler /cog/info 的 colormap 字段）。"""
    endpoint = f"{(titiler_endpoint or 'https://titiler.xyz').rstrip('/')}/cog/info"
    response = requests.get(endpoint, params={"url": asset_url}, timeout=timeout, proxies=request_proxies())
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("TiTiler info 返回了无效 JSON") from exc
    return payload.get("colormap") or {}


def _fetch_palette_values(asset_url, bbox, titiler_endpoint=None, size=128, timeout=60):
    """读取调色板 COG 并反查为原始数值数组（无效像元为 NaN）。"""
    rgba = fetch_cog_bbox_array(asset_url, bbox, titiler_endpoint, size=size, timeout=timeout, kind="palette")
    colormap = _cog_colormap(asset_url, titiler_endpoint, timeout=timeout)
    lookup = {}
    for value, color in (colormap or {}).items():
        try:
            lookup[(int(color[0]), int(color[1]), int(color[2]))] = float(value)
        except (TypeError, ValueError, IndexError):
            continue
    flat = rgba.reshape(-1, rgba.shape[-1])
    out = np.full(flat.shape[0], np.nan, dtype=np.float32)
    valid = np.isfinite(flat[:, 0])
    for (r, g, b), value in lookup.items():
        match = valid & (flat[:, 0] == r) & (flat[:, 1] == g) & (flat[:, 2] == b)
        out[match] = value
    return out.reshape(rgba.shape[:2])


def _tile_token(value, positive, negative, pad=0):
    token = f"{abs(int(value)):0{pad}d}" if pad else str(abs(int(value)))
    return f"{token}{positive if value >= 0 else negative}"


def _hemi_token(value, positive, negative, pad=0):
    token = f"{abs(int(value)):0{pad}d}" if pad else str(abs(int(value)))
    return f"{positive if value >= 0 else negative}{token}"


def _dominant_tile(bbox, tile_deg):
    """bbox 跨瓦片时选覆盖面积最大的一片，返回 (锚点 lon, 锚点 lat, 是否跨界)。"""
    west, south, east, north = _bbox_corners(bbox)
    best, best_area = None, -1.0
    for lon0 in range(int(math.floor(west / tile_deg)) * tile_deg, int(math.floor(east / tile_deg)) * tile_deg + 1, tile_deg):
        for lat0 in range(int(math.floor(south / tile_deg)) * tile_deg, int(math.floor(north / tile_deg)) * tile_deg + 1, tile_deg):
            overlap = max(0.0, min(east, lon0 + tile_deg) - max(west, lon0)) * max(0.0, min(north, lat0 + tile_deg) - max(south, lat0))
            if overlap > best_area:
                best, best_area = (lon0, lat0), overlap
    total = max(1e-12, (east - west) * (north - south))
    return best[0], best[1], best_area < total - 1e-9


# ---------------- T4 JRC GSW 水体基线 ----------------

def query_water_baseline(bbox, ndwi_ratio=None, titiler_endpoint=None, size=128, timeout=60):
    """读取 GSW occurrence(0-100%)，给出历史水体覆盖率与季节性水域占比。

    GSW 瓦片命名 = 西边缘经度 + 北边缘纬度（实测 2026-09-10：
    occurrence_110E_40N 覆盖 lon110-120/lat30-40）。
    """
    west, south, east, north = _bbox_corners(bbox)
    best, best_area = None, -1.0
    for lon0 in range(int(math.floor(west / 10)) * 10, int(math.floor(east / 10)) * 10 + 1, 10):
        latn = (int(math.floor(south / 10)) + 1) * 10
        while latn <= int(math.ceil(north / 10)) * 10:
            overlap = max(0.0, min(east, lon0 + 10) - max(west, lon0)) * max(0.0, min(north, latn) - max(south, latn - 10))
            if overlap > best_area:
                best, best_area = (lon0, latn), overlap
            latn += 10
    lon0, latn = best
    total = max(1e-12, (east - west) * (north - south))
    partial = best_area < total - 1e-9
    url = GSW_OCCURRENCE_URL.format(
        lon_token=_tile_token(lon0, "E", "W"),
        lat_token=_tile_token(latn, "N", "S"),
    )
    try:
        arr = _fetch_palette_values(url, bbox, titiler_endpoint, size=size, timeout=timeout)
    except requests.exceptions.HTTPError:
        return {"available": False, "reason": "该区域无 GSW 覆盖（瓦片不存在）", "tile": url.rsplit("/", 1)[-1]}
    valid = np.isfinite(arr) & (arr >= 0) & (arr <= 100)
    valid_count = int(valid.sum())
    if valid_count < 16:
        return {"available": False, "reason": "GSW 有效像元过少，无法形成基线结论", "tile": url.rsplit("/", 1)[-1]}
    occurrence = arr[valid]
    permanent_ratio = float((occurrence > 50).sum() / valid_count)
    seasonal_ratio = float(((occurrence >= 10) & (occurrence <= 50)).sum() / valid_count)
    result = {
        "available": True,
        "tile": url.rsplit("/", 1)[-1],
        "tile_partial": partial,
        "tile_note": "bbox 跨 GSW 瓦片，仅统计覆盖度最大的瓦片" if partial else "bbox 位于单一 GSW 瓦片内",
        "permanent_water_ratio": round(permanent_ratio, 4),
        "permanent_water_percent": round(permanent_ratio * 100, 1),
        "seasonal_water_ratio": round(seasonal_ratio, 4),
        "seasonal_water_percent": round(seasonal_ratio * 100, 1),
        "valid_pixel_ratio": round(valid_count / arr.size, 4),
        "attribution": GSW_ATTRIBUTION,
    }
    if ndwi_ratio is not None:
        ndwi_ratio = float(ndwi_ratio)
        result["ndwi_ratio"] = round(ndwi_ratio, 4)
        if ndwi_ratio > permanent_ratio + seasonal_ratio + 0.05:
            conclusion = "当期 NDWI 水体占比明显高于历史基线，提示可能存在新增淹没/洪泛。"
        elif ndwi_ratio < permanent_ratio - 0.05:
            conclusion = "当期 NDWI 水体占比低于历史常年水体基线，提示水面收缩或干旱线索。"
        else:
            conclusion = "当期 NDWI 水体占比与历史基线基本一致，未见显著异常。"
        result["baseline_comparison"] = conclusion
    return result


# ---------------- T5 ESA WorldCover ----------------

def query_landcover_context(bbox, titiler_endpoint=None, size=128, timeout=60):
    """读取 ESA WorldCover 2021 v200 分类栅格，给出各类占地占比 top5。"""
    lon0, lat0, partial = _dominant_tile(bbox, 3)
    url = WORLDCOVER_URL.format(
        lat_token=_hemi_token(lat0, "N", "S", pad=2),
        lon_token=_hemi_token(lon0, "E", "W", pad=3),
    )
    try:
        arr = _fetch_palette_values(url, bbox, titiler_endpoint, size=size, timeout=timeout)
    except requests.exceptions.HTTPError:
        return {"available": False, "reason": "该区域无 WorldCover 覆盖（瓦片不存在）", "tile": url.rsplit("/", 1)[-1]}
    valid = np.isfinite(arr)
    valid_count = int(valid.sum())
    if valid_count < 16:
        return {"available": False, "reason": "WorldCover 有效像元过少", "tile": url.rsplit("/", 1)[-1]}
    values = arr[valid].astype(int)
    tally = {}
    for code, name in WORLDCOVER_CLASSES.items():
        n = int((values == code).sum())
        if n:
            tally[name] = n / valid_count
    top = sorted(tally.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "available": True,
        "tile": url.rsplit("/", 1)[-1],
        "tile_partial": partial,
        "tile_note": "bbox 跨 WorldCover 瓦片，仅统计覆盖度最大的瓦片" if partial else "bbox 位于单一 WorldCover 瓦片内",
        "landcover_top": [{"class": name, "ratio": round(ratio, 4), "percent": round(ratio * 100, 1)} for name, ratio in top],
        "valid_pixel_ratio": round(valid_count / arr.size, 4),
        "attribution": WORLDCOVER_ATTRIBUTION,
    }
