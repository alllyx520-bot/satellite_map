from datetime import datetime, timezone
import os
import time

import requests

from .base import ImageryCandidate, ImageryProvider
from ..utils.http import request_proxies
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

# 数据源扩展 M0:collection profile 表驱动。candidate/渲染/落库元数据全部从这里取,
# 新增 collection 只改表,不改管线代码。experimental=True 的占位 profile 仅打通机制,
# 渲染参数(titiler_params)留待 M1 校准,不作为正式数据源开放。
SENTINEL2_L2A_PROFILE = {
    "source": "sentinel2",
    "source_label": "Sentinel-2 L2A",
    "source_label_mosaic": "Sentinel-2 L2A 多景拼接",
    "processing_level": "sentinel-2-l2a",
    "license_type": "sentinel_data_terms",
    "limitations": EARTH_SEARCH_LIMITATIONS,
    "decision_grade": "reference",
    "product_id_keys": ["s2:product_uri"],
    "cloud_property": "eo:cloud_cover",
    "asset_keys": ["visual", "thumbnail", "red", "green", "blue", "nir", "swir16", "swir22", "scl"],
    "gsd_keys": ["visual", "red", "green", "blue"],
    "gsd_default": 10.0,
    "render": {"asset": "visual"},
    "qa_band": "scl",
    "native_resolution_m": 10,
    "experimental": False,
}

COLLECTION_PROFILES = {
    "sentinel-2-l2a": SENTINEL2_L2A_PROFILE,
    "sentinel-2-c1-l2a": {
        **SENTINEL2_L2A_PROFILE,
        "processing_level": "sentinel-2-c1-l2a",
        "experimental": False,
    },
    # L1C 没有 scl QA 波段,只作 L2A 无覆盖时的时效兜底;保持 experimental 不对外正式开放。
    "sentinel-2-l1c": {
        **SENTINEL2_L2A_PROFILE,
        "processing_level": "sentinel-2-l1c",
        "qa_band": None,
        "experimental": True,
    },
    "sentinel-1-grd": {
        "source": "sentinel1",
        "source_label": "Sentinel-1 GRD",
        "source_label_mosaic": "Sentinel-1 GRD 多景拼接",
        "processing_level": "sentinel-1-grd",
        "license_type": "sentinel_data_terms",
        "limitations": (
            "Sentinel-1 GRD 是 C 波段 SAR 后向散射产品，反映地表介电与粗糙度特征，"
            "不是光学真彩色影像；可全天候获取、适合水体/洪涝与地表变化线索，"
            "但斑点噪声、几何畸变和入射角差异会限制直接目视解译，"
            "不应单独作为行政裁量证据。"
        ),
        "decision_grade": "reference",
        "product_id_keys": [],
        "cloud_property": None,
        "asset_keys": ["vv", "vh", "thumbnail"],
        "gsd_keys": ["vv", "vh"],
        "gsd_default": 10.0,
        "render": {"asset": "vv", "titiler_params": {"bidx": 1, "rescale": "0,400"}},
        "native_resolution_m": 10,
        "experimental": False,
    },
    "cop-dem-glo-30": {
        "source": "copdem",
        "source_label": "Copernicus DEM GLO-30",
        "source_label_mosaic": "Copernicus DEM GLO-30 多景拼接",
        "processing_level": "cop-dem-glo-30",
        "license_type": "copernicus_dem_license",
        "limitations": (
            "Copernicus DEM GLO-30 是静态数字高程模型（采集基线为 TanDEM-X 2011-2015），"
            "不反映拍摄时刻的地表状态；适合地形/坡度分析和水文背景判断，"
            "不能用于任何时相变化监测或执法证据。"
        ),
        "decision_grade": "reference",
        "product_id_keys": [],
        "cloud_property": None,
        "asset_keys": ["data", "thumbnail", "preview"],
        "gsd_keys": ["data"],
        "gsd_default": 30.0,
        "render": {"asset": "data", "titiler_params": {"bidx": 1, "colormap_name": "terrain", "rescale": "0,2000"}},
        "native_resolution_m": 30,
        "experimental": False,
    },
    "landsat-c2-l2": {
        "source": "landsat",
        "source_label": "Landsat Collection 2 Level-2",
        "source_label_mosaic": "Landsat Collection 2 Level-2 多景拼接",
        "processing_level": "landsat-c2-l2",
        "license_type": "usgs_landsat_terms",
        "limitations": (
            "Landsat Collection 2 Level-2 是 USGS 地表反射率产品，空间分辨率约 30 米，"
            "重访周期 16 天；适合宏观变化与历史回溯，细节能力低于 Sentinel-2。"
            "其资产存储在 usgs-landsat requester-pays 桶，当前部署的渲染服务无法匿名读取，"
            "需要自带凭证的渲染链路支持。"
        ),
        "decision_grade": "reference",
        "product_id_keys": ["landsat:product_id", "landsat:scene_id"],
        "cloud_property": "eo:cloud_cover",
        "asset_keys": ["red", "green", "blue", "nir08", "swir16", "qa_pixel", "thumbnail"],
        "gsd_keys": ["red", "green", "blue"],
        "gsd_default": 30.0,
        "render": {"asset": "red"},
        "experimental": True,
    },
}


