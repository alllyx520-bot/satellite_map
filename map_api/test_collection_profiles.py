"""M0 数据源扩展:COLLECTION_PROFILES 表驱动改造的行为测试。"""
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from .imagery_sources import get_provider
from .imagery_sources.earth_search import (
    COLLECTION_PROFILES, DEFAULT_COLLECTION, EARTH_SEARCH_LIMITATIONS,
    EarthSearchProvider, get_collection_profile, resolve_asset_href,
    score_candidate,
)


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "earth_search_s2_l2a_item.json"
)


def load_fixture_item():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


class Sentinel2L2AProfileRegressionTests(SimpleTestCase):
    """sentinel-2-l2a 是唯一正式 profile,行为必须与改造前逐字段一致。

    夹具为 2026-09-10 从 Earth Search v1 实抓的真实 item
    (S2A_48QZK_20260903_0_L2A,已裁剪到 profile 资产白名单)。
    """

    def setUp(self):
        self.item = load_fixture_item()
        self.candidate = EarthSearchProvider().candidate_from_item(self.item)

    def test_fixture_item_maps_to_existing_candidate_shape(self):
        c = self.candidate
        self.assertEqual(c.source, "earth_search")
        self.assertEqual(c.source_label, "Element84 Earth Search / Sentinel-2 L2A")
        self.assertEqual(c.collection, "sentinel-2-l2a")
        self.assertEqual(c.item_id, "S2A_48QZK_20260903_0_L2A")
        self.assertEqual(
            c.product_id,
            "S2A_MSIL2A_20260903T032151_N0512_R118_T48QZK_20260903T075409.SAFE",
        )
        self.assertEqual(
            c.acquired_at,
            datetime(2026, 9, 3, 3, 32, 11, 872000, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(c.cloud_percent, 11.671656)
        self.assertEqual(c.gsd_m, 10.0)
        self.assertEqual(c.processing_level, "sentinel-2-l2a")
        self.assertEqual(c.license_type, "sentinel_data_terms")
        self.assertEqual(c.decision_grade, "screening")
        self.assertEqual(c.limitations, EARTH_SEARCH_LIMITATIONS)
        self.assertEqual(
            c.bbox,
            {"min_lng": 107.89704, "min_lat": 21.56803,
             "max_lng": 108.983413, "max_lat": 22.580521},
        )
        self.assertEqual(
            set(c.assets),
            {"visual", "thumbnail", "red", "green", "blue", "nir", "swir16", "swir22", "scl"},
        )
        self.assertTrue(c.assets["visual"]["href"].startswith(
            "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"
        ))
        self.assertEqual(c.assets["visual"]["gsd"], 10)
        self.assertEqual(c.metadata["platform"], "sentinel-2a")
        self.assertEqual(c.metadata["processing_baseline"], "05.12")
        self.assertNotIn("experimental_collection", c.metadata)
        self.assertFalse(any("云量" in r and "不参与" in r for r in c.score_reasons))

    def test_unknown_collection_falls_back_to_default_profile(self):
        item = dict(self.item)
        item["collection"] = "some-future-collection"
        candidate = EarthSearchProvider().candidate_from_item(item)
        self.assertEqual(candidate.collection, "some-future-collection")
        self.assertEqual(candidate.source_label, "Element84 Earth Search / Sentinel-2 L2A")
        self.assertEqual(candidate.product_id, self.candidate.product_id)

    def test_product_id_falls_back_to_item_id(self):
        item = json.loads(json.dumps(self.item))
        del item["properties"]["s2:product_uri"]
        candidate = EarthSearchProvider().candidate_from_item(item)
        self.assertEqual(candidate.product_id, candidate.item_id)


class ResolveAssetHrefTests(SimpleTestCase):
    def test_known_public_buckets_use_their_region(self):
        self.assertEqual(
            resolve_asset_href("s3://sentinel-s1-l1c/GRD/2026/1/1/x/measurement/iw-vv.tif"),
            "https://sentinel-s1-l1c.s3.eu-central-1.amazonaws.com/GRD/2026/1/1/x/measurement/iw-vv.tif",
        )
        self.assertEqual(
            resolve_asset_href("s3://copernicus-dem-30m/Copernicus_DSM_COG_10_N00_00_E006_00_DEM/Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif"),
            "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/Copernicus_DSM_COG_10_N00_00_E006_00_DEM/Copernicus_DSM_COG_10_N00_00_E006_00_DEM.tif",
        )
        self.assertEqual(
            resolve_asset_href("s3://copernicus-dem-90m/a/b.tif"),
            "https://copernicus-dem-90m.s3.eu-central-1.amazonaws.com/a/b.tif",
        )

    def test_requester_pays_bucket_raises_for_existing_render_degradation(self):
        with self.assertRaisesRegex(ValueError, "需要自带凭证"):
            resolve_asset_href("s3://usgs-landsat/collection02/level-2/x.tif")

    def test_unknown_bucket_defaults_to_us_west_2(self):
        self.assertEqual(
            resolve_asset_href("s3://new-bucket/path/x.tif"),
            "https://new-bucket.s3.us-west-2.amazonaws.com/path/x.tif",
        )

    def test_https_href_passes_through(self):
        url = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/x.tif"
        self.assertEqual(resolve_asset_href(url), url)


class NonVisualRenderTests(SimpleTestCase):
    bbox = {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.12, "max_lat": 22.12}

    def sar_candidate(self):
        return EarthSearchProvider().candidate_from_item({
            "id": "S1A_GRD_SMOKE",
            "collection": "sentinel-1-grd",
            "bbox": [108.1, 22.1, 108.12, 22.12],
            "properties": {"datetime": "2026-09-01T10:00:00Z"},
            "assets": {
                "vv": {
                    "href": "s3://sentinel-s1-l1c/GRD/2026/9/1/IW/DV/S1A_IW_GRDH_1SDV_x/measurement/iw-vv.tif",
                    "gsd": 10,
                },
            },
        })

    def test_sar_render_uses_profile_asset_and_titiler_params(self):
        candidate = self.sar_candidate()
        self.assertNotIn("visual", candidate.assets)
        captured = {}

        def fake_get(endpoint, *, params, **kwargs):
            captured["endpoint"] = endpoint
            captured["params"] = params
            return SimpleNamespace(
                content=b"\xff\xd8jpeg",
                headers={"content-type": "image/jpeg"},
                raise_for_status=lambda: None,
            )

        with patch("map_api.imagery_sources.earth_search.requests.get", side_effect=fake_get):
            data = EarthSearchProvider(titiler_endpoint="https://titiler.test").render_candidate_jpeg(
                candidate, self.bbox, 256, 256
            )
        self.assertEqual(data, b"\xff\xd8jpeg")
        self.assertIn("https://titiler.test/cog/bbox/108.1,22.1,108.12,22.12/256x256.jpg", captured["endpoint"])
        self.assertEqual(
            captured["params"]["url"],
            "https://sentinel-s1-l1c.s3.eu-central-1.amazonaws.com/GRD/2026/9/1/IW/DV/S1A_IW_GRDH_1SDV_x/measurement/iw-vv.tif",
        )
        self.assertEqual(captured["params"]["bidx"], 1)
        self.assertEqual(captured["params"]["rescale"], "0,400")

    def test_sentinel2_render_has_no_titiler_params(self):
        candidate = EarthSearchProvider().candidate_from_item(load_fixture_item())
        captured = {}

        def fake_get(endpoint, *, params, **kwargs):
            captured["params"] = params
            return SimpleNamespace(
                content=b"\xff\xd8jpeg",
                headers={"content-type": "image/jpeg"},
                raise_for_status=lambda: None,
            )

        with patch("map_api.imagery_sources.earth_search.requests.get", side_effect=fake_get):
            EarthSearchProvider(titiler_endpoint="https://titiler.test").render_candidate_jpeg(
                candidate, self.bbox, 256, 256
            )
        self.assertEqual(set(captured["params"]), {"url"})

    def test_requester_pays_render_error_reaches_render_errors_channel(self):
        candidate = EarthSearchProvider().candidate_from_item({
            "id": "LC08_SMOKE",
            "collection": "landsat-c2-l2",
            "bbox": [108.1, 22.1, 108.12, 22.12],
            "properties": {"datetime": "2026-09-01T03:00:00Z", "eo:cloud_cover": 5},
            "assets": {"red": {"href": "s3://usgs-landsat/collection02/level-2/x_B4.TIF", "gsd": 30}},
        })
        with self.assertRaisesRegex(ValueError, "需要自带凭证"):
            EarthSearchProvider().render_candidate_jpeg(candidate, self.bbox, 256, 256)


class CloudExemptScoreTests(SimpleTestCase):
    def test_sar_dem_collections_skip_cloud_penalty(self):
        profile = get_collection_profile("sentinel-1-grd")
        self.assertIsNone(profile["cloud_property"])
        score, reasons = score_candidate(
            datetime(2026, 9, 5, tzinfo=timezone.utc), None, 10.0,
            has_product_id=True, cloud_exempt=True,
        )
        self.assertNotIn("缺少云量指标", reasons)
        self.assertTrue(any("不受云量影响" in r for r in reasons))
        # 同条件下无云量豁免会少 25 分且带"缺少云量指标"提示;豁免不扣分。
        baseline, baseline_reasons = score_candidate(
            datetime(2026, 9, 5, tzinfo=timezone.utc), None, 10.0, has_product_id=True
        )
        self.assertIn("缺少云量指标", baseline_reasons)
        self.assertGreaterEqual(score, baseline)

    def test_sar_candidate_has_no_cloud_percent(self):
        candidate = EarthSearchProvider().candidate_from_item({
            "id": "S1A_GRD_X",
            "collection": "sentinel-1-grd",
            "bbox": [108.1, 22.1, 108.12, 22.12],
            "properties": {"datetime": "2026-09-01T10:00:00Z"},
            "assets": {"vv": {"href": "s3://sentinel-s1-l1c/x/iw-vv.tif", "gsd": 10}},
        })
        self.assertIsNone(candidate.cloud_percent)
        self.assertEqual(candidate.processing_level, "sentinel-1-grd")
        self.assertTrue(any("不受云量影响" in r for r in candidate.score_reasons))
        # M1 起 sentinel-1-grd 是一等影像源,不再打 experimental 标记。
        self.assertNotIn("experimental_collection", candidate.metadata)

    def test_unknown_s3_bucket_is_flagged_in_metadata(self):
        candidate = EarthSearchProvider().candidate_from_item({
            "id": "S1A_GRD_Y",
            "collection": "sentinel-1-grd",
            "bbox": [108.1, 22.1, 108.12, 22.12],
            "properties": {"datetime": "2026-09-01T10:00:00Z"},
            "assets": {"vv": {"href": "s3://unknown-bucket/x.tif", "gsd": 10}},
        })
        self.assertEqual(candidate.metadata["unverified_s3_buckets"], ["unknown-bucket"])
        self.assertTrue(any("未验证" in r for r in candidate.score_reasons))


class ProviderRegistryTests(SimpleTestCase):
    def test_get_provider_returns_known_providers(self):
        from .imagery_sources.earth_search import EarthSearchProvider
        from .imagery_sources.mapbox import MapboxProvider
        self.assertIsInstance(get_provider("earth_search"), EarthSearchProvider)
        self.assertIsInstance(get_provider("mapbox"), MapboxProvider)

    def test_get_provider_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            get_provider("not_a_provider")

    def test_default_profile_is_not_experimental(self):
        self.assertFalse(COLLECTION_PROFILES[DEFAULT_COLLECTION]["experimental"])
        # M1 起 SAR/DEM 转正式源;landsat 仍是占位;L1C 只作兜底保持 experimental。
        self.assertFalse(COLLECTION_PROFILES["sentinel-1-grd"]["experimental"])
        self.assertFalse(COLLECTION_PROFILES["cop-dem-glo-30"]["experimental"])
        self.assertFalse(COLLECTION_PROFILES["sentinel-2-c1-l2a"]["experimental"])
        self.assertTrue(COLLECTION_PROFILES["sentinel-2-l1c"]["experimental"])
        self.assertTrue(COLLECTION_PROFILES["landsat-c2-l2"]["experimental"])


class CollectionValidationTests(TestCase):
    def test_get_sentinel_img_api_rejects_unknown_collection(self):
        response = self.client.post(
            "/api/satellite/get-sentinel-img/",
            data={"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.12, "max_lat": 22.12,
                  "collection": "no-such-collection"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("collection", response.json()["msg"])

    def test_get_sentinel_img_api_default_collection_still_works(self):
        candidate = EarthSearchProvider().candidate_from_item({
            "id": "S2A_COLLECTION_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [108.1, 22.1, 108.12, 22.12],
            "properties": {
                "datetime": "2026-09-01T03:17:00Z",
                "eo:cloud_cover": 8,
                "s2:product_uri": "S2A_COLLECTION_TEST.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        })
        searched = {}

        def fake_search(self, bbox, **kwargs):
            searched.update(kwargs)
            return [candidate]

        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (64, 64), (90, 130, 170)).save(buf, "JPEG")
        jpg = buf.getvalue()
        with patch("map_api.views.EarthSearchProvider.search", fake_search), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=jpg):
            response = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.12, "max_lat": 22.12},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(searched["collection"], "sentinel-2-l2a")

    def test_imagery_search_rejects_unknown_collection(self):
        response = self.client.get(
            "/api/imagery/search/?min_lng=108.1&min_lat=22.1&max_lng=108.12&max_lat=22.12&collection=bad"
        )
        self.assertEqual(response.status_code, 400)
