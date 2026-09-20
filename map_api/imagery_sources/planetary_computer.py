"""Microsoft Planetary Computer 影像源(M3-3a)。

匿名 SAS 签名:资产 href 是未签名的 blob URL,直接读 403;经
/api/sas/v1/sign?href=... 换签名 URL 后可公开读取(签名有有效期,约 45 分钟)。
Landsat 真彩渲染走后端合成:red/green/blue 分别签名,经 titiler /cog/bbox .tif
取数组,按资产 raster:bands 的 scale/offset 转反射率后本地拉伸合成 RGB JPEG。
(titiler 的多 url 三波段堆叠实测产灰度图,不可用。)
"""
import os
import time
from datetime import datetime
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import numpy as np
import requests
import tifffile
from PIL import Image

from .earth_search import EarthSearchProvider, get_collection_profile
from ..utils.http import request_proxies
from ..utils.service_health import check_service, record_failure, record_success, service_key


PLANETARY_COMPUTER_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PLANETARY_COMPUTER_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
# 签名 URL 有效期较短(约 45 分钟),提前 5 分钟视为过期,避免渲染中途失效。
SIGN_EXPIRY_SKEW_SECONDS = 300
# Landsat L2 反射率 DN 兜底校准系数(优先读资产 raster:bands 的 scale/offset)。
LANDSAT_DN_SCALE = 2.75e-5
LANDSAT_DN_OFFSET = -0.2
LANDSAT_FILL_DN = 0
LANDSAT_SATURATED_DN = 65000


