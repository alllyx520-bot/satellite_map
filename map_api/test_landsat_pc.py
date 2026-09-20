"""M3-3a:Landsat C2 L2 经 Microsoft Planetary Computer 匿名 SAS 签名接入。"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile
from django.test import SimpleTestCase, TestCase
from PIL import Image

from .data_contract import SOURCE_CAPABILITIES
from .imagery_sources import get_provider, get_provider_for_collection
from .imagery_sources.earth_search import COLLECTION_PROFILES, EarthSearchProvider
from .imagery_sources.planetary_computer import PlanetaryComputerProvider
from .run_executor import SOURCE_TO_COLLECTION
from .utils.agent_tools import _qa_valid_mask, _read_ndwi_inputs, deterministic_extract_slots


LANDSAT_BBOX = {"min_lng": 108.30, "min_lat": 22.75, "max_lng": 108.35, "max_lat": 22.80}
RASTER_BANDS = [{"scale": 2.75e-5, "offset": -0.2, "nodata": 0}]


def _landsat_item():
    return {
        "id": "LC08_L2SP_SMOKE",
        "collection": "landsat-c2-l2",
        "bbox": [108.30, 22.75, 108.35, 22.80],
        "properties": {
            "datetime": "2024-06-15T03:20:00Z",
            "eo:cloud_cover": 5.0,
            "landsat:product_id": "LC08_L2SP_126045_20240615_02_T1",
            "platform": "landsat-8",
        },
        "assets": {
            key: {"href": f"https://landsateuwest.blob.core.windows.net/landsat-c2/{key}.TIF",
                  "gsd": 30, "raster:bands": RASTER_BANDS}
            for key in ("red", "green", "blue", "nir08", "swir16", "qa_pixel")
        },
    }


def _signed_response(href, expiry):
    signed = f"{href}?se={expiry.strftime('%Y-%m-%dT%H:%M:%SZ')}&sig=fake"
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"href": signed},
    )


def _tif_response(array):
    buf = BytesIO()
    tifffile.imwrite(buf, array)
    return SimpleNamespace(
        content=buf.getvalue(),
        raise_for_status=lambda: None,
    )


class SignHrefCacheTests(SimpleTestCase):
    def test_temperature_alias_requires_confirmed_level2_st_product(self):
        item = _landsat_item()
        item['assets']['lwir11'] = {
            'href': 'https://example.org/LC09_L2SP_123032_ST_B10.TIF',
            'roles': ['data', 'temperature'],
            'raster:bands': [{'unit': 'kelvin', 'scale': 0.00341802, 'offset': 149., 'nodata': 0}],
        }
        provider = PlanetaryComputerProvider()
        self.assertIn('st_b10', provider.candidate_from_item(item).assets)
        item['assets']['lwir11']['href'] = 'https://example.org/LC09_L1TP_B10.TIF'
        self.assertNotIn('st_b10', provider.candidate_from_item(item).assets)

    def test_sign_href_caches_until_expiry(self):
        provider = PlanetaryComputerProvider()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=40)
        calls = []

        def fake_get(url, *, params, **kwargs):
            calls.append(url)
            return _signed_response(params["href"], expiry)

        with patch("map_api.imagery_sources.planetary_computer.requests.get", side_effect=fake_get):
            first = provider.sign_href("https://blob/x.TIF")
            second = provider.sign_href("https://blob/x.TIF")
        self.assertEqual(first, second)
        self.assertIn("sig=fake", first)
        self.assertEqual(len(calls), 1)

    def test_sign_href_refreshes_when_near_expiry(self):
        provider = PlanetaryComputerProvider()
        # 过期时刻距现在不足 5 分钟缓冲,必须重新签名。
        expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
        calls = []

        def fake_get(url, *, params, **kwargs):
            calls.append(url)
            return _signed_response(params["href"], expiry)

        with patch("map_api.imagery_sources.planetary_computer.requests.get", side_effect=fake_get):
            provider.sign_href("https://blob/x.TIF")
            provider.sign_href("https://blob/x.TIF")
        self.assertEqual(len(calls), 2)

    def test_sign_href_failure_raises_value_error(self):
        import requests as req
        provider = PlanetaryComputerProvider()
        with patch(
            "map_api.imagery_sources.planetary_computer.requests.get",
            side_effect=req.exceptions.ConnectionError("boom"),
        ):
            with self.assertRaisesRegex(ValueError, "SAS 签名失败"):
                provider.sign_href("https://blob/x.TIF")


class RgbComposeRenderTests(SimpleTestCase):
    def _fake_requests_get(self, dn_arrays, signed_calls):
        expiry = datetime.now(timezone.utc) + timedelta(minutes=40)
        band_names = ["red", "green", "blue"]
        fetch_count = {"n": 0}

        def fake_get(url, *, params, **kwargs):
            if "sas" in url:
                href = params["href"]
                signed_calls.append(href)
                return _signed_response(href, expiry)
            # titiler 取数组:按调用顺序返回 red/green/blue
            index = fetch_count["n"]
            fetch_count["n"] += 1
            self.assertEqual(params["bidx"], 1)
            return _tif_response(dn_arrays[band_names[index]])

        return fake_get

    def test_rgb_compose_math_nodata_black_and_stretch(self):
        candidate = PlanetaryComputerProvider().candidate_from_item(_landsat_item())
        self.assertEqual(candidate.product_id, "LC08_L2SP_126045_20240615_02_T1")
        self.assertEqual(candidate.source, "planetary_computer")
        shape = (16, 16)
        # dn=12000 → refl=0.13 → 拉伸后约 0.4333*255≈110;dn=0/70000 为填充值必须置黑。
        base = np.full(shape, 12000, dtype=np.uint16)
        base[0, 0] = 0
        base[0, 1] = 65535
        dn = {name: base.copy() for name in ("red", "green", "blue")}
        signed_calls = []
        provider = PlanetaryComputerProvider(titiler_endpoint="https://titiler.test")
        with patch(
            "map_api.imagery_sources.planetary_computer.requests.get",
            side_effect=self._fake_requests_get(dn, signed_calls),
        ):
            jpeg = provider.render_candidate_jpeg(candidate, LANDSAT_BBOX, 16, 16)
        image = Image.open(BytesIO(jpeg))
        self.assertEqual(image.format, "JPEG")
        arr = np.asarray(image)
        self.assertEqual(arr.shape, (16, 16, 3))
        # 填充值置黑
        self.assertTrue((arr[0, 0] < 10).all())
        self.assertTrue((arr[0, 1] < 10).all())
        # 正常像元拉伸到 ~110(JPEG 有损,容差 ±6)
        expected = round(np.clip((12000 * 2.75e-5 - 0.2) / 0.3, 0, 1) * 255)
        self.assertTrue((np.abs(arr[5, 5].astype(int) - expected) <= 6).all())
        # 三个波段资产分别签名
        self.assertEqual(len(signed_calls), 3)
        self.assertEqual(len({call for call in signed_calls}), 3)

    def test_render_fails_when_band_missing(self):
        item = _landsat_item()
        del item["assets"]["blue"]
        candidate = PlanetaryComputerProvider().candidate_from_item(item)
        provider = PlanetaryComputerProvider()
        with self.assertRaisesRegex(ValueError, "blue"):
            provider.render_candidate_jpeg(candidate, LANDSAT_BBOX, 16, 16)

    def test_render_band_failure_raises_value_error(self):
        import requests as req
        candidate = PlanetaryComputerProvider().candidate_from_item(_landsat_item())
        provider = PlanetaryComputerProvider(titiler_endpoint="https://titiler.test")

        def fake_get(url, *, params, **kwargs):
            if "sas" in url:
                return _signed_response(params["href"], datetime.now(timezone.utc) + timedelta(minutes=40))
            raise req.exceptions.ConnectionError("titiler down")

        with patch("map_api.imagery_sources.planetary_computer.requests.get", side_effect=fake_get):
            with self.assertRaises(req.exceptions.ConnectionError):
                provider.render_candidate_jpeg(candidate, LANDSAT_BBOX, 16, 16)


class ProviderRoutingTests(SimpleTestCase):
    def test_get_provider_for_collection_routes_landsat(self):
        provider = get_provider_for_collection("landsat-c2-l2")
        self.assertIsInstance(provider, PlanetaryComputerProvider)
        self.assertEqual(provider.source, "planetary_computer")

    def test_get_provider_for_collection_defaults_to_earth_search(self):
        self.assertIsInstance(get_provider_for_collection("sentinel-2-l2a"), EarthSearchProvider)
        self.assertIsInstance(get_provider_for_collection("cop-dem-glo-30"), EarthSearchProvider)

    def test_provider_registered(self):
        self.assertIsInstance(get_provider("planetary_computer"), PlanetaryComputerProvider)

    def test_landsat_profile_final(self):
        profile = COLLECTION_PROFILES["landsat-c2-l2"]
        self.assertFalse(profile["experimental"])
        self.assertEqual(profile["provider"], "planetary_computer")
        self.assertEqual(profile["qa_band"], "qa_pixel")
        self.assertEqual(profile["qa_kind"], "bitmask")
        self.assertEqual(profile["qa_bad_bits"], [1, 2, 3, 4])
        self.assertEqual(profile["band_aliases"], {"nir": "nir08"})
        self.assertEqual(profile["native_resolution_m"], 30)
        self.assertEqual(profile["render"]["strategy"], "rgb_compose")


class RegistrationTests(SimpleTestCase):
    def test_source_capabilities(self):
        caps = SOURCE_CAPABILITIES["landsat"]
        self.assertTrue(caps["visual_interpretation"])
        self.assertTrue(caps["spectral_index"])
        self.assertFalse(caps["physical_measurement"])
        self.assertTrue(caps["change_detection"])
        self.assertFalse(caps["small_target_detection"])

    def test_source_to_collection(self):
        self.assertEqual(SOURCE_TO_COLLECTION["landsat"], "landsat-c2-l2")

    def test_search_tool_schema_enum(self):
        from .agent.tools import DEFINITIONS
        enum = DEFINITIONS["search_sentinel_imagery"].parameters["properties"]["collection"]["enum"]
        self.assertIn("landsat-c2-l2", enum)

    def test_history_keywords_route_to_landsat(self):
        slots = deterministic_extract_slots("对比南宁市十年前和现在的城区变化")
        self.assertEqual(slots["source"], "landsat")
        # 历史词优先于时效覆写
        slots = deterministic_extract_slots("回溯该水库多年前的水面")
        self.assertEqual(slots["source"], "landsat")
        # flood/terrain 不被历史词抢走
        slots = deterministic_extract_slots("查看历史洪水淹没范围")
        self.assertEqual(slots["source"], "sentinel1")


class QaPixelBitmaskTests(SimpleTestCase):
    def test_bitmask_marks_bad_bits_invalid(self):
        profile = COLLECTION_PROFILES["landsat-c2-l2"]
        # bit0(填充)=1 不在 bad_bits 内;bit1/2/3/4 置位即剔除。
        qa = np.array([
            [0, 1, 2, 4],      # 无标志(保留), bit0 填充(不在 bad_bits 内), bit1 稀释云, bit2 卷云
            [8, 16, 3, 547],   # bit3 云, bit4 云影, bit0+1, 多位含 bad
        ], dtype=np.float32)
        mask = _qa_valid_mask(qa, profile)
        expected = np.array([
            [True, True, False, False],
            [False, False, False, False],
        ])
        self.assertTrue((mask == expected).all())

    def test_bitmask_nan_is_invalid(self):
        profile = COLLECTION_PROFILES["landsat-c2-l2"]
        mask = _qa_valid_mask(np.array([[np.nan, 0.0]]), profile)
        self.assertEqual(mask.tolist(), [[False, True]])

    def test_read_ndwi_inputs_uses_nir08_and_qa_pixel(self):
        candidate = PlanetaryComputerProvider().candidate_from_item(_landsat_item())
        fetched = []

        def fake_fetch(asset, bbox, titiler_endpoint, **kwargs):
            fetched.append((asset["href"], kwargs.get("kind", "reflectance")))
            if "qa_pixel" in asset["href"]:
                return np.zeros((8, 8), dtype=np.float32)
            return np.full((8, 8), 0.2, dtype=np.float32)

        def fake_get_provider(name, **kwargs):
            provider = SimpleNamespace(sign_asset=lambda asset: {**asset, "href": asset["href"] + "?sig=1"})
            return provider

        with patch("map_api.utils.agent_tools.fetch_cog_bbox_array", side_effect=fake_fetch), \
                patch("map_api.imagery_sources.get_provider", side_effect=fake_get_provider):
            green, nir, qa = _read_ndwi_inputs(candidate, LANDSAT_BBOX, None)
        self.assertTrue(qa.all())
        hrefs = [href for href, _ in fetched]
        # nir 经 band_aliases 落到 nir08;所有资产 href 已签名。
        self.assertTrue(any("nir08" in href and "sig=1" in href for href in hrefs))
        self.assertTrue(any("qa_pixel" in href and kind == "scl" for href, kind in fetched))


class RecommendSourceEndpointTests(TestCase):
    def test_history_question_recommends_landsat(self):
        response = self.client.post(
            "/api/imagery/recommend-source/",
            data={"question": "对比南宁市十年前和现在的城区扩张", "current_source": "mapbox"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rec = response.json()["data"]["recommendation"]
        self.assertEqual(rec["recommended_source"], "landsat")
        self.assertIn("Landsat", rec["recommended_label"])

    def test_landsat_current_source_accepted(self):
        response = self.client.post(
            "/api/imagery/recommend-source/",
            data={"question": "看看这片区域", "current_source": "landsat"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["current_source"], "landsat")
