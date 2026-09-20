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
from datetime import date as calendar_date

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
OPENMETEO_ATTRIBUTION = "气象数据来自 Open-Meteo（预报/历史再分析网格数据）,CC-BY-4.0"

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
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Overpass 响应缺少 elements，不能把无效响应当作零要素")
    if payload.get("remark"):
        raise ValueError("Overpass 报告查询未完整执行；请缩小 bbox 后重试，不能把部分结果当作完整统计")
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
    if len(counts) != 3:
        raise ValueError("Overpass 缺少完整的建筑/道路/水系统计，不能把缺失计数当作零")
    landuse_top = sorted(landuse_tally.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "building_count": counts[0],
        "road_count": counts[1],
        "water_feature_count": counts[2],
        "landuse_top": [{"landuse": name, "count": n} for name, n in landuse_top],
        "landuse_sampled": landuse_sampled,
        "bbox": bbox,
        "attribution": OSM_ATTRIBUTION,
        "limitations": ["OSM 为社区维护地图，数量表示地图要素而非遥感检出的真实目标；缺少地图要素不证明实地不存在", "未查询历史版本，不能作为过去日期的地物状态"],
    }


# ---------------- T3 Open-Meteo ----------------

def query_weather_context(lat, lng, date=None, timeout=20, *, date_start=None, date_end=None):
    """Return forecast or a bounded historical daily series at the requested point.

    Historical reanalysis has no precipitation probability variable. Sending that
    forecast-only field to /archive causes HTTP 400 for otherwise valid dates.
    """
    if date and (date_start or date_end):
        raise ValueError("weather 使用 date 或 date_start/date_end，不能混用")
    if bool(date_start) != bool(date_end):
        raise ValueError("weather 的 date_start/date_end 必须同时提供")
    start, end = (date, date) if date else (date_start, date_end)
    if start:
        try:
            first, last = calendar_date.fromisoformat(start), calendar_date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            raise ValueError("weather 日期必须为有效 YYYY-MM-DD") from exc
        if first > last or (last - first).days >= 366:
            raise ValueError("weather 日期区间须有序且不超过 366 日")
    daily_vars = "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"
    if not start:
        daily_vars += ",precipitation_probability_max"
    params = {"latitude": float(lat), "longitude": float(lng), "daily": daily_vars, "timezone": "auto"}
    if start:
        url = OPENMETEO_ARCHIVE_URL
        params.update({"start_date": start, "end_date": end})
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
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Open-Meteo 返回无效或错误响应")
    daily = payload.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        raise ValueError("Open-Meteo 没有返回日期序列，不能判断该时段无降水")
    for variable in daily_vars.split(","):
        if not isinstance(daily.get(variable), list) or len(daily[variable]) != len(times):
            raise ValueError(f"Open-Meteo {variable} 与日期序列不一致")
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
    precipitation = [row["precipitation_sum"] for row in series if row["precipitation_sum"] is not None]
    maximums = [row["temperature_2m_max"] for row in series if row["temperature_2m_max"] is not None]
    minimums = [row["temperature_2m_min"] for row in series if row["temperature_2m_min"] is not None]
    return {
        "mode": mode,
        "latitude": payload.get("latitude", lat),
        "longitude": payload.get("longitude", lng),
        "timezone": payload.get("timezone"),
        "daily": series,
        "daily_units": payload.get("daily_units") or {},
        "requested_period": {"date_start": start, "date_end": end} if start else None,
        "summary": {"day_count": len(series), "precipitation_valid_day_count": len(precipitation),
                    "precipitation_sum_mm": round(sum(precipitation), 3) if precipitation else None,
                    "temperature_max_celsius": max(maximums) if maximums else None,
                    "temperature_min_celsius": min(minimums) if minimums else None},
        "attribution": OPENMETEO_ATTRIBUTION,
        "limitations": ["此处为 bbox 中心点对应的气象网格，不能当作整个区域的面积平均或站点实测", "降水和温度只能补充时间背景，不能独立证明水位、冰情或水体变化的原因", "缺测日不计入降水合计；使用合计前须核对有效天数"],
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


def _intersecting_tiles(bbox, tile_deg):
    """Return every degree-grid tile touched by bbox, never silently selecting one."""
    west, south, east, north = _bbox_corners(bbox)
    lon_start = int(math.floor(west / tile_deg)) * tile_deg
    # East/north edges belong to the preceding tile when exactly aligned.
    lon_end = int(math.floor((east - 1e-12) / tile_deg)) * tile_deg
    lat_start = int(math.floor(south / tile_deg)) * tile_deg
    lat_end = int(math.floor((north - 1e-12) / tile_deg)) * tile_deg
    return [(lon, lat) for lon in range(lon_start, lon_end + 1, tile_deg)
            for lat in range(lat_start, lat_end + 1, tile_deg)]


# TiTiler resamples every clip to ``size`` pixels.  Pixel counts therefore do
# not represent a common ground area when clips have different extents.  These
# weights approximate each output pixel as a latitude/longitude cell on a
# sphere; the error is the usual spherical-Earth/geographic-grid approximation.
_EARTH_RADIUS_KM = 6371.0088


def _pixel_area_weights_sq_km(shape, bbox):
    """Return geographic area weights for a 2-D north-up clip on a sphere."""
    rows, cols = shape
    if rows <= 0 or cols <= 0:
        return np.empty(shape, dtype=np.float64)
    west, south, east, north = _bbox_corners(bbox)
    lon_width = abs(math.radians(east - west)) / cols
    lat_edges = np.linspace(math.radians(north), math.radians(south), rows + 1)
    row_areas = (_EARTH_RADIUS_KM ** 2 * lon_width *
                 np.abs(np.sin(lat_edges[:-1]) - np.sin(lat_edges[1:])))
    return np.broadcast_to(row_areas[:, np.newaxis], (rows, cols)).copy()


def _area_sq_km(bbox):
    """Approximate bbox area using the same spherical latitude-strip model."""
    return float(_pixel_area_weights_sq_km((1, 1), bbox)[0, 0])


def _coverage_fields(requested_area, fetched_area, valid_area, total_sample_area, valid_pixel_ratio=None):
    """Common area and coverage accounting for COG summaries."""
    return {
        "requested_area_sq_km": round(requested_area, 3),
        "fetched_area_sq_km": round(fetched_area, 3),
        "failed_area_sq_km": round(max(0.0, requested_area - fetched_area), 3),
        "valid_area_sq_km": round(valid_area, 3),
        "source_coverage_ratio": round(fetched_area / requested_area, 4) if requested_area else 0.0,
        "valid_area_ratio": round(valid_area / requested_area, 4) if requested_area else 0.0,
        # Kept for consumers that used the old resampled-pixel coverage field.
        "valid_pixel_ratio": round(valid_pixel_ratio if valid_pixel_ratio is not None
                                   else (valid_area / total_sample_area if total_sample_area else 0.0), 4),
        "area_weighting_note": "按输出像元对应的经纬度条带球面面积加权；采用球形地球与 north-up 等距经纬网近似。",
    }


# ---------------- T4 JRC GSW 水体基线 ----------------

def query_water_baseline(bbox, ndwi_ratio=None, titiler_endpoint=None, size=128, timeout=60):
    """读取 GSW occurrence(0-100%)，给出按面积加权的历史出现频率分组。

    GSW 瓦片命名 = 西边缘经度 + 北边缘纬度（实测 2026-09-10：
    occurrence_110E_40N 覆盖 lon110-120/lat30-40）。
    """
    arrays, tiles, failed = [], [], []
    west, south, east, north = _bbox_corners(bbox)
    requested_area = _area_sq_km(bbox)
    fetched_area = 0.0
    for lon0, lat0 in _intersecting_tiles(bbox, 10):
        latn = lat0 + 10
        url = GSW_OCCURRENCE_URL.format(lon_token=_tile_token(lon0, "E", "W"), lat_token=_tile_token(latn, "N", "S"))
        clip = {"min_lng": max(west, lon0), "max_lng": min(east, lon0 + 10),
                "min_lat": max(south, lat0), "max_lat": min(north, lat0 + 10)}
        try:
            arrays.append((_fetch_palette_values(url, clip, titiler_endpoint, size=size, timeout=timeout), clip))
            tiles.append(url.rsplit("/", 1)[-1])
            fetched_area += _area_sq_km(clip)
        except requests.exceptions.RequestException as exc:
            failed.append({"tile": url.rsplit("/", 1)[-1], "error": str(exc) or type(exc).__name__})
    if failed:
        fields = _coverage_fields(requested_area, fetched_area, 0.0, 0.0)
        return {"available": False, "reason": "GSW 未能读取 AOI 的全部瓦片，不能输出完整区域基线", "tiles": tiles,
                "failed_tiles": failed, "coverage_complete": False, **fields}
    if not arrays:
        return {"available": False, "reason": "该区域无 GSW 覆盖", "coverage_complete": False,
                **_coverage_fields(requested_area, 0.0, 0.0, 0.0)}
    valid_area = total_sample_area = permanent_area = occurrence_10_50_area = 0.0
    valid_count = total_pixel_count = 0
    for values, clip in arrays:
        weights = _pixel_area_weights_sq_km(values.shape, clip)
        total_sample_area += float(weights.sum())
        valid = np.isfinite(values) & (values >= 0) & (values <= 100)
        valid_count += int(valid.sum())
        total_pixel_count += values.size
        valid_area += float(weights[valid].sum())
        permanent_area += float(weights[valid & (values > 50)].sum())
        occurrence_10_50_area += float(weights[valid & (values >= 10) & (values <= 50)].sum())
    fields = _coverage_fields(requested_area, fetched_area, valid_area, total_sample_area,
                              valid_count / total_pixel_count if total_pixel_count else 0.0)
    if valid_count < 16 or valid_area <= 0:
        return {"available": False, "reason": "GSW 有效像元过少，无法形成基线结论", "tiles": tiles,
                "coverage_complete": True, **fields}
    permanent_ratio = permanent_area / valid_area
    seasonal_ratio = occurrence_10_50_area / valid_area
    result = {
        "available": True,
        "tiles": tiles, "tile_count": len(tiles), "coverage_complete": True,
        "tile_note": "已汇总 AOI 触及的全部 GSW 瓦片；比例按地理像元面积加权",
        "permanent_water_ratio": round(permanent_ratio, 4),
        "permanent_water_percent": round(permanent_ratio * 100, 1),
        "seasonal_water_ratio": round(seasonal_ratio, 4),
        "seasonal_water_percent": round(seasonal_ratio * 100, 1),
        "valid_pixel_count": valid_count,
        "metric_definitions": {
            "permanent_water_ratio": "兼容字段：GSW occurrence >50% 的有效面积占比，只表示历史观测出现频率较高，不能证明常年水体。",
            "seasonal_water_ratio": "兼容字段：GSW occurrence 10–50% 的有效面积占比，只表示中等历史出现频率，不能单独证明季节性水体。",
        },
        "attribution": GSW_ATTRIBUTION,
        **fields,
    }
    if ndwi_ratio is not None:
        ndwi_ratio = float(ndwi_ratio)
        result["ndwi_ratio"] = round(ndwi_ratio, 4)
        if ndwi_ratio > permanent_ratio + seasonal_ratio + 0.05:
            conclusion = "当期 NDWI 水体占比明显高于历史基线，提示可能存在新增淹没/洪泛。"
        elif ndwi_ratio < permanent_ratio - 0.05:
            conclusion = "当期 NDWI 水体占比低于历史高出现频率水域占比，提示水面收缩或干旱线索。"
        else:
            conclusion = "当期 NDWI 水体占比与历史基线基本一致，未见显著异常。"
        result["baseline_comparison"] = conclusion
    return result


# ---------------- T5 ESA WorldCover ----------------

def query_landcover_context(bbox, titiler_endpoint=None, size=128, timeout=60):
    """读取 ESA WorldCover 2021 v200 分类栅格，给出各类占地占比 top5。"""
    arrays, tiles, failed = [], [], []
    west, south, east, north = _bbox_corners(bbox)
    requested_area = _area_sq_km(bbox)
    fetched_area = 0.0
    for lon0, lat0 in _intersecting_tiles(bbox, 3):
        url = WORLDCOVER_URL.format(lat_token=_hemi_token(lat0, "N", "S", pad=2), lon_token=_hemi_token(lon0, "E", "W", pad=3))
        clip = {"min_lng": max(west, lon0), "max_lng": min(east, lon0 + 3), "min_lat": max(south, lat0), "max_lat": min(north, lat0 + 3)}
        try:
            arrays.append((_fetch_palette_values(url, clip, titiler_endpoint, size=size, timeout=timeout), clip))
            tiles.append(url.rsplit("/", 1)[-1])
            fetched_area += _area_sq_km(clip)
        except requests.exceptions.RequestException as exc:
            failed.append({"tile": url.rsplit("/", 1)[-1], "error": str(exc) or type(exc).__name__})
    if failed:
        fields = _coverage_fields(requested_area, fetched_area, 0.0, 0.0)
        return {"available": False, "reason": "WorldCover 未能读取 AOI 的全部瓦片，不能输出完整区域统计", "tiles": tiles,
                "failed_tiles": failed, "coverage_complete": False, **fields}
    valid_area = total_sample_area = 0.0
    valid_count = total_pixel_count = 0
    tally = {}
    known_codes = np.array(tuple(WORLDCOVER_CLASSES))
    for values, clip in arrays:
        weights = _pixel_area_weights_sq_km(values.shape, clip)
        total_sample_area += float(weights.sum())
        # Exclude palette-unmapped nodata and every code not defined by v200.
        valid = np.isfinite(values) & np.isin(values, known_codes)
        valid_count += int(valid.sum())
        total_pixel_count += values.size
        valid_area += float(weights[valid].sum())
        for code, name in WORLDCOVER_CLASSES.items():
            area = float(weights[valid & (values == code)].sum())
            if area:
                tally[name] = tally.get(name, 0.0) + area
    fields = _coverage_fields(requested_area, fetched_area, valid_area, total_sample_area,
                              valid_count / total_pixel_count if total_pixel_count else 0.0)
    if valid_count < 16 or valid_area <= 0:
        return {"available": False, "reason": "WorldCover 有效像元过少", "tiles": tiles,
                "coverage_complete": True, **fields}
    tally = {name: area / valid_area for name, area in tally.items()}
    top = sorted(tally.items(), key=lambda item: item[1], reverse=True)[:5]
    return {
        "available": True,
        "tiles": tiles, "tile_count": len(tiles), "coverage_complete": True,
        "tile_note": "已汇总 AOI 触及的全部 WorldCover 瓦片；比例按地理像元面积加权",
        "landcover_top": [{"class": name, "ratio": round(ratio, 4), "percent": round(ratio * 100, 1)} for name, ratio in top],
        "valid_pixel_count": valid_count,
        "attribution": WORLDCOVER_ATTRIBUTION,
        **fields,
    }