def get_collection_profile(collection):
    """返回 collection 对应 profile；未知 collection 回退到默认 profile。"""
    return COLLECTION_PROFILES.get(collection, COLLECTION_PROFILES[DEFAULT_COLLECTION])


# 已知公开匿名可读的 S3 桶区域表(2026-09-10 实测 Earth Search v1)。
PUBLIC_S3_BUCKET_REGIONS = {
    "sentinel-s1-l1c": "eu-central-1",
    "copernicus-dem-30m": "eu-central-1",
    "copernicus-dem-90m": "eu-central-1",
}
# requester-pays 桶匿名 403,当前部署的渲染链路不支持。
REQUESTER_PAYS_BUCKETS = {"usgs-landsat"}
REQUESTER_PAYS_RENDER_ERROR = "该数据源需要自带凭证的渲染服务，当前部署暂不支持"


def resolve_asset_href(href):
    """把 STAC 资产 href 转成渲染服务可访问的 https URL。

    已知公开桶按区域表转换；requester-pays 桶直接报错交给上层降级；
    未知 s3:// 桶按 us-west-2 默认转换(未验证,由调用方在元数据标注)。
    """
    if not href or not str(href).startswith("s3://"):
        return href
    rest = str(href)[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if bucket in REQUESTER_PAYS_BUCKETS:
        raise ValueError(REQUESTER_PAYS_RENDER_ERROR)
    region = PUBLIC_S3_BUCKET_REGIONS.get(bucket, "us-west-2")
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def s3_bucket_unverified(href):
    """s3:// 且不在已知桶表内(公开/付费都已知)时返回 True,用于元数据标注。"""
    if not href or not str(href).startswith("s3://"):
        return False
    bucket = str(href)[len("s3://"):].partition("/")[0]
    return bucket not in PUBLIC_S3_BUCKET_REGIONS and bucket not in REQUESTER_PAYS_BUCKETS


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


def score_candidate(acquired_at, cloud_percent, gsd_m, has_product_id=True, cloud_exempt=False):
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

    if cloud_exempt:
        # SAR/DEM 等无云量概念的 collection:云量项不参与评分,不能让这些源
        # 因"缺少云量指标"恒被扣分垫底。
        reasons.append("该数据源为 SAR/DEM 类型，不受云量影响，云量项不参与评分")
    elif cloud_percent is None:
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
        profile = get_collection_profile(getattr(candidate, "collection", "") or "")
        render_spec = profile.get("render") or {}
        render_asset_key = render_spec.get("asset") or "visual"
        render_asset = candidate.assets.get(render_asset_key) or {}
        # s3:// href 经 resolve_asset_href 转换;requester-pays 桶在此抛 ValueError,
        # 由 sentinel_retrieval_result 的既有 render_errors 机制降级到下一候选。
        cog_url = resolve_asset_href(render_asset.get("href"))
        if not cog_url:
            raise ValueError(f"候选影像缺少可渲染的 {render_asset_key} 资产")
        titiler_params = {"url": cog_url}
        titiler_params.update(render_spec.get("titiler_params") or {})
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
                    params=titiler_params,
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
        profile = get_collection_profile(collection)
        acquired_at = parse_stac_datetime(properties.get("datetime"))
        published_at = parse_stac_datetime(properties.get("updated") or properties.get("created"))
        product_id = next(
            (properties.get(key) for key in profile["product_id_keys"] if properties.get(key)),
            None,
        ) or item_id
        cloud_property = profile.get("cloud_property")
        cloud_percent = properties.get(cloud_property) if cloud_property else None
        assets = self._asset_links(item.get("assets") or {}, profile)
        gsd_m = self._best_gsd(item.get("assets") or {}, profile)
        score, reasons = score_candidate(
            acquired_at, cloud_percent, gsd_m, bool(product_id),
            cloud_exempt=cloud_property is None,
        )
        decision_grade = "screening" if score >= 45 else profile.get("decision_grade", "reference")
        min_lng, min_lat, max_lng, max_lat = item.get("bbox") or [None, None, None, None]

        metadata = {
            "platform": properties.get("platform"),
            "constellation": properties.get("constellation"),
            "instruments": properties.get("instruments"),
            "processing_baseline": properties.get("s2:processing_baseline"),
            "proj_epsg": properties.get("proj:epsg"),
            "stac_version": item.get("stac_version"),
        }
        if profile.get("experimental"):
            metadata["experimental_collection"] = True
        unverified_buckets = sorted({
            str(asset.get("href"))[len("s3://"):].partition("/")[0]
            for asset in assets.values()
            if s3_bucket_unverified(asset.get("href"))
        })
        if unverified_buckets:
            # 未知 s3:// 桶默认按 us-west-2 转换,可用性未经实测,显式标注。
            metadata["unverified_s3_buckets"] = unverified_buckets
            reasons.append("资产位于未验证的 S3 桶（默认按 us-west-2 解析，可用性未实测）")

        return ImageryCandidate(
            source=self.source,
            source_label=f"Element84 Earth Search / {profile['source_label']}",
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
            processing_level=profile["processing_level"],
            license_type=profile["license_type"],
            decision_grade=decision_grade,
            suitability_score=score,
            score_reasons=reasons,
            limitations=profile["limitations"],
            assets=assets,
            links=self._links(item.get("links") or []),
            metadata=metadata,
        )

    def _best_gsd(self, assets, profile=None):
        profile = profile or COLLECTION_PROFILES[DEFAULT_COLLECTION]
        for key in profile["gsd_keys"]:
            asset = assets.get(key) or {}
            if asset.get("gsd"):
                return float(asset["gsd"])
            bands = asset.get("raster:bands") or []
            if bands and bands[0].get("spatial_resolution"):
                return float(bands[0]["spatial_resolution"])
        return profile["gsd_default"]

    def _asset_links(self, assets, profile=None):
        profile = profile or COLLECTION_PROFILES[DEFAULT_COLLECTION]
        result = {}
        for key in profile["asset_keys"]:
            asset = assets.get(key)
            if asset and asset.get("href"):
                result[key] = {
                    "href": asset.get("href"),
                    "type": asset.get("type", ""),
                    "title": asset.get("title", ""),
                    "roles": asset.get("roles", []),
                    "gsd": asset.get("gsd"),
                    "raster:bands": asset.get("raster:bands") or [],
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
