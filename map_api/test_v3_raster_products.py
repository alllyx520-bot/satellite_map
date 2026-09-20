import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from django.test import SimpleTestCase
from rasterio.transform import from_origin

from .v3.raster_products import _recoverable_cog_error, compute_attachment, ingest_cog_mosaic, ingest_cogs


class NativeRasterProductTests(SimpleTestCase):

    def test_wrapped_warp_operation_is_recoverable(self):
        error = type("WarpOperationError", (Exception,), {})("Chunk and warp failed")
        self.assertTrue(_recoverable_cog_error(error))

    def test_ingest_retries_corrupt_remote_range_with_new_cache_key(self):
        target = Path(tempfile.gettempdir()) / "v3-cog-retry-test.tif"
        selected = [("red", {"href": "https://example.test/B04.tif", "source_href": "https://example.test/B04.tif"})]
        calls = []

        def fail_then_succeed(request_selected, bbox, path, **kwargs):
            calls.append(request_selected[0][1]["href"])
            if len(calls) == 1:
                raise RuntimeError("TIFFReadEncodedTile: short HTTP range response")
            path.write_bytes(b"native-cog-output")
            return {"band_map": {"red": 1}}

        with patch("map_api.v3.raster_products._ingest_cogs_once", side_effect=fail_then_succeed):
            metadata = ingest_cogs(selected, {"min_lng": 1, "min_lat": 1, "max_lng": 2, "max_lat": 2}, target, profile={})
        try:
            self.assertEqual(metadata["band_map"], {"red": 1})
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(all("cog_retry=" in call for call in calls))
            self.assertEqual(selected[0][1]["href"], "https://example.test/B04.tif")
        finally:
            if target.exists(): target.unlink()
    def _write(self, path, values, transform, crs="EPSG:3857"):
        with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
                           dtype="float32", crs=crs, transform=transform, tiled=True, blockxsize=16, blockysize=16) as dst:
            dst.write(values, 1)

    def test_ingest_preserves_reference_grid_and_records_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); transform = from_origin(12240000, 3550000, 10, 10)
            red, nir, scl = root / "red.tif", root / "nir.tif", root / "scl.tif"
            self._write(red, np.full((64, 64), 1000, dtype="float32"), transform)
            self._write(nir, np.full((64, 64), 6000, dtype="float32"), transform)
            self._write(scl, np.full((64, 64), 4, dtype="float32"), transform)
            bbox = {"min_lng": 109.95, "min_lat": 29.95, "max_lng": 110.01, "max_lat": 30.01}
            # Use a geographic bbox derived from the source itself so this also checks CRS transformation.
            with rasterio.open(red) as src:
                from rasterio.warp import transform_bounds
                west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            bbox = {"min_lng": west + (east-west)*.2, "min_lat": south + (north-south)*.2, "max_lng": west + (east-west)*.8, "max_lat": south + (north-south)*.8}
            asset = lambda p: {"href": str(p), "raster:bands": [{"scale": .0001, "offset": 0, "nodata": 0}]}
            target = root / "out.tif"
            meta = ingest_cogs([("red", asset(red)), ("nir", asset(nir)), ("scl", asset(scl))], bbox, target, profile={}, source_metadata={"collection":"sentinel-2-l2a", "products":["ndvi"]})
            with rasterio.open(target) as output:
                self.assertEqual(output.crs.to_string(), "EPSG:3857")
                self.assertLess(output.width, 64)
                self.assertEqual(abs(output.transform.a), 10)
                expected_pixels = output.width * output.height
            self.assertEqual(meta["band_map"], {"red": 1, "nir": 2, "scl": 3})
            self.assertEqual(meta["band_calibration"]["nir"]["scale"], .0001)
            result = compute_attachment(target, meta, "ndvi")
            self.assertEqual(result["summary"]["valid_pixel_count"], expected_pixels)
            self.assertAlmostEqual(result["summary"]["mean"], .7143, places=4)

    def test_optical_product_refuses_missing_qa_and_sar_refuses_undeclared_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.tif"; transform = from_origin(0, 100, 10, 10)
            with rasterio.open(path, "w", driver="GTiff", height=16, width=16, count=2, dtype="float32", crs="EPSG:3857", transform=transform, tiled=True, blockxsize=16, blockysize=16) as dst:
                dst.write(np.full((16,16), .1, dtype="float32"), 1); dst.write(np.full((16,16), .6, dtype="float32"), 2)
            optical = {"collection":"sentinel-2-l2a", "band_map":{"red":1,"nir":2}, "band_calibration":{"red":{"scale":1,"offset":0},"nir":{"scale":1,"offset":0}}}
            with self.assertRaisesRegex(ValueError, "SCL"):
                compute_attachment(path, optical, "ndvi")
            sar = {"collection":"sentinel-1-grd", "band_map":{"vv":1}}
            with self.assertRaisesRegex(ValueError, "calibration"):
                compute_attachment(path, sar, "sar_backscatter")

    def test_same_day_mosaic_fills_adjacent_tiles_and_prefers_clear_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def tile(prefix, west, red_value, qa_value):
                transform = from_origin(west, 16, 1, 1)
                paths = []
                for name, value in (("red", red_value), ("nir", .6), ("scl", qa_value)):
                    path = root / f"{prefix}-{name}.tif"; self._write(path, np.full((16, 24), value, dtype="float32"), transform, "EPSG:4326")
                    paths.append((name, {"href": str(path), "raster:bands": [{"scale": 1, "offset": 0, "nodata": 0}]}))
                return {"item_id": prefix, "assets": paths}
            # left covers x=0..24 but is cloudy; right covers x=8..32 and must
            # replace it in the overlap while also completing the requested AOI.
            target = root / "mosaic.tif"
            metadata = ingest_cog_mosaic([tile("left", 0, .1, 8), tile("right", 8, .2, 4)],
                                         {"min_lng": 0, "min_lat": 0, "max_lng": 32, "max_lat": 16}, target,
                                         profile={"qa_band": "scl"}, source_metadata={"collection": "sentinel-2-l2a"})
            with rasterio.open(target) as output:
                red = output.read(metadata["band_map"]["red"])
                qa = output.read(metadata["band_map"]["scl"])
            self.assertEqual(red.shape, (16, 32))
            self.assertAlmostEqual(float(red[8, 12]), .2)  # clear right tile wins overlap
            self.assertAlmostEqual(float(red[8, 2]), .1)   # cloudy left still records real observation
            self.assertEqual(float(qa[8, 12]), 4.)
            self.assertTrue(metadata["mosaic_coverage"]["coverage_complete"])
            self.assertEqual(metadata["mosaic_coverage"]["source_coverage_ratio"], 1.0)
            self.assertEqual(metadata["mosaic_coverage"]["qa_valid_coverage_ratio"], 0.75)

    def test_mosaic_rejects_partial_coverage_and_calibration_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def scene(scale):
                result = []
                for name, value in (("red", .1), ("nir", .6), ("scl", 4)):
                    path = root / f"{scale}-{name}.tif"; self._write(path, np.full((16, 16), value, dtype="float32"), from_origin(0, 16, 1, 1), "EPSG:4326")
                    result.append((name, {"href": str(path), "raster:bands": [{"scale": scale, "offset": 0, "nodata": 0}]}))
                return {"assets": result}
            bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 32, "max_lat": 16}
            with self.assertRaisesRegex(ValueError, "未完整覆盖"):
                ingest_cog_mosaic([scene(1)], bbox, root / "partial.tif", profile={"qa_band": "scl"})
            with self.assertRaisesRegex(ValueError, "校准"):
                ingest_cog_mosaic([scene(1), scene(.5)], {**bbox, "max_lng": 16}, root / "bad-scale.tif", profile={"qa_band": "scl"})

    def _scene_with_nodata_holes(self, root, prefix, holes, size=1024):
        paths = []
        for name, value in (("red", .1), ("nir", .6), ("scl", 4)):
            data = np.full((size, size), value, dtype="float32")
            data[holes] = 0.0
            path = root / f"{prefix}-{name}.tif"
            with rasterio.open(path, "w", driver="GTiff", height=size, width=size, count=1,
                               dtype="float32", crs="EPSG:4326", transform=from_origin(0, size, 1, 1),
                               tiled=True, blockxsize=16, blockysize=16, nodata=0) as dst:
                dst.write(data, 1)
            paths.append((name, {"href": str(path), "raster:bands": [{"scale": 1, "offset": 0, "nodata": 0}]}))
        return {"item_id": prefix, "assets": paths}

    def test_mosaic_tolerates_tiny_boundary_gap_and_records_it_honestly(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            size = 1024
            holes = np.zeros((size, size), dtype=bool)
            holes[:10, :10] = True  # 100 个像元(<0.1%),模拟 AOI 边界少量超出场景足迹
            metadata = ingest_cog_mosaic([self._scene_with_nodata_holes(root, "gap", holes)],
                                         {"min_lng": 0, "min_lat": 0, "max_lng": size, "max_lat": size},
                                         root / "gap.tif", profile={"qa_band": "scl"})
            coverage = metadata["mosaic_coverage"]
            self.assertFalse(coverage["coverage_complete"])
            self.assertAlmostEqual(coverage["source_coverage_ratio"], 1 - 100 / (size * size), places=5)
            self.assertIn("缺口", coverage["coverage_note"])
            big = np.zeros((size, size), dtype=bool)
            big[:140, :150] = True  # 约 2% 真实缺口,仍必须拒绝
            with self.assertRaisesRegex(ValueError, "未完整覆盖"):
                ingest_cog_mosaic([self._scene_with_nodata_holes(root, "big", big)],
                                  {"min_lng": 0, "min_lat": 0, "max_lng": size, "max_lat": size},
                                  root / "big.tif", profile={"qa_band": "scl"})

    def _full_mosaic_scene(self, root, prefix, red_value=.1, size=1024):
        paths = []
        for name, value in (("red", red_value), ("nir", .6), ("scl", 4)):
            path = root / f"{prefix}-{name}.tif"
            self._write(path, np.full((size, size), value, dtype="float32"), from_origin(0, size, 1, 1), "EPSG:4326")
            paths.append((name, {"href": str(path), "raster:bands": [{"scale": 1, "offset": 0, "nodata": 0}]}))
        return {"item_id": prefix, "assets": paths}

    def _interrupt_after_two_windows(self):
        from .v3.runtime import ToolInterrupted
        calls = {"n": 0}
        def interrupt():
            calls["n"] += 1
            if calls["n"] == 4:  # 1 scene-open checkpoint + 2 completed windows
                raise ToolInterrupted("时限到达")
        return interrupt

    def test_mosaic_resume_keeps_progress_and_matches_one_shot_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scenes = [self._full_mosaic_scene(root, "full")]
            bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1024, "max_lat": 1024}
            target = root / "files" / "resumed.tif"
            target.parent.mkdir()
            with patch("map_api.v3.raster_products.checkpoint", side_effect=self._interrupt_after_two_windows()):
                from .v3.runtime import ToolInterrupted
                with self.assertRaises(ToolInterrupted):
                    ingest_cog_mosaic(scenes, bbox, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            partial = target.parent / ".partial" / "resume-key.tif"
            sidecar = partial.with_suffix(".json")
            self.assertTrue(partial.exists())
            state = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(len(state["done_windows"]), 2)
            self.assertEqual(state["total_windows"], 4)
            self.assertFalse(target.exists())
            metadata = ingest_cog_mosaic(scenes, bbox, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            self.assertFalse(sidecar.exists())
            self.assertFalse(partial.exists())
            self.assertEqual(metadata["mosaic_coverage"]["requested_pixel_count"], 1024 * 1024)
            one_shot = root / "files" / "one-shot.tif"
            ingest_cog_mosaic(scenes, bbox, one_shot, profile={"qa_band": "scl"})
            self.assertEqual(target.read_bytes(), one_shot.read_bytes())

    def test_mosaic_resume_discards_mismatched_signature_or_grid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scenes = [self._full_mosaic_scene(root, "full")]
            bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1024, "max_lat": 1024}
            target = root / "files" / "out.tif"
            target.parent.mkdir()
            from .v3.runtime import ToolInterrupted
            with patch("map_api.v3.raster_products.checkpoint", side_effect=self._interrupt_after_two_windows()):
                with self.assertRaises(ToolInterrupted):
                    ingest_cog_mosaic(scenes, bbox, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            # Signature mismatch: different scene hrefs under the same key must recompute.
            other = [self._full_mosaic_scene(root, "other", red_value=.2)]
            ingest_cog_mosaic(other, bbox, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            with rasterio.open(target) as output:
                self.assertAlmostEqual(float(output.read(1)[512, 512]), .2)
            self.assertFalse((target.parent / ".partial" / "resume-key.json").exists())
            # Grid mismatch: same scenes but a smaller bbox must drop the stale partial.
            with patch("map_api.v3.raster_products.checkpoint", side_effect=self._interrupt_after_two_windows()):
                with self.assertRaises(ToolInterrupted):
                    ingest_cog_mosaic(scenes, bbox, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            ingest_cog_mosaic(scenes, {**bbox, "max_lng": 512}, target, profile={"qa_band": "scl"}, progress_key="resume-key")
            with rasterio.open(target) as output:
                self.assertEqual(output.width, 512)
                self.assertEqual(output.height, 1024)
