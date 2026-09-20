"""M1 数据源扩展:sentinel-1-grd / cop-dem-glo-30 / sentinel-2-c1-l2a / sentinel-2-l1c 一等源注册测试。"""
from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from PIL import Image

from .agent.loop import _deterministic_next_call
from .agent.tools import REGISTRY
from .data_contract import SOURCE_CAPABILITIES, build_scene_contract
from .imagery_sources.earth_search import EarthSearchProvider
from .run_executor import SOURCE_TO_COLLECTION, SLOT_SCHEMA, SatelliteHandlers
from .spectral_products import compute_spectral_summary
from .utils.agent_tools import deterministic_extract_slots, merge_agent_slots


def _jpg_bytes():
    buf = BytesIO()
    Image.new("RGB", (64, 64), (90, 130, 170)).save(buf, "JPEG")
    return buf.getvalue()


BBOX = {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.12, "max_lat": 22.12}


def _candidate(collection, assets, **properties):
    return EarthSearchProvider().candidate_from_item({
        "id": f"TEST_{collection}",
        "collection": collection,
        "bbox": [BBOX["min_lng"], BBOX["min_lat"], BBOX["max_lng"], BBOX["max_lat"]],
        "properties": {"datetime": "2026-09-01T03:17:00Z", **properties},
        "assets": assets,
    })


class SourceCapabilitiesTests(SimpleTestCase):
    def test_new_sources_registered_with_expected_fields(self):
        for source in ("sentinel1", "copdem"):
            self.assertIn(source, SOURCE_CAPABILITIES)
            caps = SOURCE_CAPABILITIES[source]
            self.assertTrue(caps["visual_interpretation"])
            for key in ("spectral_index", "physical_measurement", "change_detection", "small_target_detection"):
                self.assertFalse(caps[key], f"{source}.{key} 应为 False")

    def test_scene_contract_uses_profile_native_resolution(self):
        scene = type("Scene", (), {
            "source": "sentinel1", "product_id": "S1", "acquired_at": None, "published_at": None,
            "min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1, "gsd_m": 10.0,
            "cloud_percent": None, "metadata": {"collection": "sentinel-1-grd"},
        })()
        contract = build_scene_contract(scene)
        self.assertEqual(contract["spatial"]["native_resolution_m"], 10)
        self.assertTrue(contract["capabilities"]["visual_interpretation"])
        self.assertFalse(contract["capabilities"]["spectral_index"])
        self.assertTrue(any("SAR" in item for item in contract["limitations"]))

        dem_scene = type("Scene", (), {
            "source": "copdem", "product_id": "DEM", "acquired_at": None, "published_at": None,
            "min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1, "gsd_m": 30.0,
            "cloud_percent": None, "metadata": {"collection": "cop-dem-glo-30"},
        })()
        dem_contract = build_scene_contract(dem_scene)
        self.assertEqual(dem_contract["spatial"]["native_resolution_m"], 30)


class SlotPlanningTests(SimpleTestCase):
    def test_flood_keywords_route_to_sentinel1(self):
        slots = deterministic_extract_slots("调查南宁近期洪水淹没情况")
        self.assertEqual(slots["task"], "flood")
        self.assertEqual(slots["source"], "sentinel1")
        # 时效词不能把 flood 抢回 sentinel2。
        self.assertTrue(slots["needs_timeliness"])

    def test_terrain_keywords_route_to_copdem(self):
        slots = deterministic_extract_slots("分析南宁周边地形坡度")
        self.assertEqual(slots["task"], "terrain")
        self.assertEqual(slots["source"], "copdem")

    def test_all_weather_keywords_route_to_sentinel1(self):
        slots = deterministic_extract_slots("多云天气看看南宁土地利用")
        self.assertEqual(slots["source"], "sentinel1")

    def test_merge_agent_slots_accepts_new_sources(self):
        merged = merge_agent_slots({"task": "flood", "source": "sentinel1"}, "一般调查")
        self.assertEqual(merged["source"], "sentinel1")
        merged = merge_agent_slots({"task": "terrain", "source": "copdem"}, "一般调查")
        self.assertEqual(merged["source"], "copdem")

    def test_merge_agent_slots_normalizes_illegal_source_by_task(self):
        merged = merge_agent_slots({"task": "flood", "source": "bogus"}, "一般调查")
        self.assertEqual(merged["source"], "sentinel1")
        merged = merge_agent_slots({"task": "terrain", "source": "bogus"}, "一般调查")
        self.assertEqual(merged["source"], "copdem")

    def test_slot_schema_source_enum_extended(self):
        self.assertEqual(
            set(SLOT_SCHEMA["properties"]["source"]["enum"]),
            {"sentinel2", "mapbox", "tianditu", "esri", "sentinel1", "copdem", "landsat"},
        )


class AgentToolSchemaTests(SimpleTestCase):
    def test_search_sentinel_imagery_schema_has_collection_enum(self):
        schema = REGISTRY["search_sentinel_imagery"]["parameters"]
        collection = schema["properties"]["collection"]
        self.assertEqual(
            set(collection["enum"]),
            {"sentinel-2-l2a", "sentinel-2-c1-l2a", "sentinel-2-l1c", "sentinel-1-grd", "cop-dem-glo-30", "landsat-c2-l2"},
        )
        self.assertEqual(collection["default"], "sentinel-2-l2a")

    def test_deterministic_next_call_dispatches_new_sources(self):
        ctx = {"slots": {"source": "sentinel1", "place_name": "南宁市"}, "bbox": BBOX, "scene_id": None}
        call = _deterministic_next_call(ctx)
        self.assertEqual(call["name"], "search_sentinel_imagery")
        self.assertEqual(call["args"]["collection"], "sentinel-1-grd")

        ctx = {"slots": {"source": "copdem", "place_name": "南宁市"}, "bbox": BBOX, "scene_id": None}
        call = _deterministic_next_call(ctx)
        self.assertEqual(call["args"]["collection"], "cop-dem-glo-30")

        ctx = {"slots": {"source": "sentinel2", "place_name": "南宁市"}, "bbox": BBOX, "scene_id": None}
        call = _deterministic_next_call(ctx)
        self.assertNotIn("collection", call["args"])


