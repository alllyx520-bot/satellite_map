from datetime import datetime, timezone

import requests

from .base import ImageryCandidate, ImageryProvider


EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
DEFAULT_COLLECTION = "sentinel-2-l2a"
EARTH_SEARCH_LIMITATIONS = (
    "Element84 Earth Search 是公开 STAC 检索服务，可提供 Sentinel-2 等影像的拍摄时间、"
    "云量、产品编号和资产链接；适合做时效筛查和变化线索发现。该服务与公开卫星产品"
    "本身不等同于本地行政证据链，且空间分辨率、云影、重访周期和公开服务可用性会限制"
    "结论可靠性，不应单独作为执法或行政裁量证据。"
)


def parse_stac_datetime(value):
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def score_candidate(acquired_at, cloud_percent, gsd_m, has_product_id=True):
    score = 0
    reasons = []
    now = datetime.now(timezone.utc)

    if acquired_at:
        age_days = max(0, (now - acquired_at.astimezone(timezone.utc)).days)
        if age_days <= 7:
            score += 35
            reasons.append("拍摄时间在 7 天内，时效性很好")
        elif age_days <= 30:
            score += 28
            reasons.append("拍摄时间在 30 天内，适合近期筛查")
        elif age_days <= 90:
            score += 18
            reasons.append("拍摄时间在 90 天内，可用于阶段性参考")
        else:
            score += 8
            reasons.append("拍摄时间较旧，只适合背景参考")
    else:
        reasons.append("缺少明确拍摄时间")

    if cloud_percent is None:
        reasons.append("缺少云量指标")
    elif cloud_percent <= 10:
        score += 25
        reasons.append("云量低于 10%，可视条件较好")
    elif cloud_percent <= 30:
        score += 17
        reasons.append("云量低于 30%，可用于一般筛查")
    elif cloud_percent <= 60:
        score += 8
        reasons.append("云量偏高，需谨慎解译")
    else:
        reasons.append("云量很高，不建议用于细节判断")

    if gsd_m and gsd_m <= 1:
        score += 25
        reasons.append("空间分辨率达到米级，细节能力较好")
    elif gsd_m and gsd_m <= 10:
        score += 18
        reasons.append("空间分辨率约 10 米，适合中尺度变化筛查")
    elif gsd_m:
        score += 8
        reasons.append("空间分辨率较粗，只适合宏观判断")
    else:
        reasons.append("缺少空间分辨率信息")

    if has_product_id:
        score += 15
        reasons.append("包含可复核产品编号")
    else:
        reasons.append("缺少可复核产品编号")

    return min(score, 100), reasons


def decision_grade_for_score(score):
    if score >= 45:
        return "screening"
    return "reference"


class EarthSearchProvider(ImageryProvider):
    source = "earth_search"

    def __init__(self, endpoint=EARTH_SEARCH_URL, timeout=20):
        self.endpoint = endpoint
        self.timeout = timeout

    def search(self, bbox, start_date=None, end_date=None, max_cloud=30, limit=10, collection=DEFAULT_COLLECTION):
        payload = {
            "collections": [collection],
            "bbox": [bbox["min_lng"], bbox["min_lat"], bbox["max_lng"], bbox["max_lat"]],
            "limit": limit,
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }
        if start_date or end_date:
            payload["datetime"] = f"{start_date or '..'}/{end_date or '..'}"
        if max_cloud is not None:
            payload["query"] = {"eo:cloud_cover": {"lte": float(max_cloud)}}

        response = requests.post(
            self.endpoint,
            json=payload,
            timeout=self.timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        data = response.json()
        return [self.candidate_from_item(item) for item in data.get("features", [])]

    def candidate_from_item(self, item):
        properties = item.get("properties") or {}
        item_id = item.get("id", "")
        collection = item.get("collection", "")
        acquired_at = parse_stac_datetime(properties.get("datetime"))
        published_at = parse_stac_datetime(properties.get("updated") or properties.get("created"))
        product_id = properties.get("s2:product_uri") or item_id
        cloud_percent = properties.get("eo:cloud_cover")
        gsd_m = self._best_gsd(item.get("assets") or {})
        score, reasons = score_candidate(acquired_at, cloud_percent, gsd_m, bool(product_id))
        decision_grade = decision_grade_for_score(score)
        min_lng, min_lat, max_lng, max_lat = item.get("bbox") or [None, None, None, None]

        return ImageryCandidate(
            source=self.source,
            source_label="Element84 Earth Search / Sentinel-2 L2A",
            collection=collection,
            item_id=item_id,
            product_id=product_id,
            acquired_at=acquired_at,
            published_at=published_at,
            bbox={
                "min_lng": min_lng,
                "min_lat": min_lat,
                "max_lng": max_lng,
                "max_lat": max_lat,
            },
            gsd_m=gsd_m,
            cloud_percent=cloud_percent,
            processing_level="sentinel-2-l2a",
            license_type="sentinel_data_terms",
            decision_grade=decision_grade,
            suitability_score=score,
            score_reasons=reasons,
            limitations=EARTH_SEARCH_LIMITATIONS,
            assets=self._asset_links(item.get("assets") or {}),
            links=self._links(item.get("links") or []),
            metadata={
                "platform": properties.get("platform"),
                "constellation": properties.get("constellation"),
                "instruments": properties.get("instruments"),
                "processing_baseline": properties.get("s2:processing_baseline"),
                "proj_epsg": properties.get("proj:epsg"),
                "stac_version": item.get("stac_version"),
            },
        )

    def _best_gsd(self, assets):
        for key in ("visual", "red", "green", "blue"):
            asset = assets.get(key) or {}
            if asset.get("gsd"):
                return float(asset["gsd"])
            bands = asset.get("raster:bands") or []
            if bands and bands[0].get("spatial_resolution"):
                return float(bands[0]["spatial_resolution"])
        return 10.0

    def _asset_links(self, assets):
        result = {}
        for key in ("visual", "thumbnail", "red", "green", "blue", "nir", "scl"):
            asset = assets.get(key)
            if asset and asset.get("href"):
                result[key] = {
                    "href": asset.get("href"),
                    "type": asset.get("type", ""),
                    "title": asset.get("title", ""),
                    "roles": asset.get("roles", []),
                }
        return result

    def _links(self, links):
        result = {}
        for link in links:
            rel = link.get("rel")
            href = link.get("href")
            if rel and href and rel in ("self", "license", "canonical", "collection"):
                result[rel] = href
        return result
