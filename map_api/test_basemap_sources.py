"""M2a/b:天地图 / Esri World Imagery 高清底图源注册测试。"""
import os
import tempfile
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from PIL import Image

from .agent.tools import REGISTRY
from .data_contract import SOURCE_CAPABILITIES
from .imagery_sources import get_provider
from .run_executor import SLOT_SCHEMA, SatelliteHandlers
from .utils.get_satellite_image import fetch_satellite_image

BBOX = {"min_lng": 108.10, "min_lat": 22.10, "max_lng": 108.12, "max_lat": 22.12}


def _tile_image(*args, **kwargs):
    return Image.new("RGB", (256, 256), (80, 120, 160))


class BasemapProviderTests(SimpleTestCase):
    def test_tianditu_metadata(self):
        meta = get_provider("tianditu").metadata_for_bbox(BBOX).as_dict()
        self.assertEqual(meta["source"], "tianditu")
        self.assertEqual(meta["source_label"], "天地图影像")
        self.assertEqual(meta["processing_level"], "basemap")
        self.assertEqual(meta["license_type"], "tianditu_terms")
        self.assertEqual(meta["decision_grade"], "reference")
        self.assertIn("版权", meta["limitations"])

    def test_esri_metadata(self):
        meta = get_provider("esri").metadata_for_bbox(BBOX).as_dict()
        self.assertEqual(meta["source"], "esri")
        self.assertEqual(meta["source_label"], "Esri World Imagery")
        self.assertEqual(meta["license_type"], "esri_terms")
        self.assertIn("非营运", meta["limitations"])

    def test_capabilities_match_mapbox_grade(self):
        for source in ("tianditu", "esri"):
            caps = SOURCE_CAPABILITIES[source]
            self.assertTrue(caps["visual_interpretation"])
            for key in ("spectral_index", "physical_measurement", "change_detection", "small_target_detection"):
                self.assertFalse(caps[key], f"{source}.{key} 应为 False")


class BasemapTileFetchTests(SimpleTestCase):
    def test_esri_tile_url_template(self):
        urls = []
        with patch("map_api.utils.get_satellite_image._fetch_tile", side_effect=lambda url, *a, **k: (urls.append(url), _tile_image())[1]):
            with tempfile.TemporaryDirectory() as tmp:
                out = fetch_satellite_image(108.10, 22.10, 108.11, 22.11, save_dir=tmp,
                                            file_name="esri_test.jpg", target_resolution=256,
                                            basemap_source="esri")
        self.assertTrue(out and out.endswith("esri_test.jpg"))
        self.assertTrue(urls, "应至少抓取一张瓦片")
        for url in urls:
            self.assertIn("server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/", url)

    def test_tianditu_tile_url_template_and_key(self):
        urls = []
        with patch.dict(os.environ, {"TIANDITU_KEY": "dummy-tk"}):
            with patch("map_api.utils.get_satellite_image._fetch_tile", side_effect=lambda url, *a, **k: (urls.append(url), _tile_image())[1]):
                with tempfile.TemporaryDirectory() as tmp:
                    out = fetch_satellite_image(108.10, 22.10, 108.11, 22.11, save_dir=tmp,
                                                file_name="td_test.jpg", target_resolution=256,
                                                basemap_source="tianditu")
        self.assertTrue(out and out.endswith("td_test.jpg"))
        self.assertTrue(urls)
        for url in urls:
            self.assertIn("tianditu.gov.cn/DataServer?T=img_w", url)
            self.assertIn("tk=dummy-tk", url)

    def test_tianditu_missing_key_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "TIANDITU_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    fetch_satellite_image(108.10, 22.10, 108.11, 22.11, save_dir=tmp,
                                          file_name="td_nokey.jpg", target_resolution=256,
                                          basemap_source="tianditu")

    def test_unknown_basemap_source_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                fetch_satellite_image(108.10, 22.10, 108.11, 22.11, save_dir=tmp,
                                      file_name="bad.jpg", target_resolution=256,
                                      basemap_source="nonsense")


class BasemapEndpointTests(TestCase):
    def test_invalid_basemap_source_rejected(self):
        resp = Client().post("/api/satellite/get-img/", data={
            "min_lng": 108.10, "min_lat": 22.10, "max_lng": 108.12, "max_lat": 22.12,
            "basemap_source": "nonsense",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_mapbox_token_check_only_for_mapbox(self):
        env = {k: v for k, v in os.environ.items() if k not in ("MAPBOX_TOKEN", "TIANDITU_KEY")}
        env["AGENT_EXECUTION_MODE"] = "queue"
        with patch.dict(os.environ, env, clear=True):
            resp = Client().post("/api/satellite/get-img/", data={
                "min_lng": 108.10, "min_lat": 22.10, "max_lng": 108.12, "max_lat": 22.12,
                "basemap_source": "mapbox",
            }, content_type="application/json")
            self.assertEqual(resp.status_code, 500)
            self.assertIn("MAPBOX_TOKEN", resp.json()["msg"])

            resp = Client().post("/api/satellite/get-img/", data={
                "min_lng": 108.10, "min_lat": 22.10, "max_lng": 108.12, "max_lat": 22.12,
                "basemap_source": "tianditu",
            }, content_type="application/json")
            self.assertEqual(resp.status_code, 500)
            self.assertIn("TIANDITU_KEY", resp.json()["msg"])

    def test_esri_scene_source_without_any_key(self):
        env = {k: v for k, v in os.environ.items() if k not in ("MAPBOX_TOKEN", "TIANDITU_KEY")}
        env["AGENT_EXECUTION_MODE"] = "queue"
        with patch.dict(os.environ, env, clear=True):
            resp = Client().post("/api/satellite/get-img/", data={
                "min_lng": 108.10, "min_lat": 22.10, "max_lng": 108.12, "max_lat": 22.12,
                "basemap_source": "esri", "target_resolution": 256,
            }, content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        payload = resp.json()["data"]
        self.assertEqual(payload["scene"]["source"], "esri")
        self.assertEqual(payload["scene"]["source_label"], "Esri World Imagery")


class BasemapAgentRegistrationTests(SimpleTestCase):
    def test_tool_schema_has_basemap_source(self):
        params = REGISTRY["fetch_mapbox_imagery"]["parameters"]["properties"]
        self.assertIn("basemap_source", params)
        self.assertEqual(params["basemap_source"]["enum"], ["mapbox", "tianditu", "esri"])

    def test_slot_schema_accepts_new_sources(self):
        enum = SLOT_SCHEMA["properties"]["source"]["enum"]
        self.assertIn("tianditu", enum)
        self.assertIn("esri", enum)

    def test_search_scenes_dispatches_basemap_source(self):
        calls = []

        def fake_tool(ctx, args):
            calls.append(args)
            ctx["scene_id"] = 1
            ctx["file_name"] = "agent_esri_x.jpg"
            return {"status": "ok"}

        run = type("Run", (), {"goal": "g", "mode": "precise"})()
        handler = SatelliteHandlers(run=run, claim=None, context={
            "slots": {"source": "esri"}, "aoi": {"bbox": BBOX},
        })
        handler.before_request = lambda: None
        with patch("map_api.agent.tools._tool_fetch_mapbox_imagery", side_effect=fake_tool):
            result = handler.search_scenes()
        self.assertEqual(calls, [{"basemap_source": "esri"}])
        self.assertEqual(result["mapbox_file_name"], "agent_esri_x.jpg")
