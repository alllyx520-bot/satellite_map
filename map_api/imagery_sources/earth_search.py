from datetime import datetime, timezone
import os
import time

import requests

from .base import ImageryCandidate, ImageryProvider
from ..utils.service_health import check_service, record_failure, record_success, service_key


EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
TITILER_URL = "https://titiler.xyz"
DEFAULT_COLLECTION = "sentinel-2-l2a"
EARTH_SEARCH_LIMITATIONS = (
    "Element84 Earth Search 是公开 STAC 检索服务，可提供 Sentinel-2 等影像的拍摄时间、"
    "云量、产品编号和资产链接；适合做时效筛查和变化线索发现。该服务与公开卫星产品"
    "本身不等同于本地行政证据链，且空间分辨率、云影、重访周期和公开服务可用性会限制"
    "结论可靠性，不应单独作为执法或行政裁量证据。"
)


def request_proxies():
    """默认遵循当前进程代理；仅显式要求时才强制直连。"""
    direct = os.environ.get("SATELLITESENSE_DIRECT_HTTP", "").strip().lower()
    return {"http": None, "https": None} if direct in {"1", "true", "yes"} else None


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


def stac_datetime_range(start_date=None, end_date=None):
    def normalize(value, is_end=False):
        if not value:
            return ".."
        text = str(value).strip()
        if not text:
            return ".."
        if "T" in text:
            return text if text.endswith("Z") or "+" in text else f"{text}Z"
        suffix = "T23:59:59Z" if is_end else "T00:00:00Z"
        return f"{text}{suffix}"

    if not start_date and not end_date:
        return None
    return f"{normalize(start_date)}/{normalize(end_date, is_end=True)}"


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

    def __init__(self, endpoint=EARTH_SEARCH_URL, titiler_endpoint=TITILER_URL, timeout=20):
        self.endpoint = endpoint
        self.titiler_endpoint = (titiler_endpoint or TITILER_URL).rstrip("/")
        self.timeout = timeout

    def search(self, bbox, start_date=None, end_date=None, max_cloud=30, limit=10, collection=DEFAULT_COLLECTION):
        health_key = service_key("earth-search", self.endpoint)
        check_service(health_key)
        payload = {
            "collections": [collection],
            "bbox": [bbox["min_lng"], bbox["min_lat"], bbox["max_lng"], bbox["max_lat"]],
            "limit": limit,
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }
        datetime_range = stac_datetime_range(start_date, end_date)
        if datetime_range:
            payload["datetime"] = datetime_range
        if max_cloud is not None:
            payload["query"] = {"eo:cloud_cover": {"lte": float(max_cloud)}}

        retries = max(0, min(3, int(os.environ.get("EARTH_SEARCH_RETRIES", "2"))))
        response = None
        for attempt in range(retries + 1):
            try:
                response = requests.post(
                    self.endpoint,
                    json=payload,
                    timeout=self.timeout,
                    proxies=request_proxies(),
                )
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= retries:
                    record_failure(
                        health_key,
                        exc,
                        threshold=int(os.environ.get("EARTH_SEARCH_CIRCUIT_FAILURES", "2")),
                        cooldown_seconds=int(os.environ.get("EARTH_SEARCH_CIRCUIT_SECONDS", "30")),
                    )
                    raise
                time.sleep(min(2 ** attempt, 4))
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            try:
                status = int(getattr(response, "status_code", 0) or 0)
                server_error = status >= 500 or status in (408, 425, 429)
            except (TypeError, ValueError):
                server_error = False
            if server_error:
                record_failure(
                    health_key,
                    exc,
                    threshold=int(os.environ.get("EARTH_SEARCH_CIRCUIT_FAILURES", "2")),
                    cooldown_seconds=int(os.environ.get("EARTH_SEARCH_CIRCUIT_SECONDS", "30")),
                )
            raise
        try:
            data = response.json()
        except ValueError as exc:
            record_failure(
                health_key,
                exc,
                threshold=int(os.environ.get("EARTH_SEARCH_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("EARTH_SEARCH_CIRCUIT_SECONDS", "30")),
            )
            raise ValueError("Earth Search 返回了无效 JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("features"), list):
            error = ValueError("Earth Search 返回了无效 STAC 响应结构")
            record_failure(
                health_key,
                error,
                threshold=int(os.environ.get("EARTH_SEARCH_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("EARTH_SEARCH_CIRCUIT_SECONDS", "30")),
            )
            raise error
        record_success(health_key)
        return [self.candidate_from_item(item) for item in data.get("features", [])]

    def render_candidate_jpeg(self, candidate, bbox, width, height):
        health_key = service_key("titiler", self.titiler_endpoint)
        check_service(health_key)
        visual_asset = candidate.assets.get("visual") or {}
        cog_url = visual_asset.get("href")
        if not cog_url:
            raise ValueError("候选影像缺少可渲染的 true color 资产")
        endpoint = (
            f"{self.titiler_endpoint}/cog/bbox/"
            f"{bbox['min_lng']},{bbox['min_lat']},{bbox['max_lng']},{bbox['max_lat']}/"
            f"{width}x{height}.jpg"
        )
        retries = max(0, min(2, int(os.environ.get("TITILER_RETRIES", "1"))))
        response = None
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    endpoint,
                    params={"url": cog_url},
                    # TiTiler 正常响应通常在秒级；失败时不应让一个候选阻塞整条
                    # Agent 流程一分钟。保留足够的网络余量，但将单候选上限控制在
                    # 20 秒以内，后续候选仍可继续尝试。
                    timeout=max(self.timeout, 20),
                    proxies=request_proxies(),
                )
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= retries:
                    record_failure(
                        health_key,
                        exc,
                        threshold=int(os.environ.get("TITILER_CIRCUIT_FAILURES", "2")),
                        cooldown_seconds=int(os.environ.get("TITILER_CIRCUIT_SECONDS", "30")),
                    )
                    raise
                time.sleep(min(2 ** attempt, 3))
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            try:
                status = int(getattr(response, "status_code", 0) or 0)
                server_error = status >= 500 or status in (408, 425, 429)
            except (TypeError, ValueError):
                server_error = False
            if server_error:
                record_failure(
                    health_key,
                    exc,
                    threshold=int(os.environ.get("TITILER_CIRCUIT_FAILURES", "2")),
                    cooldown_seconds=int(os.environ.get("TITILER_CIRCUIT_SECONDS", "30")),
                )
            raise
        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            record_failure(
                health_key,
                "TiTiler 返回非图片响应",
                threshold=int(os.environ.get("TITILER_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("TITILER_CIRCUIT_SECONDS", "30")),
            )
            raise ValueError("影像渲染服务未返回图片")
        record_success(health_key)
        return response.content

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