class PlanetaryComputerProvider(EarthSearchProvider):
    source = "planetary_computer"
    circuit_kind = "planetary-computer"
    env_prefix = "PC"

    def candidate_from_item(self, item):
        candidate = super().candidate_from_item(item)
        if candidate and item.get("collection") == "landsat-c2-l2":
            thermal = (item.get("assets") or {}).get("lwir11") or {}
            band = (thermal.get("raster:bands") or [{}])[0]
            # PC calls its Level-2 ST_B10 asset lwir11. Verify product identity,
            # physical units and calibration before exposing temperature tools.
            path = urlparse(thermal.get("href", "")).path.upper()
            if (path.endswith("_ST_B10.TIF") and "temperature" in thermal.get("roles", [])
                    and str(band.get("unit", "")).lower() == "kelvin"
                    and band.get("scale") is not None and band.get("offset") is not None):
                candidate.assets["st_b10"] = dict(thermal)
        return candidate

    def __init__(self, endpoint=None, titiler_endpoint=None, timeout=20, sign_endpoint=None):
        super().__init__(
            endpoint or os.environ.get("PLANETARY_COMPUTER_STAC_URL") or PLANETARY_COMPUTER_STAC_URL,
            titiler_endpoint,
            timeout,
        )
        self.sign_endpoint = (
            sign_endpoint
            or os.environ.get("PLANETARY_COMPUTER_SIGN_URL")
            or PLANETARY_COMPUTER_SIGN_URL
        ).rstrip("/")
        self._sign_cache = {}

    def sign_href(self, href):
        """匿名 SAS 签名;带内存缓存,从签名 URL 的 se 参数解析过期时刻。"""
        if not href:
            raise ValueError("缺少待签名的资产链接")
        cached = self._sign_cache.get(href)
        if cached and cached[1] > time.time():
            return cached[0]
        health_key = service_key(self.circuit_kind, self.sign_endpoint)
        check_service(health_key)
        try:
            response = requests.get(
                self.sign_endpoint,
                params={"href": href},
                timeout=self.timeout,
                proxies=request_proxies(),
            )
            response.raise_for_status()
            signed = (response.json() or {}).get("href")
        except (requests.RequestException, ValueError) as exc:
            record_failure(
                health_key,
                exc,
                threshold=int(os.environ.get("PC_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("PC_CIRCUIT_SECONDS", "30")),
            )
            raise ValueError(f"Planetary Computer SAS 签名失败: {exc}") from exc
        if not signed:
            error = ValueError("Planetary Computer SAS 签名响应缺少 href")
            record_failure(
                health_key,
                error,
                threshold=int(os.environ.get("PC_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("PC_CIRCUIT_SECONDS", "30")),
            )
            raise error
        record_success(health_key)
        self._sign_cache[href] = (signed, self._signed_expiry(signed))
        return signed

    def sign_asset(self, asset):
        """返回 href 已签名的资产字典副本(其余元数据原样保留)。"""
        asset = dict(asset or {})
        asset["href"] = self.sign_href(asset.get("href"))
        return asset

    @staticmethod
    def _signed_expiry(signed_url):
        try:
            raw = (parse_qs(urlparse(signed_url).query).get("se") or [""])[0]
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() - SIGN_EXPIRY_SKEW_SECONDS
        except (ValueError, TypeError, IndexError, OverflowError):
            # 解析不出有效期时保守缓存 10 分钟。
            return time.time() + 600

    def render_candidate_jpeg(self, candidate, bbox, width, height):
        profile = get_collection_profile(getattr(candidate, "collection", "") or "")
        render_spec = profile.get("render") or {}
        if render_spec.get("strategy") != "rgb_compose":
            return super().render_candidate_jpeg(candidate, bbox, width, height)
        keys = list(render_spec.get("assets") or ["red", "green", "blue"])
        stretch = render_spec.get("stretch") or [0.0, 0.3]
        if len(keys) != 3:
            raise ValueError("rgb_compose 渲染必须恰好三个波段资产")
        bands_meta = []
        for key in keys:
            asset = candidate.assets.get(key) or {}
            if not asset.get("href"):
                raise ValueError(f"候选影像缺少可渲染的 {key} 资产")
            bands_meta.append(asset)
        bands = []
        for asset in bands_meta:
            dn = self._fetch_band_array(self.sign_href(asset["href"]), bbox, width, height)
            bands.append(self._dn_to_reflectance(dn, asset))
        low, high = float(stretch[0]), float(stretch[1])
        if not high > low:
            raise ValueError("rgb_compose 拉伸区间无效")
        rgb = np.stack(
            [np.clip((band - low) / (high - low), 0.0, 1.0) for band in bands], axis=-1
        )
        image = Image.fromarray((rgb * 255).round().astype(np.uint8), "RGB")
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=92)
        return buffer.getvalue()

    def _fetch_band_array(self, signed_url, bbox, width, height):
        """经 titiler /cog/bbox .tif 读单波段数组;返回可能是 (h,w,2) 时取 [...,0]。"""
        health_key = service_key("titiler", self.titiler_endpoint)
        check_service(health_key)
        endpoint = (
            f"{self.titiler_endpoint}/cog/bbox/"
            f"{bbox['min_lng']},{bbox['min_lat']},{bbox['max_lng']},{bbox['max_lat']}/"
            f"{width}x{height}.tif"
        )
        retries = max(0, min(2, int(os.environ.get("TITILER_RETRIES", "1"))))
        response = None
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    endpoint,
                    params={"url": signed_url, "bidx": 1, "resampling": "bilinear"},
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
        try:
            with tifffile.TiffFile(BytesIO(response.content)) as image:
                arr = np.asarray(image.pages[0].asarray(), dtype=np.float64)
        except Exception as exc:
            record_failure(
                health_key,
                exc,
                threshold=int(os.environ.get("TITILER_CIRCUIT_FAILURES", "2")),
                cooldown_seconds=int(os.environ.get("TITILER_CIRCUIT_SECONDS", "30")),
            )
            raise ValueError("波段 TIFF 解析失败") from exc
        if arr.ndim == 3:
            arr = arr[..., 0]
        if arr.ndim != 2:
            raise ValueError("波段 TIFF 必须返回单波段栅格")
        record_success(health_key)
        return arr

    @staticmethod
    def _dn_to_reflectance(dn, asset):
        bands = asset.get("raster:bands") or []
        band_meta = bands[0] if bands and isinstance(bands[0], dict) else {}
        scale = float(band_meta.get("scale", LANDSAT_DN_SCALE))
        offset = float(band_meta.get("offset", LANDSAT_DN_OFFSET))
        nodata = band_meta.get("nodata", LANDSAT_FILL_DN)
        # dn==nodata(默认 0) 或 >=65000 为填充/饱和值,必须置黑:
        # 否则白边会骗过 image_valid_ratio/no-data 裁边逻辑。
        fill = ~np.isfinite(dn) | (dn >= LANDSAT_SATURATED_DN)
        if nodata is not None:
            fill |= dn == float(nodata)
        refl = np.where(fill, 0.0, dn * scale + offset)
        return np.where(fill, 0.0, refl)
