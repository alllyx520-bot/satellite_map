"""Explicit place lookup with WGS84 coordinates and provider provenance."""
import hashlib
import math
import re

import requests
from django.core.cache import cache

from ..utils.http import request_proxies

ENDPOINT = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"


def search(query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError("请输入不超过 200 字的地点或经纬度")
    query = query.strip()
    coordinate = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*[,，\s]\s*([-+]?\d+(?:\.\d+)?)", query)
    if coordinate:
        longitude, latitude = map(float, coordinate.groups())
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("经度应在 -180～180，纬度应在 -90～90")
        return {"items": [{"name": query, "lon": longitude, "lat": latitude,
            "source": "user_coordinates", "crs": "EPSG:4326", "precision": "point"}]}
    key = "v3:place:" + hashlib.sha256(query.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.get(ENDPOINT, params={"SingleLine": query, "f": "json", "outSR": 4326,
            "outFields": "Match_addr,Addr_type", "maxLocations": 5}, timeout=(5, 12), proxies=request_proxies())
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise ValueError("地点服务暂时不可用，可输入经度和纬度继续")
        items = []
        for candidate in data.get("candidates", []):
            location = candidate.get("location") or {}
            lon, lat = float(location["x"]), float(location["y"])
            if not math.isfinite(lon) or not math.isfinite(lat) or abs(lon) > 180 or abs(lat) > 90:
                continue
            items.append({"name": candidate.get("address", query), "lon": lon, "lat": lat,
                "score": candidate.get("score"), "source": "Esri World Geocoding", "crs": "EPSG:4326",
                "precision": "place_match", "extent": candidate.get("extent"),
                "limitation": "地名匹配结果需结合影像核对；返回范围不代表行政边界"})
    except (requests.RequestException, KeyError, TypeError, ValueError):
        # GeoNames-backed city lookup is independently available in regions
        # where the detailed Esri endpoint cannot be reached.
        try:
            response = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                params={"name": query, "count": 5, "language": "zh", "format": "json"},
                timeout=(5, 10), proxies=request_proxies())
            response.raise_for_status()
            items = [{"name": " · ".join(str(row[k]) for k in ("name", "admin1", "country") if row.get(k)),
                "lon": row["longitude"], "lat": row["latitude"], "source": "Open-Meteo / GeoNames",
                "crs": "EPSG:4326", "precision": "settlement_point",
                "limitation": "城市或聚落中心；不是港区设施定位或行政边界"}
                for row in response.json().get("results", [])]
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            raise ValueError("地点服务连接失败，可输入经度和纬度继续") from exc
    result = {"items": items}
    cache.set(key, result, 3600)
    return result


def tool(args, context):
    return search(args["query"])