class L1CFallbackTests(TestCase):
    def test_l2a_empty_search_falls_back_to_l1c(self):
        from map_api.orchestrator import _agent_fetch_sentinel

        l1c_candidate = _candidate(
            "sentinel-2-l1c",
            {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
            **{"eo:cloud_cover": 8, "s2:product_uri": "S2A_L1C_TEST.SAFE"},
        )
        with patch("map_api.views.EarthSearchProvider.search", side_effect=[[], [l1c_candidate]]) as search_mock, \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=_jpg_bytes()):
            scene, candidate, retrieval = _agent_fetch_sentinel(BBOX, {"date_start": None, "date_end": None})
        self.assertIsNotNone(scene)
        # 第一次 L2A 空、第二次 L1C 有候选。
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(search_mock.call_args_list[1].kwargs["collection"], "sentinel-2-l1c")
        self.assertEqual(retrieval["collection_fallback"], "sentinel-2-l1c")
        self.assertEqual((scene.metadata or {}).get("collection_fallback"), "sentinel-2-l1c")
        self.assertIn("已回退到 L1C", scene.limitations)
        self.assertIn("已回退到 L1C", retrieval["candidate"].limitations)

    def test_l2a_hit_does_not_fallback(self):
        from map_api.orchestrator import _agent_fetch_sentinel

        l2a_candidate = _candidate(
            "sentinel-2-l2a",
            {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
            **{"eo:cloud_cover": 8, "s2:product_uri": "S2A_L2A_TEST.SAFE"},
        )
        with patch("map_api.views.EarthSearchProvider.search", return_value=[l2a_candidate]) as search_mock, \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=_jpg_bytes()):
            scene, candidate, retrieval = _agent_fetch_sentinel(BBOX, {"date_start": None, "date_end": None})
        self.assertEqual(search_mock.call_count, 1)
        self.assertNotIn("collection_fallback", retrieval)
        self.assertNotIn("collection_fallback", scene.metadata or {})


class QualityGateNewSourceTests(SimpleTestCase):
    def _handler(self, source, candidates):
        context = {
            "slots": {"source": source, "date_start": "2026-08-01", "date_end": "2026-09-10", "two_dates": False},
            "aoi": {"bbox": BBOX, "polygon": None, "scope": "user_bbox"},
            "requirements": {"required_indices": []},
            "candidates": [candidates],
        }
        return SatelliteHandlers(None, None, context)

    def test_sentinel1_candidate_passes_without_scl_or_cloud(self):
        candidate = _candidate(
            "sentinel-1-grd",
            {"vv": {"href": "https://example.com/iw-vv.tif", "gsd": 10}},
        ).as_dict()
        self.assertIsNone(candidate["cloud_percent"])
        handler = self._handler("sentinel1", [candidate])
        quality = handler.quality_gate()["quality"]
        self.assertTrue(quality["passed"])

    def test_copdem_candidate_passes_with_data_asset(self):
        candidate = _candidate(
            "cop-dem-glo-30",
            {"data": {"href": "https://example.com/dem.tif", "gsd": 30}},
        ).as_dict()
        handler = self._handler("copdem", [candidate])
        quality = handler.quality_gate()["quality"]
        self.assertTrue(quality["passed"])

    def test_source_collection_mapping(self):
        self.assertEqual(SOURCE_TO_COLLECTION["sentinel2"], "sentinel-2-l2a")
        self.assertEqual(SOURCE_TO_COLLECTION["sentinel1"], "sentinel-1-grd")
        self.assertEqual(SOURCE_TO_COLLECTION["copdem"], "cop-dem-glo-30")


class SpectralDegradationTests(SimpleTestCase):
    def test_l1c_scene_rejects_spectral_index_with_clear_error(self):
        candidate = type("Candidate", (), {"assets": {}, "collection": "sentinel-2-l1c"})()
        with self.assertRaisesRegex(ValueError, "无 SCL 云掩膜"):
            compute_spectral_summary(candidate, BBOX, index="ndwi")

    def test_sar_scene_rejects_spectral_index_with_clear_error(self):
        candidate = type("Candidate", (), {"assets": {"vv": {"href": "x"}}, "collection": "sentinel-1-grd"})()
        with self.assertRaisesRegex(ValueError, "无 SCL 云掩膜"):
            compute_spectral_summary(candidate, BBOX, index="ndvi")


class RecommendSourceEndpointTests(TestCase):
    def test_flood_question_recommends_sentinel1(self):
        response = self.client.post(
            "/api/imagery/recommend-source/",
            data={"question": "南宁最近洪水情况如何", "current_source": "mapbox"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rec = response.json()["data"]["recommendation"]
        self.assertEqual(rec["recommended_source"], "sentinel1")

    def test_terrain_question_recommends_copdem(self):
        response = self.client.post(
            "/api/imagery/recommend-source/",
            data={"question": "南宁周边地形坡度怎么样", "current_source": "mapbox"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rec = response.json()["data"]["recommendation"]
        self.assertEqual(rec["recommended_source"], "copdem")
