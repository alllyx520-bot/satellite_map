import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from django.test import TestCase, override_settings
from rasterio.transform import from_origin

from .models import AgentRun, Conversation, ImageryScene, SpatialAttachment
from .v3.data_adapter import _same_day_scene_groups, data_specs, execute


class V3DataAdapterTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = override_settings(MEDIA_ROOT=self.temp.name); self.settings.enable(); self.addCleanup(self.settings.disable)
        self.conversation = Conversation.objects.create(owner_session_key="owner", title="test")
        self.run = AgentRun.objects.create(goal="test", conversation=self.conversation, run_key="adapter-test")
        self.context = {"conversation": self.conversation, "run": self.run}

    def attachment(self, values, band_map, metadata, products=None):
        path = Path(self.temp.name) / "input.tif"
        with rasterio.open(path, "w", driver="GTiff", height=values.shape[1], width=values.shape[2], count=values.shape[0], dtype="float32", crs="EPSG:4326", transform=from_origin(110, 30, .01, .01)) as dst:
            dst.write(values)
        return SpatialAttachment.objects.create(owner_session_key="owner", conversation=self.conversation, name="input.tif", kind="geotiff", status="ready", file_path=str(path), width=values.shape[2], height=values.shape[1], bbox={"min_lng":110,"min_lat":29.92,"max_lng":110.08,"max_lat":30}, crs="EPSG:4326", metadata={"band_map": band_map, "products": products or [], **metadata})

    def test_compute_from_attachment_persists_evidence_and_safe_artifact(self):
        attachment = self.attachment(np.stack([np.full((8,8), .1), np.full((8,8), .6), np.full((8,8), 4)]), {"red": 1, "nir": 2, "scl": 3}, {"collection":"sentinel-2-l2a", "band_calibration":{"red":{"scale":1,"offset":0},"nir":{"scale":1,"offset":0}}}, ["ndvi"])
        response = execute("compute_product", {"attachment_id": str(attachment.id), "product": "ndvi"}, self.context)
        self.assertNotIn("error", response)
        self.assertEqual(self.run.evidence_v2.count(), 1)
        artifact = self.run.artifacts_v2.get()
        self.assertTrue(artifact.metadata["relative_path"].startswith(f"{self.run.id}/"))
        self.assertTrue((Path(self.temp.name) / "v3" / artifact.metadata["relative_path"]).is_file())

    def test_model_arrays_are_rejected_before_compute(self):
        response = execute("compute_product", {"attachment_id": "x", "product": "ndvi", "bands": {"red": [[.1]]}}, self.context)
        self.assertEqual(response["error"]["code"], "reference_required")

    def test_landsat_needs_attachment_metadata_proving_collection_and_qa(self):
        attachment = self.attachment(np.stack([np.full((8,8),44178),np.zeros((8,8))]), {"st": 1, "qa_pixel": 2}, {"collection":"landsat-c2-l2", "asset_name":"ST_B10", "band_calibration":{"st":{"scale":0.00341802,"offset":149}}}, ["landsat_surface_temperature"])
        response = execute("compute_product", {"attachment_id":str(attachment.id), "product":"landsat_surface_temperature"}, self.context)
        self.assertAlmostEqual(response["result"]["summary"]["summary"]["mean_celsius"], 26.85, places=1)

    def test_specs_have_json_schemas_and_no_array_fields(self):
        specs = data_specs()
        self.assertEqual({x["name"] for x in specs}, {"search_scenes", "retrieve_imagery", "compute_product", "compare_two_date_change", "external_evidence"})
        self.assertNotIn("bands", specs[2]["schema"]["properties"])

    def test_source_metadata_cannot_be_overridden_by_model(self):
        attachment = self.attachment(np.stack([np.full((8,8), .1), np.full((8,8), .6)]), {"red": 1, "nir": 2}, {"collection":"sentinel-2-l2a", "band_calibration":{"red":{"scale":1,"offset":0},"nir":{"scale":1,"offset":0}}}, ["ndvi"])
        response = execute("compute_product", {"attachment_id": str(attachment.id), "product": "ndvi", "band_map": {"red": 2, "nir": 1}}, self.context)
        self.assertEqual(response["error"]["code"], "reference_required")

    def test_mocked_search_retrieve_compute_uses_real_provider_signature_and_asset_path(self):
        source = Path(self.temp.name) / "source.tif"
        values = np.stack([np.full((8,8), .1), np.full((8,8), .6)]).astype("float32")
        with rasterio.open(source, "w", driver="GTiff", height=8, width=8, count=2, dtype="float32", crs="EPSG:4326", transform=from_origin(110,30,.01,.01)) as dst: dst.write(values)
        scl = Path(self.temp.name) / "scl.tif"
        with rasterio.open(scl, "w", driver="GTiff", height=8, width=8, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(110,30,.01,.01)) as dst: dst.write(np.full((1,8,8), 4, dtype="float32"))
        scene = ImageryScene.objects.create(file_name="scene.tif", source="sentinel2", min_lng=110, min_lat=29.92, max_lng=110.08, max_lat=30,
            metadata={"band_map":{"red":1,"nir":2}, "products":["ndvi"], "collection":"sentinel-2-l2a"})
        candidate = type("Candidate", (), {"item_id":"S2-test", "product_id":"S2-test", "acquired_at":None,
            "assets": {name:{"href":str(scl if name == "scl" else source), "raster:bands":[{"scale":1,"offset":0}]} for name in ("red","green","blue","nir","swir16","swir22","scl")}, "metadata": {},
            "as_dict": lambda self: {"item_id":"S2-test", "collection":"sentinel-2-l2a"}})()
        bbox = {"min_lng":110,"min_lat":29.92,"max_lng":110.08,"max_lat":30}
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider:
            provider.return_value.search.return_value = [candidate]
            searched = execute("search_scenes", {"collection":"sentinel-2-l2a", "bbox":bbox}, self.context)
            retrieved = execute("retrieve_imagery", {"collection":"sentinel-2-l2a", "bbox":bbox}, self.context)
        self.assertEqual(provider.return_value.search.call_count, 2)
        provider.return_value.search.assert_any_call(bbox, start_date=None, end_date=None, max_cloud=30, limit=30, collection="sentinel-2-l2a")
        self.assertTrue(all(call.kwargs["limit"] == 30 for call in provider.return_value.search.call_args_list))
        self.assertEqual(searched["result"]["items"][0]["item_id"], "S2-test")
        attachment = retrieved["result"]["attachment"]
        self.assertEqual(attachment["status"], "ready")
        self.assertTrue(attachment["tile_url"])
        computed = execute("compute_product", {"attachment_id":attachment["id"], "product":"ndvi"}, self.context)
        self.assertNotIn("error", computed)

    def test_tool_interrupted_maps_to_retryable_ingest_interrupted(self):
        from .v3.runtime import ToolInterrupted
        candidate = type("Candidate", (), {"item_id":"S2-int", "product_id":"S2-int", "acquired_at":None,
            "assets": {name:{"href":"unused", "raster:bands":[{"scale":1,"offset":0}]} for name in ("red","green","blue","nir","swir16","swir22","scl")}, "metadata": {},
            "as_dict": lambda self: {"item_id":"S2-int", "collection":"sentinel-2-l2a"}})()
        bbox = {"min_lng":110,"min_lat":29.92,"max_lng":110.08,"max_lat":30}
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider, \
                patch("map_api.v3.data_adapter.ingest_cog_mosaic", side_effect=ToolInterrupted("时限到达")):
            provider.return_value.search.return_value = [candidate]
            result = execute("retrieve_imagery", {"collection":"sentinel-2-l2a", "bbox":bbox}, self.context)
        self.assertEqual(result["error"]["code"], "ingest_interrupted")
        self.assertTrue(result["error"]["retryable"])
        self.assertIn("继续下载", result["error"]["message"])

    def test_oversized_bbox_is_rejected_before_download_with_scene_bounds(self):
        candidate = type("Candidate", (), {"item_id":"S2-small", "product_id":"S2-small", "acquired_at":None,
            "bbox": {"min_lng":110.0,"min_lat":29.0,"max_lng":111.0,"max_lat":30.0},
            "assets": {name:{"href":"unused", "raster:bands":[{"scale":1,"offset":0}]} for name in ("red","green","blue","nir","swir16","swir22","scl")}, "metadata": {},
            "as_dict": lambda self: {"item_id":"S2-small", "collection":"sentinel-2-l2a"}})()
        bbox = {"min_lng":110,"min_lat":29.92,"max_lng":112.5,"max_lat":30}
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider, \
                patch("map_api.v3.data_adapter.ingest_cog_mosaic") as ingest:
            provider.return_value.search.return_value = [candidate]
            result = execute("retrieve_imagery", {"collection":"sentinel-2-l2a", "bbox":bbox, "item_id":"S2-small"}, self.context)
        self.assertEqual(result["error"]["code"], "invalid_data_input")
        self.assertIn("S2-small", result["error"]["message"])
        self.assertIn("缩到该范围内", result["error"]["message"])
        ingest.assert_not_called()

    def test_multi_scene_selection_never_mixes_acquisition_dates(self):
        candidate = lambda item, day: type("Candidate", (), {"item_id": item, "acquired_at": day})()
        first = candidate("first", datetime(2026, 9, 1, 10, tzinfo=timezone.utc))
        second = candidate("second", datetime(2026, 9, 1, 10, 5, tzinfo=timezone.utc))
        other_day = candidate("other", datetime(2026, 9, 2, 10, tzinfo=timezone.utc))
        groups = _same_day_scene_groups([first, second, other_day], "sentinel-2-l2a")
        self.assertEqual([[item.item_id for item in group] for group in groups], [["other"], ["first", "second"]])

    def test_bad_bbox_is_rejected_before_any_provider_call_with_correction(self):
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider:
            for tool, args in (
                ("search_scenes", {"collection": "sentinel-2-l2a"}),
                ("retrieve_imagery", {"collection": "sentinel-2-l2a"}),
                ("external_evidence", {"product": "weather"}),
            ):
                response = execute(tool, {**args, "bbox": {"west": 126.5, "south": 45.6, "east": 126.8, "north": 45.9}}, self.context)
                self.assertEqual(response["error"]["code"], "invalid_data_input")
                self.assertIn("min_lng/min_lat/max_lng/max_lat", response["error"]["message"])
            provider.assert_not_called()

    def test_invalid_ranges_dates_and_collection_fail_before_io(self):
        bbox = {"min_lng": 126.5, "min_lat": 45.6, "max_lng": 126.8, "max_lat": 45.9}
        cases = [
            {"bbox": {**bbox, "min_lng": 127}},
            {"bbox": {**bbox, "min_lat": -91}},
            {"date_start": "2026-02-30"},
            {"date_start": "2026-06-01", "date_end": "2026-03-01"},
            {"collection": "sentinel2"},
            {"max_cloud": 101},
        ]
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider:
            for overrides in cases:
                with self.subTest(overrides=overrides):
                    response = execute("search_scenes", {"collection": "sentinel-2-l2a", "bbox": bbox, **overrides}, self.context)
                    self.assertEqual(response["error"]["code"], "invalid_data_input")
            provider.assert_not_called()

    def test_unknown_collection_does_not_silently_retrieve_mapbox(self):
        with patch("map_api.v3.data_adapter._agent_fetch_mapbox") as fetch:
            result = execute("retrieve_imagery", {"collection": "gsw", "bbox": {"min_lng": 126, "min_lat": 45, "max_lng": 127, "max_lat": 46}}, self.context)
        self.assertIn("error", result)
        fetch.assert_not_called()

    def test_weather_range_is_forwarded_and_records_actual_source(self):
        bbox = {"min_lng": 126.5, "min_lat": 45.6, "max_lng": 126.8, "max_lat": 45.9}
        with patch("map_api.v3.data_adapter.external_data.query_weather_context", return_value={"daily": [{"date": "2026-03-01"}], "limitations": ["网格背景"]}) as weather:
            response = execute("external_evidence", {"product": "weather", "bbox": bbox, "date_start": "2026-03-01", "date_end": "2026-05-31"}, self.context)
        self.assertNotIn("error", response)
        weather.assert_called_once_with(45.75, 126.65, None, date_start="2026-03-01", date_end="2026-05-31")
        evidence = self.run.evidence_v2.get()
        self.assertEqual(evidence.data_contract["source"], "open-meteo")
        self.assertEqual(evidence.data_contract["date_end"], "2026-05-31")
        self.assertEqual(evidence.limitations, ["网格背景"])

    def test_static_background_cannot_be_misrepresented_as_current_date(self):
        with patch("map_api.v3.data_adapter.external_data.query_water_baseline") as baseline:
            response = execute("external_evidence", {"product": "water_baseline", "bbox": {"min_lng": 126, "min_lat": 45, "max_lng": 127, "max_lat": 46}, "date": "2026-04-01"}, self.context)
        self.assertIn("error", response)
        self.assertIn("固定历史基线", response["error"]["message"])
        baseline.assert_not_called()

    def test_weather_date_options_are_not_silently_ignored(self):
        bbox = {"min_lng": 126, "min_lat": 45, "max_lng": 127, "max_lat": 46}
        with patch("map_api.v3.data_adapter.external_data.query_weather_context") as weather:
            for options in ({"date_start": "2026-03-01"}, {"date": "2026-03-01", "date_start": "2026-03-01", "date_end": "2026-05-31"}, {"date_start": "2025-03-01", "date_end": "2026-05-31"}, {"days": 3}):
                response = execute("external_evidence", {"product": "weather", "bbox": bbox, **options}, self.context)
                self.assertIn("error", response)
            weather.assert_not_called()

    def test_failed_background_creates_no_computed_evidence(self):
        with patch("map_api.v3.data_adapter.external_data.query_water_baseline", return_value={"available": False, "failed_tiles": [{"tile": "x"}]}):
            response = execute("external_evidence", {"product": "water_baseline", "bbox": {"min_lng": 126, "min_lat": 45, "max_lng": 127, "max_lat": 46}}, self.context)
        self.assertEqual(response["result"]["error"]["code"], "data_unavailable")
        self.assertEqual(self.run.evidence_v2.count(), 0)

    def test_search_has_coverage_and_limit_but_retains_large_assets_only_in_artifact(self):
        bbox = {"min_lng": 126, "min_lat": 45, "max_lng": 127, "max_lat": 46}
        candidate = type("Candidate", (), {"as_dict": lambda self: {"item_id": "scene", "collection": "sentinel-2-l2a", "bbox": {**bbox, "max_lng": 126.5}, "assets": {"red": {"href": "https://example.org/band.tif", "metadata": "x" * 10000}}}})()
        with patch("map_api.v3.data_adapter.get_provider_for_collection") as provider:
            provider.return_value.search.return_value = [candidate]
            result = execute("search_scenes", {"collection": "sentinel-2-l2a", "bbox": bbox, "limit": 1}, self.context)["result"]
        self.assertEqual(result["items"][0]["bbox_overlap_ratio"], 0.5)
        self.assertEqual(result["items"][0]["asset_keys"], ["red"])
        self.assertNotIn("assets", result["items"][0])
        self.assertTrue(result["possibly_truncated"])
