"""2026-09-11 全量实测发现的"源契合度"修复的行为测试。

覆盖四个实测确认的错配:
1. sentinel-2-l2a(Element84 Sen2Cor COG)DN 无 +1000 谐波偏移,元数据 offset=-0.1
   与实际不符 → profile radiometry_override(同源的 c1-l2a 实测带偏移,不覆盖)。
2. 无云量属性的 collection(SAR/DEM)检索不得再带 eo:cloud_cover 过滤。
3. 静态数据集(Cop-DEM)检索不得带 datetime(Earth Search 上是生产发布日期)。
4. SAR/DEM 有效性走 PNG alpha(真实 nodata),不再用亮度启发式误杀暗区。
"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile
from PIL import Image
from django.test import SimpleTestCase

from .imagery_sources.earth_search import (
    COLLECTION_PROFILES, EarthSearchProvider, get_collection_profile,
)
from .utils.agent_tools import fetch_cog_bbox_array


def _tif_bytes(array):
    buf = BytesIO()
    tifffile.imwrite(buf, array)
    return buf.getvalue()


def _png_bytes(mode="LA", alpha_zero_cols=2, size=8):
    img = Image.new(mode, (size, size))
    arr = np.zeros((size, size, 2 if mode == "LA" else 4), dtype=np.uint8)
    arr[..., 0] = 120
    arr[..., 1 if mode == "LA" else 3] = 255
    arr[:, :alpha_zero_cols, -1] = 0
    img = Image.fromarray(arr, mode)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class RadiometryOverrideTests(SimpleTestCase):
    bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}

    def _fetch(self, asset, radiometry=None):
        with patch("map_api.utils.agent_tools.requests.get") as mock_get:
            mock_get.return_value = SimpleNamespace(
                content=_tif_bytes(np.full((8, 8), 1500, dtype=np.uint16)),
                raise_for_status=lambda: None,
            )
            return fetch_cog_bbox_array(asset, self.bbox, "https://titiler.test", radiometry=radiometry)

    def test_l2a_profile_carries_verified_override(self):
        override = get_collection_profile("sentinel-2-l2a").get("radiometry_override")
        self.assertEqual(override, {"scale": 0.0001, "offset": 0.0})

    def test_c1_and_l1c_do_not_inherit_override(self):
        self.assertIsNone(get_collection_profile("sentinel-2-c1-l2a").get("radiometry_override"))
        self.assertIsNone(get_collection_profile("sentinel-2-l1c").get("radiometry_override"))

    def test_override_bypasses_asset_metadata(self):
        asset = {"href": "https://example.test/g.tif",
                 "raster:bands": [{"scale": 0.0001, "offset": -0.1, "nodata": None}]}
        overridden = self._fetch(asset, radiometry=(0.0001, 0.0))
        metadata_driven = self._fetch(asset)
        # DN 1500:覆盖口径 0.15;元数据口径 0.05
        self.assertAlmostEqual(float(overridden[0, 0]), 0.15, places=6)
        self.assertAlmostEqual(float(metadata_driven[0, 0]), 0.05, places=6)


class SearchFilterFitTests(SimpleTestCase):
    bbox = {"min_lng": 108.0, "min_lat": 22.5, "max_lng": 108.5, "max_lat": 23.0}

    def _search_payload(self, collection, **kwargs):
        captured = {}

        def fake_post(url, *, json, timeout, proxies):
            captured.update(json)
            return SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"features": []},
            )

        with patch("map_api.imagery_sources.earth_search.requests.post", side_effect=fake_post):
            EarthSearchProvider().search(self.bbox, start_date="2020-01-01", end_date="2020-12-31",
                                         max_cloud=30, collection=collection, **kwargs)
        return captured

    def test_sar_search_has_no_cloud_filter(self):
        payload = self._search_payload("sentinel-1-grd")
        self.assertNotIn("query", payload)
        self.assertIn("datetime", payload)

    def test_dem_search_skips_datetime_and_cloud(self):
        payload = self._search_payload("cop-dem-glo-30")
        self.assertNotIn("query", payload)
        self.assertNotIn("datetime", payload)

    def test_sentinel2_search_keeps_datetime_and_cloud(self):
        payload = self._search_payload("sentinel-2-l2a")
        self.assertIn("datetime", payload)
        self.assertEqual(payload["query"], {"eo:cloud_cover": {"lte": 30.0}})


class AlphaValidityTests(SimpleTestCase):
    bbox = {"min_lng": 108.0, "min_lat": 22.5, "max_lng": 108.5, "max_lat": 23.0}

    def test_sar_profile_uses_alpha_validity(self):
        profile = get_collection_profile("sentinel-1-grd")
        self.assertEqual(profile.get("valid_check"), "alpha")
        self.assertFalse(profile.get("auto_crop"))
        self.assertEqual(profile.get("render_nodata"), 0)

    def test_alpha_path_returns_ratio_and_jpeg(self):
        candidate = SimpleNamespace(
            collection="sentinel-1-grd",
            assets={"vv": {"href": "https://example.test/vv.tif", "gsd": 10}},
        )
        png = _png_bytes(alpha_zero_cols=2, size=8)  # 8x8,左两列 alpha=0 → 0.75
        provider = EarthSearchProvider(titiler_endpoint="https://titiler.test")
        with patch.object(EarthSearchProvider, "_render_image", return_value=png):
            jpeg, ratio = provider.render_candidate_with_validity(candidate, self.bbox, 8, 8)
        self.assertEqual(ratio, 0.75)
        self.assertEqual(jpeg[:2], b"\xff\xd8")  # JPEG magic

    def test_default_profile_returns_none_ratio(self):
        candidate = SimpleNamespace(
            collection="sentinel-2-l2a",
            assets={"visual": {"href": "https://example.test/visual.tif", "gsd": 10}},
        )
        provider = EarthSearchProvider(titiler_endpoint="https://titiler.test")
        with patch.object(EarthSearchProvider, "render_candidate_jpeg", return_value=b"\xff\xd8jpg") as mock_render:
            result = provider.render_candidate_with_validity(candidate, self.bbox, 8, 8)
        self.assertEqual(result, (b"\xff\xd8jpg", None))
        mock_render.assert_called_once()


class AdaptiveRescaleTests(SimpleTestCase):
    bbox = {"min_lng": 108.0, "min_lat": 22.55, "max_lng": 108.45, "max_lat": 22.95}

    def test_dem_render_uses_bbox_percentile_rescale(self):
        candidate = SimpleNamespace(
            collection="cop-dem-glo-30",
            assets={"data": {"href": "https://example.test/dem.tif", "gsd": 30}},
        )
        captured = {}

        def fake_get(url, *, params, timeout, proxies):
            if url.endswith("/cog/statistics"):
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"b1": {"percentile_2": 87.93, "percentile_98": 558.47}},
                )
            captured.update(params)
            return SimpleNamespace(
                content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"},
                raise_for_status=lambda: None,
            )

        provider = EarthSearchProvider(titiler_endpoint="https://titiler.test")
        with patch("map_api.imagery_sources.earth_search.requests.get", side_effect=fake_get):
            data = provider.render_candidate_jpeg(candidate, self.bbox, 256, 256)
        self.assertEqual(data, b"\xff\xd8jpg")
        self.assertEqual(captured.get("rescale"), "87.93,558.47")
        self.assertEqual(captured.get("colormap_name"), "terrain")

    def test_adaptive_rescale_failure_falls_back_to_no_rescale(self):
        candidate = SimpleNamespace(
            collection="cop-dem-glo-30",
            assets={"data": {"href": "https://example.test/dem.tif", "gsd": 30}},
        )
        captured = {}

        def fake_get(url, *, params, timeout, proxies):
            if url.endswith("/cog/statistics"):
                raise requests_exc.Timeout()
            captured.update(params)
            return SimpleNamespace(
                content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"},
                raise_for_status=lambda: None,
            )

        import requests as requests_exc
        provider = EarthSearchProvider(titiler_endpoint="https://titiler.test")
        with patch("map_api.imagery_sources.earth_search.requests.get", side_effect=fake_get):
            provider.render_candidate_jpeg(candidate, self.bbox, 256, 256)
        self.assertNotIn("rescale", captured)
