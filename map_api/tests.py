"""核心纯函数单元测试。

只覆盖不依赖数据库/网络/外部 API 的纯逻辑函数,可直接运行:
    python manage.py test map_api
"""
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError
from django.contrib.sessions.models import Session
from django.http import JsonResponse
from django.utils import timezone
import os
import json
import tempfile
import time
from http import HTTPStatus
from io import BytesIO, StringIO
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from contextlib import ExitStack
import numpy as np
import requests
from PIL import Image
from .remote_sensing_indices import ndvi, ndwi, bsi, ndsi, summarize, otsu_threshold, quantile_threshold, get_index_function, compute_index, change_summary
from .agent.registry import ToolDefinition
from .utils.agent_tools import fetch_cog_bbox_array


class SpectralIndexTests(SimpleTestCase):
    def test_cog_reader_uses_discrete_scl_contract(self):
        response = MagicMock()
        response.content = b"fake"
        response.raise_for_status.return_value = None
        image = Image.new("L", (2, 2), 8)
        buf = BytesIO(); image.save(buf, "TIFF")
        response.content = buf.getvalue()
        with patch("map_api.utils.agent_tools.requests.get", return_value=response) as get_mock:
            fetch_cog_bbox_array({"href": "https://example.com/SCL.tif"}, {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}, "https://titiler.test", kind="scl")
        self.assertEqual(get_mock.call_args.kwargs["params"], {"url": "https://example.com/SCL.tif", "resampling": "nearest"})

    def test_indices_and_summary_are_numeric_and_masked(self):
        red = np.array([[1, 2], [0, 4]], dtype=np.float32)
        nir = np.array([[3, 2], [0, 8]], dtype=np.float32)
        result, valid = ndvi(red, nir)
        self.assertAlmostEqual(float(result[0, 0]), 0.5)
        self.assertFalse(bool(valid[1, 0]))
        self.assertEqual(summarize(result)["valid_pixel_count"], 3)
        self.assertEqual(summarize(result)["valid_pixel_ratio"], 0.75)

    def test_mismatched_bands_fail_fast(self):
        with self.assertRaises(ValueError):
            ndwi(np.zeros((2, 2)), np.zeros((3, 3)))

    def test_summary_rejects_mismatched_mask(self):
        with self.assertRaises(ValueError):
            summarize(np.zeros((2, 2)), valid_mask=np.ones((3, 3), dtype=bool))

    def test_otsu_threshold_separates_two_modes(self):
        values = np.array([[-0.7, -0.6, 0.6, 0.7]], dtype=np.float32)
        threshold = otsu_threshold(values)
        self.assertGreater(threshold, -0.7)
        self.assertLess(threshold, 0.7)

    def test_tool_definition_rejects_unknown_and_wrong_types(self):
        tool = ToolDefinition("demo", "", {
            "type": "object", "additionalProperties": False,
            "properties": {"count": {"type": "integer"}},
        }, lambda ctx, args: args)
        with self.assertRaises(ValueError):
            tool.validate_args({"extra": 1})
        with self.assertRaises(ValueError):
            tool.validate_args({"count": "1"})

    def test_bsi_and_ndsi_are_available_and_shape_safe(self):
        values = np.ones((2, 2), dtype=np.float32)
        result, valid = bsi(values, values, values, values)
        self.assertTrue(np.all(valid))
        self.assertTrue(np.allclose(result, 0))
        snow, _ = ndsi(values * 2, values)
        self.assertTrue(np.allclose(snow, 1 / 3))
        with self.assertRaises(ValueError):
            bsi(values, values, values, np.ones((1, 1)))

    def test_quantile_threshold_is_masked_and_bounded(self):
        values = np.array([[-1, 0, 1, np.nan]], dtype=np.float32)
        mask = np.array([[True, True, False, True]])
        self.assertEqual(quantile_threshold(values, 0.5, valid_mask=mask), -0.5)
        with self.assertRaises(ValueError):
            quantile_threshold(values, 1.1)

    def test_index_function_registry_resolves_supported_names(self):
        self.assertIs(get_index_function(" NDVI "), ndvi)
        with self.assertRaises(ValueError):
            get_index_function("unknown")

    def test_summary_reports_threshold_area_when_pixel_area_known(self):
        values = np.array([[0.1, 0.8, 0.9]], dtype=np.float32)
        result = summarize(values, threshold=0.5, pixel_area_m2=100)
        self.assertEqual(result["above_threshold_ratio"], 0.6667)
        self.assertEqual(result["above_threshold_area_m2"], 200.0)

    def test_compute_index_resolves_named_band_mapping(self):
        values = np.ones((2, 2), dtype=np.float32)
        result, valid = compute_index("ndvi", {"red": values, "nir": values * 3})
        self.assertTrue(np.all(valid))
        self.assertTrue(np.allclose(result, 0.5))
        with self.assertRaises(ValueError):
            compute_index("ndvi", {"red": values})

    def test_change_summary_measures_temporal_delta(self):
        before = np.array([[0.1, 0.2], [np.nan, 0.4]], dtype=np.float32)
        after = np.array([[0.3, 0.1], [0.9, 0.8]], dtype=np.float32)
        result = change_summary(before, after, min_delta=0.15)
        self.assertEqual(result["valid_pixel_count"], 3)
        self.assertEqual(result["changed_pixel_ratio"], 0.6667)
        with self.assertRaises(ValueError):
            change_summary(before, np.zeros((1, 1)))

from map_api.utils.smart_query_analyzer import analyze_query, adaptive_resolution, _build_clip_query
from map_api.utils.agent_tools import (
    build_agent_plan, compute_ndwi_from_arrays, deterministic_extract_slots,
    merge_agent_slots, parse_amap_boundary, parse_amap_boundary_geometry, polygon_mask_for_bbox,
    compute_ndwi_mosaic_summary
)
from map_api.utils.active_perception import (
    extract_bbox_from_response, extract_answer_text, measure_bbox, pixel_bbox_to_geo,
    map_bbox_to_original, cut_image_geom, resize_image, model_wants_zoom
)
from map_api.utils.get_satellite_image import fetch_satellite_image, haversine_distance
from map_api.utils.analysis_strategy import build_analysis_strategy
from map_api.models import AgentSession, ChatHistory, DownloadTask, ImageryScene, ExternalServiceHealth, ReportJob, ApiRateLimitBucket
from map_api.media_paths import SAVE_DIR
from map_api.imagery_sources.mapbox import MapboxProvider
from map_api.imagery_sources.earth_search import EarthSearchProvider, score_candidate, stac_datetime_range
from map_api.views import (
    ANALYSIS_MODES, compute_image_plan, normalize_bbox,
    imagery_quality_payload, analysis_confidence_payload, source_recommendation_payload,
    run_agent_session, select_best_sentinel_candidate, bbox_intersection_ratio,
    bbox_union_coverage_ratio, compose_sentinel_mosaic, image_valid_ratio,
    crop_sentinel_nodata_border, image_plan_for_bbox_and_size,
    select_sentinel_scene_candidates, sentinel_retrieval_result, _download_progress,
    safe_media_path, normalize_model_answer, _stage1_scale, _measure_and_locate,
    apply_quality_guard,
)
from map_api.agent.loop import _quality_requires_safe_fallback, _persist, _touch_worker_lease, _model_error_retryable
from map_api.agent.tools import _tool_compute_ndwi
from map_api.agent.events import normalized_payload, EVENT_STATUSES
from map_api.orchestrator import (
    _agent_step, _agent_wait, _agent_fetch_mapbox, _run_agent_background,
    _sort_sentinel_preview_candidates, _rank_sentinel_grid_candidates,
)
from map_api.middleware import reset_rate_limit_state, consume_rate_limit
from map_api.report_jobs import execute_report_job
from map_api.download_jobs import execute_download_task
from satellite_map.env import load_project_env


class AnalyzeQueryTests(SimpleTestCase):
    def test_detail_question_suggests_stages(self):
        r = analyze_query("数一下这个停车场里有多少辆车")
        self.assertTrue(r["is_detail"])
        self.assertTrue(r["suggest_stages"])
        self.assertIn("vehicle", r["entities"])

    def test_macro_question_no_stages(self):
        r = analyze_query("分析这片区域的整体用地类型构成")
        self.assertTrue(r["is_macro"])
        self.assertFalse(r["suggest_stages"])

    def test_entities_and_spatial_hints(self):
        r = analyze_query("看看东边的水体有没有污染")
        self.assertIn("water", r["entities"])
        self.assertTrue(r["spatial_hints"].get("east"))

    def test_empty_defaults_to_macro(self):
        r = analyze_query("这是什么")
        self.assertEqual(r["intent"], "macro")


class QualityGuardTests(SimpleTestCase):
    def test_redacts_precise_claims_on_low_quality_scene(self):
        scene = SimpleNamespace(
            gsd_m=228.0,
            cloud_percent=51.0,
            metadata={"target_coverage_ratio": 0.35, "valid_image_ratio": 0.30},
        )
        answer, meta = apply_quality_guard(
            "河道宽度约120m，养殖塘直径约80m，含沙量约35mg/L。",
            scene=scene,
        )
        self.assertTrue(meta["triggered"])
        self.assertEqual(meta["redacted_count"], 3)
        self.assertNotIn("120m", answer)
        self.assertNotIn("35mg/L", answer)
        self.assertIn("当前影像无法可靠估计", answer)

    def test_keeps_normal_answer_on_good_scene(self):
        scene = SimpleNamespace(gsd_m=10.0, cloud_percent=5.0, metadata={"target_coverage_ratio": 1.0, "valid_image_ratio": 1.0})
        answer, meta = apply_quality_guard("区域水面占比约12%。", scene=scene)
        self.assertFalse(meta["triggered"])
        self.assertEqual(answer, "区域水面占比约12%。")

    @patch("map_api.agent.loop._ctx_scene")
    def test_agent_uses_safe_fallback_for_very_coarse_scene(self, scene_lookup):
        scene_lookup.return_value = SimpleNamespace(gsd_m=228.0, metadata={}, cloud_percent=5.0)
        self.assertTrue(_quality_requires_safe_fallback({"scene_id": 1}))


class SentinelSelectionTests(SimpleTestCase):
    def test_cloud_percent_is_primary_selection_signal(self):
        low = SimpleNamespace(cloud_percent=3.0, suitability_score=50, acquired_at=None, bbox={"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1})
        high = SimpleNamespace(cloud_percent=35.0, suitability_score=100, acquired_at=None, bbox=low.bbox)
        self.assertIs(select_sentinel_scene_candidates([low, high], low.bbox, min_coverage=.8)[0][0], low)

    def test_preview_sort_keeps_low_cloud_candidate_ahead_of_higher_score(self):
        bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}
        low_cloud = SimpleNamespace(cloud_percent=4.0, suitability_score=35, acquired_at=None, bbox=bbox)
        high_cloud = SimpleNamespace(cloud_percent=42.0, suitability_score=99, acquired_at=None, bbox=bbox)
        ranked = _sort_sentinel_preview_candidates([high_cloud, low_cloud])
        self.assertIs(ranked[0], low_cloud)

    def test_model_read_timeout_is_retryable(self):
        self.assertTrue(_model_error_retryable(requests.exceptions.ReadTimeout("temporary timeout")))

    def test_model_auth_error_is_not_retryable(self):
        response = SimpleNamespace(status_code=401)
        error = requests.HTTPError("unauthorized", response=response)
        self.assertFalse(_model_error_retryable(error))

    @patch.dict(os.environ, {"AGENT_VISION_ASSIST": "0", "AGENT_VISION_TRIGGER": "key_checkpoints"}, clear=False)
    def test_vision_assist_can_be_disabled_by_configuration(self):
        # 配置开关由循环读取；这里锁定环境变量语义，避免部署后开关失效。
        self.assertEqual(os.environ["AGENT_VISION_ASSIST"], "0")

    def test_grid_sort_uses_cloud_after_spatial_overlap(self):
        tile = {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}
        low_cloud = SimpleNamespace(cloud_percent=2.0, suitability_score=30, acquired_at=None, bbox=tile)
        high_cloud = SimpleNamespace(cloud_percent=55.0, suitability_score=100, acquired_at=None, bbox=tile)
        ranked = _rank_sentinel_grid_candidates([high_cloud, low_cloud], tile)
        self.assertIs(ranked[0], low_cloud)


class AdaptiveResolutionTests(SimpleTestCase):
    """锁定重构后的 adaptive_resolution 行为(修复 {max_dim:max_dim} 笔误后)。"""

    def test_detail_bumps_to_3584(self):
        self.assertEqual(adaptive_resolution("识别屋顶材质和裂缝", 2560), 3584)

    def test_macro_caps_at_1536(self):
        self.assertEqual(adaptive_resolution("整体城市形态分析", 3584), 1536)

    def test_base_dim_respected_for_detail(self):
        # base_dim 已高于 3584 时应保留 base_dim
        self.assertEqual(adaptive_resolution("数车辆数量", 4096), 4096)


class ClipQueryTests(SimpleTestCase):
    def test_builds_english_phrase_from_entities(self):
        q = _build_clip_query({"water": 2, "building": 1})
        self.assertIsNotNone(q)
        self.assertIn("water", q)
        # 出现次数多的实体排前面
        self.assertLess(q.index("water"), q.index("buildings"))

    def test_no_entities_returns_none(self):
        self.assertIsNone(_build_clip_query({}))


class ExtractBboxTests(SimpleTestCase):
    def test_bbox_2d_json(self):
        text = '<think>需要放大 [{"bbox_2d": [100, 200, 300, 400], "label": "建筑"}]</think>'
        self.assertEqual(extract_bbox_from_response(text), [[100, 200, 300, 400]])

    def test_scale_factor_applied(self):
        text = '"bbox_2d": [100, 200, 300, 400]'
        self.assertEqual(extract_bbox_from_response(text, scale_factor=2.0),
                         [[200, 400, 600, 800]])

    def test_no_bbox_returns_empty(self):
        self.assertEqual(extract_bbox_from_response("无需放大，直接分析"), [])

    def test_empty_input(self):
        self.assertEqual(extract_bbox_from_response(""), [])


class ExtractAnswerTests(SimpleTestCase):
    def test_answer_tag(self):
        text = "<think>推理</think><answer>这是结论</answer>"
        self.assertEqual(extract_answer_text(text), "这是结论")

    def test_fallback_after_think(self):
        text = "<think>推理过程</think>剩余正文"
        self.assertEqual(extract_answer_text(text), "剩余正文")

    def test_plain_text_passthrough(self):
        self.assertEqual(extract_answer_text("普通回答"), "普通回答")


class MeasureBboxTests(SimpleTestCase):
    def test_size_from_gsd(self):
        # 100×50 像素 bbox,GSD 2 m/像素 → 200m × 100m,20000 m²
        w, h, area = measure_bbox([0, 0, 100, 50], 2.0)
        self.assertEqual((w, h, area), (200.0, 100.0, 20000.0))

    def test_zero_gsd(self):
        w, h, area = measure_bbox([10, 10, 110, 60], 0)
        self.assertEqual((w, h, area), (0, 0, 0))


class PixelBboxToGeoTests(SimpleTestCase):
    GEO = {"min_lng": 100.0, "max_lng": 102.0, "min_lat": 30.0, "max_lat": 32.0}

    def test_center_maps_to_geo_center(self):
        # 1000×1000 图的正中 bbox → 经纬度范围正中 (101, 31)
        lng, lat = pixel_bbox_to_geo([400, 400, 600, 600], 1000, 1000, self.GEO)
        self.assertAlmostEqual(lng, 101.0, places=6)
        self.assertAlmostEqual(lat, 31.0, places=6)

    def test_top_left_corner(self):
        # 左上角像素 → (min_lng, max_lat)(图像 y 向下对应纬度向上)
        lng, lat = pixel_bbox_to_geo([0, 0, 0, 0], 1000, 1000, self.GEO)
        self.assertAlmostEqual(lng, 100.0, places=6)
        self.assertAlmostEqual(lat, 32.0, places=6)

    def test_missing_geo_returns_none(self):
        self.assertIsNone(pixel_bbox_to_geo([0, 0, 10, 10], 1000, 1000, None))

    def test_bad_geo_returns_none(self):
        self.assertIsNone(pixel_bbox_to_geo([0, 0, 10, 10], 1000, 1000, {"min_lng": "x"}))


class MapBboxToOriginalTests(SimpleTestCase):
    """多级迭代放大的坐标回溯——全计划最易出 bug 处,重点钉死。"""

    def test_no_resize_identity_offset(self):
        # 裁剪框 (100,100)-(600,600) 在原图;裁剪图未缩放(500×500)
        # 模型在裁剪图上给 (0,0,500,500) → 回到原图应是裁剪框本身
        out = map_bbox_to_original([0, 0, 500, 500], (100, 100, 600, 600), 500, 500)
        self.assertEqual(out, [100, 100, 600, 600])

    def test_with_downscale(self):
        # 裁剪框 1000×1000 在原图 (0,0)-(1000,1000),被缩到 500×500 落盘
        # 模型在 500px 图上给中心 (250,250,500,500) → 原图放大 2 倍 (500,500,1000,1000)
        out = map_bbox_to_original([250, 250, 500, 500], (0, 0, 1000, 1000), 500, 500)
        self.assertEqual(out, [500, 500, 1000, 1000])

    def test_offset_and_scale(self):
        # 裁剪框 (200,100)-(700,400)=500×300,缩到 250×150;模型给 (0,0,250,150)→整框
        out = map_bbox_to_original([0, 0, 250, 150], (200, 100, 700, 400), 250, 150)
        self.assertEqual(out, [200, 100, 700, 400])


class HaversineTests(SimpleTestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(haversine_distance(116.4, 39.9, 116.4, 39.9), 0.0, places=3)

    def test_known_distance(self):
        # 北京 → 上海 约 1067 km,允许 ±20 km 误差
        d = haversine_distance(116.40, 39.90, 121.47, 31.23)
        self.assertAlmostEqual(d / 1000, 1067, delta=20)


class MapboxFetchTests(SimpleTestCase):
    def test_fetch_satellite_image_fails_fast_without_token(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"MAPBOX_TOKEN": ""}, clear=False), \
                patch("map_api.utils.get_satellite_image._fetch_tile") as mocked:
            out = fetch_satellite_image(1, 2, 1.01, 2.01, save_dir=tmp, file_name="sat_missing_token.jpg", target_resolution=64)
        self.assertIsNone(out)
        mocked.assert_not_called()

    def test_fetch_satellite_image_reads_mapbox_token_at_call_time(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"MAPBOX_TOKEN": "runtime-token"}, clear=False), \
                patch("map_api.utils.get_satellite_image._fetch_tile", return_value=Image.new("RGB", (16, 16), (80, 120, 160))) as mocked:
            out = fetch_satellite_image(
                1,
                2,
                1.01,
                2.01,
                save_dir=tmp,
                file_name="sat_runtime_token.jpg",
                target_resolution=64,
            )

        self.assertIsNotNone(out)
        requested_url = mocked.call_args.args[0]
        self.assertIn("access_token=runtime-token", requested_url)

    def test_fetch_satellite_image_honors_process_proxy_by_default(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"MAPBOX_TOKEN": "runtime-token", "SATELLITESENSE_DIRECT_HTTP": ""}, clear=False), \
                patch("map_api.utils.get_satellite_image._fetch_tile", return_value=Image.new("RGB", (16, 16), (80, 120, 160))) as mocked:
            fetch_satellite_image(1, 2, 1.01, 2.01, save_dir=tmp, file_name="sat_proxy.jpg", target_resolution=64)
        self.assertIsNone(mocked.call_args.args[1])

    def test_fetch_satellite_image_rejects_blank_mapbox_response(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"MAPBOX_TOKEN": "runtime-token"}, clear=False), \
                patch("map_api.utils.get_satellite_image._fetch_tile", return_value=Image.new("RGB", (64, 64), (0, 0, 0))):
            out = fetch_satellite_image(
                1,
                2,
                1.01,
                2.01,
                save_dir=tmp,
                file_name="sat_blank.jpg",
                target_resolution=64,
            )

        self.assertIsNone(out)
        self.assertFalse(os.path.exists(os.path.join(tmp, "sat_blank.jpg")))

    def test_large_mapbox_marks_done_only_after_atomic_save(self):
        progress_events = []

        def progress_callback(file_name, info):
            progress_events.append(info.copy())

        def fake_save(img, full_save_path, quality):
            statuses = [event.get("status") for event in progress_events]
            self.assertNotIn("done", statuses)
            self.assertNotIn("partial", statuses)
            img.crop((0, 0, 32, 32)).save(full_save_path, "JPEG")

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"MAPBOX_TOKEN": "runtime-token"}, clear=False), \
                patch("map_api.utils.get_satellite_image._fetch_tile", return_value=Image.new("RGB", (700, 700), (80, 120, 160))), \
                patch("map_api.utils.get_satellite_image._save_jpeg_atomic", side_effect=fake_save):
            out = fetch_satellite_image(
                1,
                2,
                1.2,
                2.2,
                save_dir=tmp,
                file_name="sat_large.jpg",
                target_resolution=1400,
                progress_callback=progress_callback,
            )
            self.assertIsNotNone(out)
            self.assertEqual(progress_events[-1]["status"], "done")
            self.assertTrue(os.path.exists(os.path.join(tmp, "sat_large.jpg")))


class ImagePlanTests(SimpleTestCase):
    def test_normalize_bbox_reversed_input(self):
        out = normalize_bbox({"min_lng": 2, "max_lng": 1, "min_lat": 4, "max_lat": 3})
        self.assertEqual(out, (1.0, 3.0, 2.0, 4.0))

    def test_normalize_bbox_rejects_zero_area(self):
        with self.assertRaises(ValueError):
            normalize_bbox({"min_lng": 1, "max_lng": 1, "min_lat": 3, "max_lat": 4})

    def test_normalize_bbox_rejects_non_finite_coordinates(self):
        with self.assertRaisesRegex(ValueError, "有限数字"):
            normalize_bbox({"min_lng": "nan", "max_lng": 2, "min_lat": 3, "max_lat": 4})

    def test_vertical_region_uses_actual_output_height_for_gsd(self):
        plan = compute_image_plan(100, 10, 100.01, 11, 1280)
        self.assertLess(plan["total_w"], plan["total_h"])
        expected_lat_gsd = (1 * 110574) / plan["total_h"]
        self.assertAlmostEqual(plan["gsd_m"], expected_lat_gsd, delta=expected_lat_gsd * 0.2)


class AnalysisModeTests(SimpleTestCase):
    def test_default_precise_mode_uses_plus_and_active_perception(self):
        self.assertEqual(ANALYSIS_MODES["precise"]["model"], "qwen3-vl-plus")
        self.assertTrue(ANALYSIS_MODES["precise"]["active_perception"])

    def test_fast_mode_uses_flash_without_active_perception(self):
        self.assertEqual(ANALYSIS_MODES["fast"]["model"], "qwen3-vl-flash")
        self.assertFalse(ANALYSIS_MODES["fast"]["active_perception"])


class AgentToolTests(SimpleTestCase):
    def test_grid_ndwi_keeps_valid_tiles_when_one_tile_fails(self):
        scene = SimpleNamespace(
            id=1,
            source="sentinel2",
            min_lng=0,
            min_lat=0,
            max_lng=3,
            max_lat=1,
            metadata={
                "grid_mosaic": True,
                "valid_image_ratio": 0.9,
                "target_coverage_ratio": 0.9,
                "mosaic_candidates": [{"product_id": "p1"}],
                "mosaic_items": [
                    {"product_id": "p1", "tile_bbox": {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}},
                    {"product_id": "p1", "tile_bbox": {"min_lng": 1, "min_lat": 0, "max_lng": 2, "max_lat": 1}},
                    {"product_id": "p1", "tile_bbox": {"min_lng": 2, "min_lat": 0, "max_lng": 3, "max_lat": 1}},
                ],
            },
        )
        good_summary = {"available": True, "sample_size_px": 100, "water_ratio": 0.2, "mean_ndwi": 0.1, "threshold": 0.1}
        with patch("map_api.agent.tools._resolve_scene", return_value=scene), \
                patch("map_api.agent.tools._views._candidate_from_mosaic_metadata", return_value=SimpleNamespace()), \
                patch("map_api.agent.tools._views.compute_ndwi_summary", side_effect=[RuntimeError("TiTiler timeout"), good_summary, good_summary]):
            result = _tool_compute_ndwi({"scene_id": 1, "slots": {}, "bbox": None}, {})
        self.assertTrue(result["result"]["available"])
        self.assertEqual(result["result"]["grid_count"], 2)
        self.assertEqual(result["result"]["grid_failed_count"], 1)

    def test_agent_mapbox_rejects_partial_stitched_image(self):
        bbox = {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.5, "max_lat": 22.9}
        temp_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", "agent_partial_test.jpg")
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)
        with open(temp_path, "wb") as handle:
            handle.write(b"partial")
        try:
            def fake_fetch(*args, **kwargs):
                file_name = args[5] if len(args) > 5 else kwargs["file_name"]
                _download_progress[file_name] = {"status": "partial", "failed": 1}
                return temp_path

            with patch("map_api.orchestrator.fetch_satellite_image", side_effect=fake_fetch):
                with self.assertRaisesRegex(ValueError, "部分瓦片"):
                    _agent_fetch_mapbox(bbox)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_extracts_nanning_april_water_slots(self):
        slots = deterministic_extract_slots("帮我调查南宁市在2026年四月的水体情况")
        self.assertEqual(slots["place_name"], "南宁市")
        self.assertEqual(slots["date_start"], "2026-04-01")
        self.assertEqual(slots["date_end"], "2026-04-30")
        self.assertEqual(slots["task"], "water")
        self.assertEqual(slots["source"], "sentinel2")

    def test_normalizes_model_task_aliases(self):
        slots = merge_agent_slots({"task": "water_body_monitoring", "source": "sentinel2"}, "调查水体")
        self.assertEqual(slots["task"], "water")

    def test_recent_without_explicit_period_uses_rolling_window(self):
        slots = merge_agent_slots(
            {"date_start": "2025-01-01", "date_end": "2025-01-31", "task": "vegetation", "source": "sentinel2"},
            "调查青秀区近期植被情况",
            today=date(2026, 9, 4),
        )
        self.assertEqual(slots["date_start"], "2026-06-06")
        self.assertEqual(slots["date_end"], "2026-09-04")
        self.assertEqual(slots["time_granularity"], "rolling_90d")

    def test_extracts_relative_current_year_spring(self):
        slots = deterministic_extract_slots("帮我调查南宁今年春季的水体情况", today=date(2026, 6, 7))
        self.assertEqual(slots["date_start"], "2026-03-01")
        self.assertEqual(slots["date_end"], "2026-05-31")
        self.assertEqual(slots["time_granularity"], "season")

    def test_rule_relative_dates_override_model_slots(self):
        slots = merge_agent_slots(
            {"date_start": "2024-03-01", "date_end": "2024-05-31", "time_granularity": "season"},
            "帮我调查南宁今年春季的水体情况",
            today=date(2026, 6, 7),
        )
        self.assertEqual(slots["date_start"], "2026-03-01")
        self.assertEqual(slots["date_end"], "2026-05-31")

    def test_stac_datetime_range_uses_rfc3339(self):
        self.assertEqual(
            stac_datetime_range("2025-03-01", "2025-05-31"),
            "2025-03-01T00:00:00Z/2025-05-31T23:59:59Z",
        )

    def test_bbox_intersection_ratio_flags_partial_sentinel_tile(self):
        target = {"min_lng": 107.0, "min_lat": 22.0, "max_lng": 109.0, "max_lat": 24.0}
        tile = {"min_lng": 107.0, "min_lat": 22.0, "max_lng": 108.0, "max_lat": 23.0}
        self.assertEqual(bbox_intersection_ratio(target, tile), 0.25)

    def test_image_valid_ratio_detects_mostly_blank_render(self):
        buf = BytesIO()
        img = Image.new("RGB", (20, 20), (0, 0, 0))
        for x in range(2):
            for y in range(2):
                img.putpixel((x, y), (80, 120, 160))
        img.save(buf, "PNG")
        self.assertLess(image_valid_ratio(buf.getvalue()), 0.02)

    def test_crop_sentinel_nodata_border_updates_effective_bbox(self):
        img = Image.new("RGB", (4, 2), (0, 0, 0))
        for x in range(2, 4):
            for y in range(2):
                img.putpixel((x, y), (90, 120, 80))
        buf = BytesIO()
        img.save(buf, "PNG")
        bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 4, "max_lat": 2}

        out = crop_sentinel_nodata_border(buf.getvalue(), bbox)
        cropped = Image.open(BytesIO(out["image_bytes"]))

        self.assertTrue(out["metadata"]["applied"])
        self.assertEqual(cropped.size, (2, 2))
        self.assertEqual(out["bbox"], {"min_lng": 2.0, "max_lng": 4.0, "max_lat": 2.0, "min_lat": 0.0})
        self.assertEqual(out["plan"]["total_w"], 2)
        self.assertEqual(out["metadata"]["valid_image_ratio_after_crop"], 1.0)

    def test_crop_sentinel_nodata_border_keeps_full_valid_image(self):
        buf = BytesIO()
        Image.new("RGB", (4, 2), (90, 120, 80)).save(buf, "PNG")
        bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 4, "max_lat": 2}

        out = crop_sentinel_nodata_border(buf.getvalue(), bbox)

        self.assertFalse(out["metadata"]["applied"])
        self.assertEqual(out["bbox"], bbox)

    def test_bbox_union_coverage_merges_partial_sentinel_tiles(self):
        target = {"min_lng": 0, "min_lat": 0, "max_lng": 2, "max_lat": 2}
        tiles = [
            {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 2},
            {"min_lng": 1, "min_lat": 0, "max_lng": 2, "max_lat": 2},
        ]
        self.assertEqual(bbox_union_coverage_ratio(target, tiles), 1.0)

    def test_image_plan_for_bbox_and_size_keeps_geometry_consistent(self):
        bbox = {"min_lng": 100.0, "min_lat": 10.0, "max_lng": 101.0, "max_lat": 11.0}
        plan = image_plan_for_bbox_and_size(bbox, 640, 320)
        self.assertEqual(plan["total_w"], 640)
        self.assertEqual(plan["total_h"], 320)
        self.assertGreater(plan["gsd_m"], 0)

    def test_compose_sentinel_mosaic_fills_valid_pixels_from_multiple_tiles(self):
        first = Image.new("RGB", (4, 2), (0, 0, 0))
        second = Image.new("RGB", (4, 2), (0, 0, 0))
        for x in range(2):
            for y in range(2):
                first.putpixel((x, y), (100, 30, 30))
        for x in range(2, 4):
            for y in range(2):
                second.putpixel((x, y), (30, 100, 30))
        first_buf = BytesIO()
        second_buf = BytesIO()
        first.save(first_buf, "PNG")
        second.save(second_buf, "PNG")
        candidate = type("Candidate", (), {"product_id": "p", "item_id": "i"})()

        mosaic, valid_ratio, items = compose_sentinel_mosaic(
            [
                {"candidate": candidate, "image_bytes": first_buf.getvalue()},
                {"candidate": candidate, "image_bytes": second_buf.getvalue()},
            ],
            4,
            2,
        )

        self.assertGreater(image_valid_ratio(mosaic), 0.95)
        self.assertEqual(valid_ratio, 1.0)
        self.assertEqual(len(items), 2)

    def test_select_sentinel_candidates_builds_mosaic_when_single_scene_is_insufficient(self):
        provider = EarthSearchProvider()
        left = provider.candidate_from_item({
            "id": "S2_LEFT",
            "collection": "sentinel-2-l2a",
            "bbox": [0, 0, 1, 2],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "eo:cloud_cover": 5,
                "s2:product_uri": "S2_LEFT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/left.tif", "gsd": 10}},
        })
        right = provider.candidate_from_item({
            "id": "S2_RIGHT",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 0, 2, 2],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "eo:cloud_cover": 5,
                "s2:product_uri": "S2_RIGHT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/right.tif", "gsd": 10}},
        })

        selected, coverage, method = select_sentinel_scene_candidates(
            [left, right],
            {"min_lng": 0, "min_lat": 0, "max_lng": 2, "max_lat": 2},
            min_coverage=0.8,
        )

        self.assertEqual(len(selected), 2)
        self.assertEqual(coverage, 1.0)
        self.assertEqual(method, "same_day_mosaic")

    def test_sentinel_retrieval_rejects_large_nodata_single_render(self):
        provider = EarthSearchProvider()
        candidate = provider.candidate_from_item({
            "id": "S2_BAD_EDGE",
            "collection": "sentinel-2-l2a",
            "bbox": [0, 0, 4, 2],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "eo:cloud_cover": 5,
                "s2:product_uri": "S2_BAD_EDGE.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/bad.tif", "gsd": 10}},
        })
        img = Image.new("RGB", (4, 2), (0, 0, 0))
        for x in range(2, 4):
            for y in range(2):
                img.putpixel((x, y), (90, 120, 80))
        buf = BytesIO()
        img.save(buf, "PNG")
        fake_provider = type("Provider", (), {
            "render_candidate_jpeg": lambda self, c, bbox, width, height: buf.getvalue()
        })()

        with patch("map_api.sentinel_pipeline.find_cached_sentinel_scene", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                sentinel_retrieval_result(
                    fake_provider,
                    [candidate],
                    {"min_lng": 0, "min_lat": 0, "max_lng": 4, "max_lat": 2},
                    {"total_w": 4, "total_h": 2, "gsd_m": 1, "area_km2": 1},
                    4,
                    min_coverage=0.9,
                    min_valid_ratio=0.4,
                )

        self.assertIn("no-data", str(ctx.exception))

    def test_merges_model_slots_with_rule_defaults(self):
        slots = merge_agent_slots({"place_name": "南宁市"}, "调查2026年四月水体")
        self.assertEqual(slots["place_name"], "南宁市")
        self.assertEqual(slots["task"], "water")
        self.assertEqual(slots["source"], "sentinel2")
        self.assertEqual(slots["mode"], "precise")

    def test_parse_amap_boundary_to_bbox(self):
        bbox = parse_amap_boundary("108.1,22.1;108.5,22.1|108.5,22.9;108.1,22.9")
        self.assertEqual(bbox, {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.5, "max_lat": 22.9})

    def test_parse_amap_boundary_geometry_preserves_rings(self):
        geometry = parse_amap_boundary_geometry(
            "108.1,22.1;108.5,22.1;108.5,22.9;108.1,22.9|108.2,22.2;108.3,22.2;108.3,22.3;108.2,22.3"
        )
        self.assertEqual(len(geometry["polygon"]), 2)
        self.assertEqual(geometry["polygon"][0][0], geometry["polygon"][0][-1])
        self.assertEqual(geometry["bbox"]["min_lng"], 108.1)

    def test_polygon_mask_clips_bbox_pixels(self):
        mask = polygon_mask_for_bbox(
            [[[0, 0], [1, 0], [0, 1], [0, 0]]],
            {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1},
            (4, 4),
        )
        self.assertIsNotNone(mask)
        self.assertGreater(int(mask.sum()), 0)
        self.assertLess(int(mask.sum()), 16)

    def test_polygon_mask_unions_multiple_amap_rings(self):
        mask = polygon_mask_for_bbox(
            [
                [[0, 0], [0.4, 0], [0.4, 1], [0, 1], [0, 0]],
                [[0.6, 0], [1, 0], [1, 1], [0.6, 1], [0.6, 0]],
            ],
            {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1},
            (10, 10),
        )
        self.assertGreater(int(mask.sum()), 0)
        self.assertLess(int(mask.sum()), 100)

    def test_ndwi_from_arrays(self):
        green = np.array([[0.8, 0.2], [0.7, 0.1]], dtype=np.float32)
        nir = np.array([[0.1, 0.5], [0.2, 0.6]], dtype=np.float32)
        out = compute_ndwi_from_arrays(green, nir, threshold=0.1, min_valid_pixels=1)
        self.assertTrue(out["available"])
        self.assertEqual(out["water_percent"], 50.0)
        self.assertEqual(out["method"], "NDWI=(Green-NIR)/(Green+NIR)")

    def test_ndwi_mosaic_passes_district_polygon_to_each_candidate(self):
        candidates = [SimpleNamespace(product_id="S2-A", item_id="", assets={})]
        polygon = [[[0, 0], [1, 0], [1, 1], [0, 0]]]
        with patch("map_api.utils.agent_tools.compute_ndwi_summary", return_value={
            "available": True,
            "sample_size_px": 4,
            "water_ratio": 0.25,
            "mean_ndwi": 0.1,
            "max_ndwi": 0.4,
        }) as mocked:
            result = compute_ndwi_mosaic_summary(candidates, {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}, polygon=polygon)
        self.assertTrue(result["available"])
        self.assertTrue(result["polygon_clipped"])
        self.assertEqual(mocked.call_args.kwargs["polygon"], polygon)

    def test_execution_event_payload_is_public_decision_summary_and_redacted(self):
        payload = normalized_payload("model_decision", {
            "phase": "retrieve_imagery",
            "thought": "根据 Authorization: Bearer secret-token 选择 Sentinel-2",
            "why": ["目标是区域级绿化"],
            "action": {"type": "tool_call", "name": "search_sentinel_imagery"},
        })
        self.assertEqual(payload["phase"], "retrieve_imagery")
        self.assertEqual(payload["status"], EVENT_STATUSES["model_decision"])
        self.assertNotIn("thought", payload)
        self.assertNotIn("secret-token", str(payload))
        self.assertEqual(payload["summary"], "根据 Authorization: Bearer [REDACTED] 选择 Sentinel-2")

    def test_sse_and_transcript_routes_exist(self):
        from django.urls import reverse
        self.assertEqual(reverse("agent_session_events_stream", kwargs={"session_id": 1}), "/api/agent/sessions/1/events/stream/")
        self.assertEqual(reverse("agent_session_transcript", kwargs={"session_id": 1}), "/api/agent/sessions/1/transcript/")

    def test_ndwi_rejects_tiny_valid_sample(self):
        green = np.ones((2, 2), dtype=np.float32)
        nir = np.ones((2, 2), dtype=np.float32)
        with self.assertRaises(ValueError):
            compute_ndwi_from_arrays(green, nir, min_valid_pixels=8)

    def test_build_agent_plan_uses_deepseek_slots(self):
        with patch("map_api.utils.agent_tools.call_deepseek_json", return_value={
            "place_name": "南宁市",
            "date_start": "2026-04-01",
            "date_end": "2026-04-30",
            "task": "water",
            "source": "sentinel2",
            "mode": "precise",
        }):
            plan = build_agent_plan("帮我调查南宁市在2026年四月的水体情况")
        self.assertEqual(plan["slots"]["task"], "water")
        self.assertEqual(plan["slots"]["source"], "sentinel2")
        self.assertIn("task_strategy", plan)


class SystemHealthTests(TestCase):
    def test_spectral_indices_catalog_endpoint(self):
        response = self.client.get("/api/analysis/indices/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 200)
        self.assertIn("mndwi", payload["data"]["indices"])

    def test_spectral_indices_catalog_rejects_non_get(self):
        response = self.client.post("/api/analysis/indices/")
        self.assertEqual(response.status_code, 405)

    def test_health_reports_core_configuration_without_secret_values(self):
        with patch.dict(os.environ, {
            "MAPBOX_TOKEN": "secret-mapbox-token",
            "DASHSCOPE_API_KEY": "secret-dashscope-key",
            "DEEPSEEK_API_KEY": "secret-deepseek-key",
            "GLM_API_KEY": "secret-glm-key",
            "AGENT_MODEL": "glm-5.3-flash",
            "AGENT_VISION_ASSIST": "1",
            "AMAP_KEY": "secret-amap-key",
        }, clear=False):
            r = self.client.get("/api/system/health/")

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["checks"]["database"])
        self.assertTrue(data["checks"]["media_root_writable"])
        self.assertTrue(data["checks"]["satellite_image_dir_writable"])
        self.assertTrue(data["config"]["mapbox_token"])
        self.assertTrue(data["config"]["dashscope_api_key"])
        self.assertTrue(data["config"]["deepseek_api_key"])
        self.assertIn("spectral_indices", data)
        self.assertIn("ndvi", data["spectral_indices"])
        self.assertTrue(data["config"]["amap_key"])
        self.assertEqual(data["analysis_modes"]["precise"]["model"], "qwen3-vl-plus")
        self.assertEqual(data["analysis_modes"]["fast"]["model"], "qwen3-vl-flash")
        self.assertEqual(data["imagery_strategy"]["id"], "task_adaptive_dual_source")
        self.assertEqual(data["imagery_strategy"]["default_source"], "mapbox")
        self.assertEqual(data["imagery_strategy"]["recommendation_endpoint"], "/api/imagery/recommend-source/")
        self.assertEqual(data["imagery_sources"]["mapbox"]["role"], "default_high_resolution_reference")
        self.assertEqual(data["imagery_sources"]["sentinel2"]["role"], "optional_recent_traceable_public")
        self.assertIn("近期态势", data["imagery_sources"]["sentinel2"]["recommended_for"])
        pipeline_ids = {item["id"] for item in data["smart_pipeline"]}
        self.assertIn("adaptive_source_routing", pipeline_ids)
        self.assertIn("active_perception", pipeline_ids)
        self.assertIn("evidence_confidence", pipeline_ids)
        self.assertEqual(data["agent"]["controller_model"], "glm-5.3-flash")
        self.assertTrue(data["agent"]["vision_assist"])
        self.assertTrue(data["agent"]["available"])

        raw = json.dumps(r.json(), ensure_ascii=False)
        self.assertNotIn("secret-mapbox-token", raw)
        self.assertNotIn("secret-dashscope-key", raw)
        self.assertNotIn("secret-deepseek-key", raw)
        self.assertNotIn("secret-glm-key", raw)
        self.assertNotIn("secret-amap-key", raw)

    def test_health_degrades_when_required_keys_are_missing(self):
        with patch.dict(os.environ, {
            "MAPBOX_TOKEN": "",
            "DASHSCOPE_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "AMAP_KEY": "",
        }, clear=False):
            r = self.client.get("/api/system/health/")

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "degraded")
        self.assertFalse(data["config"]["mapbox_token"])
        self.assertFalse(data["config"]["dashscope_api_key"])
        self.assertFalse(data["imagery_sources"]["mapbox"]["available"])

    def test_health_rejects_non_get(self):
        r = self.client.post("/api/system/health/")
        self.assertEqual(r.status_code, 405)


class EnvLoadingTests(SimpleTestCase):
    def test_load_project_env_reads_dotenv_without_overriding_existing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("SATELLITE_TEST_ENV_NEW=from-dotenv\n")
                f.write("SATELLITE_TEST_ENV_EXISTING=from-dotenv\n")

            os.environ.pop("SATELLITE_TEST_ENV_NEW", None)
            with patch.dict(os.environ, {"SATELLITE_TEST_ENV_EXISTING": "already-set"}, clear=False):
                self.assertTrue(load_project_env(tmp))
                self.assertEqual(os.environ.get("SATELLITE_TEST_ENV_NEW"), "from-dotenv")
                self.assertEqual(os.environ.get("SATELLITE_TEST_ENV_EXISTING"), "already-set")

            os.environ.pop("SATELLITE_TEST_ENV_NEW", None)


class ImageryMetadataTests(SimpleTestCase):
    def test_mapbox_provider_marks_scene_as_reference(self):
        meta = MapboxProvider().metadata_for_bbox({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertEqual(meta.source, "mapbox")
        self.assertEqual(meta.decision_grade, "reference")
        self.assertIn("不应作为单独行政决策", meta.limitations)

    def test_earth_search_item_becomes_traceable_candidate(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "stac_version": "1.0.0",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "updated": "2026-06-01T08:00:00Z",
                "eo:cloud_cover": 8.5,
                "platform": "sentinel-2a",
                "constellation": "sentinel-2",
                "instruments": ["msi"],
                "s2:product_uri": "S2A_PRODUCT.SAFE",
                "s2:processing_baseline": "05.12",
            },
            "assets": {
                "visual": {
                    "href": "https://example.com/visual.tif",
                    "type": "image/tiff",
                    "gsd": 10,
                    "roles": ["visual"],
                },
                "thumbnail": {"href": "https://example.com/thumb.jpg", "type": "image/jpeg"},
            },
            "links": [{"rel": "license", "href": "https://example.com/license"}],
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        data = candidate.as_dict()
        self.assertEqual(data["source"], "earth_search")
        self.assertEqual(data["product_id"], "S2A_PRODUCT.SAFE")
        self.assertEqual(data["cloud_percent"], 8.5)
        self.assertEqual(data["gsd_m"], 10.0)
        self.assertEqual(data["decision_grade"], "screening")
        self.assertIn("visual", data["assets"])
        self.assertIn("license", data["links"])

    def test_candidate_score_penalizes_missing_metadata(self):
        score, reasons = score_candidate(None, None, 30, False)
        self.assertLess(score, 45)
        self.assertIn("缺少明确拍摄时间", reasons)
        self.assertIn("缺少云量指标", reasons)

    def test_select_best_sentinel_candidate_uses_suitability_score(self):
        cloudy_item = {
            "id": "S2A_CLOUDY",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-02T03:17:00Z",
                "eo:cloud_cover": 55,
                "s2:product_uri": "S2A_CLOUDY.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/cloudy.tif", "gsd": 10}},
        }
        clear_item = {
            "id": "S2A_CLEAR",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-05-30T03:17:00Z",
                "eo:cloud_cover": 3,
                "s2:product_uri": "S2A_CLEAR.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/clear.tif", "gsd": 10}},
        }
        provider = EarthSearchProvider()
        cloudy = provider.candidate_from_item(cloudy_item)
        clear = provider.candidate_from_item(clear_item)

        selected = select_best_sentinel_candidate([cloudy, clear])

        self.assertEqual(selected.product_id, "S2A_CLEAR.SAFE")
        self.assertGreater(selected.suitability_score, cloudy.suitability_score)

    def test_titiler_json_response_is_not_saved_as_image(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {"datetime": "2026-06-01T03:17:00Z"},
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        response = type("Response", (), {
            "headers": {"content-type": "application/json"},
            "content": b'{"detail":"error"}',
            "raise_for_status": lambda self: None,
        })()
        with patch("map_api.imagery_sources.earth_search.requests.get", return_value=response):
            with self.assertRaises(ValueError):
                EarthSearchProvider().render_candidate_jpeg(
                    candidate,
                    {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                    256,
                    256,
                )

    def test_sentinel_scene_quality_summarizes_fit_and_limits(self):
        class Scene:
            source = "sentinel2"
            source_label = "Sentinel-2 L2A"
            acquired_at = timezone.now() - timedelta(days=3)
            fetched_at = timezone.now()
            gsd_m = 10
            cloud_percent = 8.5
            decision_grade = "screening"

        quality = imagery_quality_payload(Scene())
        self.assertEqual(quality["source"], "sentinel2")
        self.assertIn("近7天内", quality["timeliness"])
        self.assertIn("低云量", quality["cloud_quality"])
        self.assertIn("宏观地类", quality["best_for"])
        self.assertIn("不适合车辆", "；".join(quality["cautions"]))

    def test_sentinel_confidence_depends_on_task_granularity(self):
        class Scene:
            source = "sentinel2"
            source_label = "Sentinel-2 L2A"
            acquired_at = timezone.now() - timedelta(days=3)
            fetched_at = timezone.now()
            gsd_m = 10
            cloud_percent = 8.5
            decision_grade = "screening"

        quality = imagery_quality_payload(Scene())
        macro_strategy = build_analysis_strategy("综合分析这片区域的土地利用", scene=Scene())
        macro_confidence = analysis_confidence_payload(macro_strategy, quality)
        self.assertEqual(macro_confidence["level"], "decision_support")
        self.assertEqual(macro_confidence["label"], "决策辅助级")

        detail_strategy = build_analysis_strategy("数一下停车场有多少辆车", scene=Scene())
        detail_confidence = analysis_confidence_payload(detail_strategy, quality)
        self.assertEqual(detail_confidence["level"], "low")
        self.assertIn("更高分辨率影像", "；".join(detail_confidence["required_checks"]))

    def test_source_recommendation_guides_source_choice(self):
        class MapboxScene:
            source = "mapbox"
            source_label = "Mapbox"
            acquired_at = None
            fetched_at = timezone.now()
            gsd_m = 1.2
            cloud_percent = None
            decision_grade = "reference"

        detail_strategy = build_analysis_strategy("数一下停车场有多少辆车", scene=MapboxScene())
        detail_rec = source_recommendation_payload(
            detail_strategy,
            imagery_quality_payload(MapboxScene()),
            "数一下停车场有多少辆车",
        )
        self.assertEqual(detail_rec["recommended_source"], "mapbox")
        self.assertEqual(detail_rec["alignment"], "matched")

        water_strategy = build_analysis_strategy("分析这片区域的水体和岸线", scene=MapboxScene())
        water_rec = source_recommendation_payload(
            water_strategy,
            imagery_quality_payload(MapboxScene()),
            "分析这片区域的水体和岸线",
        )
        self.assertEqual(water_rec["recommended_source"], "sentinel2")
        self.assertEqual(water_rec["alignment"], "switch_recommended")
        self.assertIn("近期公开影像", water_rec["action"])

        recent_strategy = build_analysis_strategy("分析最近是否有新增建设用地", scene=MapboxScene())
        recent_rec = source_recommendation_payload(
            recent_strategy,
            imagery_quality_payload(MapboxScene()),
            "分析最近是否有新增建设用地",
        )
        self.assertEqual(recent_rec["recommended_source"], "sentinel2")
        self.assertEqual(recent_rec["alignment"], "switch_recommended")
        self.assertIn("近期公开影像", recent_rec["action"])


class AnalysisStrategyTests(SimpleTestCase):
    def test_sentinel_detail_question_disables_active_perception(self):
        class Scene:
            source = "sentinel2"
            gsd_m = 10

        strategy = build_analysis_strategy("数一下停车场有多少辆车", scene=Scene(), requested_active=True)
        self.assertFalse(strategy["active_perception"])
        self.assertEqual(strategy["source"], "sentinel2")
        self.assertIn("不适合识别小建筑", strategy["prompt"])
        self.assertIn("不可可靠判断", strategy["prompt"])

    def test_mapbox_detail_question_keeps_active_perception(self):
        class Scene:
            source = "mapbox"
            gsd_m = 1.2

        strategy = build_analysis_strategy("分析建筑屋顶和道路细节", scene=Scene(), requested_active=True)
        self.assertTrue(strategy["active_perception"])
        self.assertEqual(strategy["source"], "mapbox")
        self.assertIn("建议启用主动感知", strategy["prompt"])
        self.assertEqual(strategy["task_profile"]["task"], "built_up")
        self.assertIn("建设用地与城市形态解译", strategy["prompt"])

    def test_water_question_gets_water_rubric(self):
        strategy = build_analysis_strategy("分析这片区域的水体和岸线是否异常")
        self.assertEqual(strategy["task_profile"]["task"], "water")
        self.assertIn("水体与岸线解译", strategy["prompt"])
        self.assertIn("岸线形态", strategy["prompt"])

    def test_generic_question_gets_land_use_rubric(self):
        strategy = build_analysis_strategy("全面分析这片区域")
        self.assertEqual(strategy["task_profile"]["task"], "land_use")
        self.assertIn("综合土地利用解译", strategy["prompt"])

    def test_agriculture_question_gets_cropland_rubric(self):
        strategy = build_analysis_strategy("分析这片农田的作物长势和田块破碎化")
        self.assertEqual(strategy["task_profile"]["task"], "agriculture")
        self.assertIn("农业耕地与作物长势解译", strategy["prompt"])
        self.assertIn("田块破碎化", strategy["prompt"])

    def test_vehicle_count_question_gets_small_target_rubric(self):
        strategy = build_analysis_strategy("数一下停车场有多少辆车")
        self.assertEqual(strategy["task_profile"]["task"], "small_target")
        self.assertIn("小目标与交通设施精细判读", strategy["prompt"])
        self.assertIn("估计数量与分布", strategy["prompt"])


class AIQueryApiTests(TestCase):
    def _make_test_image(self, file_name):
        from PIL import Image

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, file_name)
        Image.new("RGB", (32, 32), (80, 120, 160)).save(path, "JPEG")
        def cleanup():
            base, _ = os.path.splitext(path)
            for name in os.listdir(save_dir):
                candidate = os.path.join(save_dir, name)
                if candidate == path or candidate.startswith(base + "_"):
                    if os.path.exists(candidate):
                        os.remove(candidate)
        self.addCleanup(cleanup)
        return path

    def _fake_qwen_response(self, text="这是 mock 遥感分析结论"):
        message = type("Message", (), {"content": [{"text": text}]})()
        choice = type("Choice", (), {"message": message})()
        output = type("Output", (), {"choices": [choice]})()
        return type("Response", (), {"status_code": 200, "output": output, "message": ""})()

    def test_ai_query_returns_analysis_method_metadata(self):
        file_name = "sat_ai_method.jpg"
        image_path = self._make_test_image(file_name)
        scene = ImageryScene.objects.create(
            file_name=file_name,
            source="mapbox",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=1.2,
        )
        preprocess = {
            "single": image_path,
            "orig_w": 32,
            "orig_h": 32,
            "eff_w": 32,
            "eff_h": 32,
        }
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.smart_prepare_image_v2", return_value=preprocess), \
                patch("map_api.views._call_qwen", return_value=self._fake_qwen_response()):
            r = self.client.post(
                "/api/ai/query-region/",
                data={
                    "file_name": file_name,
                    "scene_id": scene.id,
                    "question": "分析这片区域的水体和岸线",
                    "mode": "fast",
                    "active_perception": False,
                    "gsd": 1.2,
                    "bbox": {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                    "history": [],
                },
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["answer"], "这是 mock 遥感分析结论")
        self.assertEqual(data["analysis_method"]["mode"], "fast")
        self.assertEqual(data["analysis_method"]["model"], "qwen3-vl-flash")
        self.assertEqual(data["analysis_method"]["task_label"], "水体与岸线解译")
        self.assertFalse(data["analysis_method"]["active_perception"])
        self.assertIn("高清底图", data["analysis_method"]["imagery_quality"]["summary"])
        self.assertIn("时效性证据", "；".join(data["analysis_method"]["imagery_quality"]["cautions"]))
        self.assertEqual(data["analysis_method"]["confidence"]["level"], "reference")
        self.assertIn("视觉参考级", data["analysis_method"]["confidence"]["label"])
        self.assertEqual(data["analysis_method"]["source_recommendation"]["recommended_source"], "sentinel2")
        self.assertEqual(data["analysis_method"]["source_recommendation"]["alignment"], "switch_recommended")
        self.assertFalse(data["analysis_method"]["output_quality"]["structured_answer"])
        self.assertTrue(data["analysis_method"]["output_quality"]["fallback_used"])
        self.assertIn("模型未按 <answer> 结构化格式输出", "；".join(data["analysis_method"]["output_quality"]["warnings"]))
        self.assertEqual(data["scene"]["id"], scene.id)
        self.assertIn("quality", data["scene"])

    def test_ai_query_records_structured_output_and_self_check(self):
        file_name = "sat_ai_self_check.jpg"
        image_path = self._make_test_image(file_name)
        scene = ImageryScene.objects.create(
            file_name=file_name,
            source="mapbox",
            min_lng=10,
            min_lat=20,
            max_lng=14,
            max_lat=24,
            gsd_m=2.0,
        )
        preprocess = {
            "single": image_path,
            "orig_w": 32,
            "orig_h": 32,
            "eff_w": 32,
            "eff_h": 32,
        }
        stage1 = '<think>[{"bbox_2d": [8, 8, 16, 16], "label": "建筑"}]</think>'
        stage2 = "<answer>局部细节分析结论</answer>"
        checked = "<answer>自检后一致的最终结论</answer>"
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.smart_prepare_image_v2", return_value=preprocess), \
                patch("map_api.views._call_qwen", side_effect=[
                    self._fake_qwen_response(stage1),
                    self._fake_qwen_response(stage2),
                    self._fake_qwen_response(checked),
                ]):
            r = self.client.post(
                "/api/ai/query-region/",
                data={
                    "file_name": file_name,
                    "scene_id": scene.id,
                    "question": "分析这片区域",
                    "mode": "precise",
                    "self_check": True,
                    "history": [],
                },
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertIn("自检后一致的最终结论", data["answer"])
        output_quality = data["analysis_method"]["output_quality"]
        self.assertTrue(output_quality["structured_answer"])
        self.assertFalse(output_quality["fallback_used"])
        self.assertTrue(output_quality["self_check_enabled"])
        self.assertTrue(output_quality["self_check_applied"])
        self.assertEqual(output_quality["stage_count"], 2)

    def test_ai_query_unknown_mode_reports_precise_fallback(self):
        file_name = "sat_ai_mode_fallback.jpg"
        image_path = self._make_test_image(file_name)
        scene = ImageryScene.objects.create(
            file_name=file_name,
            source="mapbox",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=1.2,
        )
        preprocess = {
            "single": image_path,
            "orig_w": 32,
            "orig_h": 32,
            "eff_w": 32,
            "eff_h": 32,
        }
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.smart_prepare_image_v2", return_value=preprocess), \
                patch("map_api.views._call_qwen", return_value=self._fake_qwen_response()):
            r = self.client.post(
                "/api/ai/query-region/",
                data={
                    "file_name": file_name,
                    "scene_id": scene.id,
                    "question": "整体分析这片区域",
                    "mode": "unknown",
                    "active_perception": False,
                    "gsd": 1.2,
                    "bbox": {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                },
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        method = r.json()["data"]["analysis_method"]
        self.assertEqual(method["mode"], "precise")
        self.assertEqual(method["model"], "qwen3-vl-plus")

    def test_ai_query_uses_scene_gsd_and_bbox_when_frontend_context_is_missing(self):
        file_name = "sat_ai_scene_context_fallback.jpg"
        image_path = self._make_test_image(file_name)
        scene = ImageryScene.objects.create(
            file_name=file_name,
            source="sentinel2",
            min_lng=10,
            min_lat=20,
            max_lng=14,
            max_lat=24,
            gsd_m=2.0,
        )
        preprocess = {
            "single": image_path,
            "orig_w": 32,
            "orig_h": 32,
            "eff_w": 32,
            "eff_h": 32,
        }
        stage1 = '<think>[{"bbox_2d": [8, 8, 16, 16], "label": "建筑"}]</think>'
        stage2 = "<answer>局部细节分析结论</answer>"
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.smart_prepare_image_v2", return_value=preprocess), \
                patch("map_api.views._call_qwen", side_effect=[
                    self._fake_qwen_response(stage1),
                    self._fake_qwen_response(stage2),
                ]):
            r = self.client.post(
                "/api/ai/query-region/",
                data={
                    "file_name": file_name,
                    "scene_id": scene.id,
                    "question": "分析这片区域的建筑细节",
                    "mode": "precise",
                    "history": [],
                },
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["active_stages"], 1)
        self.assertEqual(data["answer"], stage1)
        self.assertEqual(data["targets"], [])

    def test_mapbox_scene_does_not_claim_physical_measurement(self):
        file_name = "sat_mapbox_no_measure.jpg"
        image_path = self._make_test_image(file_name)
        scene = ImageryScene.objects.create(
            file_name=file_name, source="mapbox", min_lng=10, min_lat=20,
            max_lng=14, max_lat=24, gsd_m=1.2,
        )
        preprocess = {"single": image_path, "orig_w": 32, "orig_h": 32, "eff_w": 32, "eff_h": 32}
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.smart_prepare_image_v2", return_value=preprocess), \
                patch("map_api.views._call_qwen", return_value=self._fake_qwen_response('<answer>整体结论</answer>')):
            response = self.client.post(
                "/api/ai/query-region/",
                data={"file_name": file_name, "scene_id": scene.id, "question": "分析整体区域", "active_perception": False},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["targets"], [])


class HistoryApiTests(TestCase):
    def _touch_history_image(self, file_name):
        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, file_name)
        with open(path, "wb") as f:
            f.write(b"jpg")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_rejects_compare_history(self):
        r = self.client.post(
            "/api/ai/history/",
            data={"image_file": "__compare__", "messages": []},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], 400)

    def test_upserts_history_by_image_file(self):
        self._touch_history_image("sat_test.jpg")
        scene = ImageryScene.objects.create(
            file_name="sat_test.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        payload = {"image_file": "sat_test.jpg", "scene_id": scene.id, "messages": [{"role": "user", "content": "a"}]}
        r1 = self.client.post("/api/ai/history/", data=payload, content_type="application/json")
        r2 = self.client.post("/api/ai/history/", data=payload, content_type="application/json")
        self.assertEqual(r1.json()["code"], 200)
        self.assertEqual(r2.json()["code"], 200)
        obj = ChatHistory.objects.get(image_file="sat_test.jpg")
        self.assertEqual(obj.scene_id, scene.id)

    def test_rejects_history_when_image_file_is_missing(self):
        r = self.client.post(
            "/api/ai/history/",
            data={"image_file": "sat_missing_history.jpg", "messages": [{"role": "user", "content": "a"}]},
            content_type="application/json",
        )

        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["code"], 410)
        self.assertFalse(ChatHistory.objects.filter(image_file="sat_missing_history.jpg").exists())

    def test_history_preserves_analysis_method_metadata(self):
        self._touch_history_image("sat_method.jpg")
        scene = ImageryScene.objects.create(
            file_name="sat_method.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        payload = {
            "image_file": "sat_method.jpg",
            "scene_id": scene.id,
            "messages": [{
                "role": "ai",
                "content": "分析结论",
                "analysis_method": {
                    "mode": "precise",
                    "model": "qwen3-vl-plus",
                    "task_label": "水体与岸线解译",
                    "source_recommendation": {
                        "recommended_source": "sentinel2",
                        "recommended_label": "建议使用近期公开影像",
                        "alignment": "switch_recommended",
                    },
                },
            }],
        }
        r = self.client.post("/api/ai/history/", data=payload, content_type="application/json")
        self.assertEqual(r.json()["code"], 200)
        detail = self.client.get(f"/api/ai/history/{r.json()['data']['id']}/")
        method = detail.json()["data"]["messages"][0]["analysis_method"]
        self.assertEqual(method["model"], "qwen3-vl-plus")
        self.assertEqual(method["task_label"], "水体与岸线解译")
        self.assertEqual(method["source_recommendation"]["recommended_source"], "sentinel2")
        self.assertEqual(method["source_recommendation"]["alignment"], "switch_recommended")

    def test_history_list_hides_missing_image_records(self):
        self._touch_history_image("sat_history_ok.jpg")
        ChatHistory.objects.create(image_file="sat_history_ok.jpg", messages=[])
        ChatHistory.objects.create(image_file="sat_history_missing.jpg", messages=[])

        r = self.client.get("/api/ai/history/")

        self.assertEqual(r.status_code, 200)
        files = [item["image_file"] for item in r.json()["data"]]
        self.assertIn("sat_history_ok.jpg", files)
        self.assertNotIn("sat_history_missing.jpg", files)
        self.assertTrue(r.json()["data"][0]["image_available"])

    def test_history_list_includes_scene_brief_for_source_traceability(self):
        self._touch_history_image("sentinel_history.jpg")
        scene = ImageryScene.objects.create(
            file_name="sentinel_history.jpg",
            source="sentinel2",
            source_label="Sentinel-2 L2A",
            product_id="S2A_HISTORY.SAFE",
            acquired_at=timezone.now() - timedelta(days=5),
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=10,
            cloud_percent=7,
            decision_grade="screening",
            metadata={
                "selection_method": "suitability_score",
                "candidate_count": 5,
                "selection_rank": 1,
                "suitability_score": 93,
                "score_reasons": ["拍摄时间在 7 天内，时效性很好", "云量低于 10%，可视条件较好"],
            },
        )
        ChatHistory.objects.create(image_file="sentinel_history.jpg", scene=scene, messages=[])

        r = self.client.get("/api/ai/history/")

        self.assertEqual(r.status_code, 200)
        item = r.json()["data"][0]
        self.assertEqual(item["scene"]["source"], "sentinel2")
        self.assertEqual(item["scene"]["source_label"], "Sentinel-2 L2A")
        self.assertEqual(item["scene"]["cloud_percent"], 7)
        self.assertEqual(item["scene"]["selection"]["candidate_count"], 5)
        self.assertEqual(item["scene"]["selection"]["suitability_score"], 93)
        self.assertIn("候选池 5 景", item["scene"]["selection"]["summary"])

    def test_history_detail_returns_410_for_missing_image(self):
        obj = ChatHistory.objects.create(image_file="sat_history_missing_detail.jpg", messages=[])

        r = self.client.get(f"/api/ai/history/{obj.id}/")

        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["code"], 410)
        self.assertIn("历史影像文件已丢失", r.json()["msg"])

    def test_delete_history_without_csrf_token(self):
        obj = ChatHistory.objects.create(image_file="sat_delete.jpg", messages=[])
        r = self.client.delete(f"/api/ai/history/{obj.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ChatHistory.objects.filter(id=obj.id).exists())


class DownloadTaskTests(TestCase):
    def test_queue_mode_persists_download_and_worker_atomically_publishes_image(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"MAPBOX_TOKEN": "test-token", "AGENT_EXECUTION_MODE": "queue"}, clear=False), \
             patch("map_api.views.SAVE_DIR", tmp), \
             patch("map_api.download_jobs.SAVE_DIR", tmp), \
             patch("map_api.views.threading.Thread") as thread:
            response = self.client.post(
                "/api/satellite/get-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 1.01, "max_lat": 2.01, "target_resolution": 64},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            thread.assert_not_called()
            task = DownloadTask.objects.get(file_name=response.json()["data"]["file_name"])
            self.assertEqual(task.status, "downloading")
            self.assertEqual(task.worker_claim, "")

            def fake_fetch(*args, save_dir, file_name, progress_callback, **kwargs):
                path = os.path.join(save_dir, file_name)
                with open(path, "wb") as handle:
                    handle.write(b"complete-image")
                progress_callback(file_name, {"total": 1, "done": 1, "failed": 0, "status": "done"})
                return path

            with patch("map_api.download_jobs.fetch_satellite_image", side_effect=fake_fetch):
                call_command("run_agent_worker", "--once", "--max-sessions", "1")
            task.refresh_from_db()
            self.assertEqual(task.status, "done")
            self.assertEqual(task.attempts, 1)
            self.assertEqual(open(os.path.join(tmp, task.file_name), "rb").read(), b"complete-image")
            self.assertFalse(any(name.endswith(".part.jpg") for name in os.listdir(tmp)))

    def test_stale_download_worker_cannot_replace_final_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_name = "sat_stale_download_owner.jpg"
            final_path = os.path.join(tmp, final_name)
            with open(final_path, "wb") as handle:
                handle.write(b"new-worker-image")
            task = DownloadTask.objects.create(
                file_name=final_name, status="downloading", total=1,
                min_lng=1, min_lat=2, max_lng=1.01, max_lat=2.01,
                resolution_px=64, worker_claim="old-worker", claimed_at=timezone.now(),
            )

            def fake_fetch(*args, save_dir, file_name, **kwargs):
                path = os.path.join(save_dir, file_name)
                with open(path, "wb") as handle:
                    handle.write(b"old-worker-image")
                DownloadTask.objects.filter(id=task.id).update(worker_claim="new-worker")
                return path

            with patch("map_api.download_jobs.SAVE_DIR", tmp), \
                 patch("map_api.download_jobs.fetch_satellite_image", side_effect=fake_fetch):
                completed = execute_download_task(task.id, "old-worker")
            self.assertFalse(completed)
            self.assertEqual(open(final_path, "rb").read(), b"new-worker-image")
            self.assertFalse(any(name.endswith(".part.jpg") for name in os.listdir(tmp)))

    def test_queue_download_preserves_partial_terminal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = DownloadTask.objects.create(
                file_name="sat_queue_partial.jpg", status="downloading", total=2,
                min_lng=1, min_lat=2, max_lng=1.01, max_lat=2.01,
                resolution_px=64, worker_claim="worker-partial", claimed_at=timezone.now(),
            )

            def fake_fetch(*args, save_dir, file_name, progress_callback, **kwargs):
                path = os.path.join(save_dir, file_name)
                with open(path, "wb") as handle:
                    handle.write(b"partial-image")
                progress_callback(file_name, {"total": 2, "done": 2, "failed": 1, "status": "partial"})
                return path

            with patch("map_api.download_jobs.SAVE_DIR", tmp), \
                 patch("map_api.download_jobs.fetch_satellite_image", side_effect=fake_fetch):
                completed = execute_download_task(task.id, "worker-partial")
            self.assertTrue(completed)
            task.refresh_from_db()
            self.assertEqual(task.status, "partial")
            self.assertEqual(task.failed, 1)

    def test_progress_falls_back_to_database(self):
        _download_progress.clear()
        scene = ImageryScene.objects.create(
            file_name="sat_db.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        DownloadTask.objects.create(
            scene=scene,
            file_name="sat_db.jpg",
            status="done",
            total=2,
            done=2,
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        r = self.client.get("/api/satellite/progress/?file=sat_db.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["status"], "done")
        self.assertEqual(r.json()["data"]["scene_id"], scene.id)

    def test_cleanup_removes_old_finished_progress_entries(self):
        _download_progress.clear()
        _download_progress["sat_old.jpg"] = {"total": 1, "done": 1, "status": "done"}
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir):
                r = self.client.post("/api/satellite/cleanup/", data={"days": 0}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("sat_old.jpg", _download_progress)

    def test_cleanup_removes_history_for_deleted_image_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            image_path = os.path.join(save_dir, "sat_cleanup_history.jpg")
            with open(image_path, "wb") as f:
                f.write(b"jpg")
            os.utime(image_path, (0, 0))

            scene = ImageryScene.objects.create(
                file_name="sat_cleanup_history.jpg",
                min_lng=1,
                min_lat=2,
                max_lng=3,
                max_lat=4,
            )
            DownloadTask.objects.create(
                scene=scene,
                file_name="sat_cleanup_history.jpg",
                status="done",
                total=1,
                done=1,
                min_lng=1,
                min_lat=2,
                max_lng=3,
                max_lat=4,
            )
            ChatHistory.objects.create(
                scene=scene,
                image_file="sat_cleanup_history.jpg",
                messages=[{"role": "ai", "content": "old"}],
            )

            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir):
                r = self.client.post("/api/satellite/cleanup/", data={"days": 0}, content_type="application/json")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["deleted_image_records"], 1)
        self.assertFalse(ChatHistory.objects.filter(image_file="sat_cleanup_history.jpg").exists())
        self.assertFalse(DownloadTask.objects.filter(file_name="sat_cleanup_history.jpg").exists())
        self.assertFalse(ImageryScene.objects.filter(file_name="sat_cleanup_history.jpg").exists())

    def test_cleanup_all_removes_histories(self):
        scene = ImageryScene.objects.create(
            file_name="sat_cleanup_all.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        ChatHistory.objects.create(scene=scene, image_file="sat_cleanup_all.jpg", messages=[])
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir):
                r = self.client.post(
                    "/api/satellite/cleanup/",
                    data={"all": True},
                    content_type="application/json",
                )

        self.assertEqual(r.status_code, 200)
        self.assertFalse(ChatHistory.objects.exists())
        self.assertFalse(ImageryScene.objects.exists())

    def test_cleanup_all_removes_agent_sessions(self):
        AgentSession.objects.create(goal="清空媒体时同步清空 Agent", status=AgentSession.STATUS_COMPLETED)
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir):
                r = self.client.post(
                    "/api/satellite/cleanup/",
                    data={"all": True},
                    content_type="application/json",
                )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AgentSession.objects.exists())

    def test_cleanup_marks_agent_image_and_report_references_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            image_name = "agent_cleanup_refs.jpg"
            report_name = "report_cleanup_refs.docx"
            image_path = os.path.join(save_dir, image_name)
            report_path = os.path.join(report_dir, report_name)
            open(image_path, "wb").write(b"image")
            open(report_path, "wb").write(b"report")
            os.utime(image_path, (0, 0))
            os.utime(report_path, (0, 0))
            session = AgentSession.objects.create(
                goal="清理引用测试",
                status=AgentSession.STATUS_COMPLETED,
                artifacts={"file_name": image_name, "image_url": "/image", "report": {"file_name": report_name}},
            )
            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir):
                r = self.client.post("/api/satellite/cleanup/", data={"days": 0}, content_type="application/json")
            self.assertEqual(r.status_code, 200)
            session.refresh_from_db()
            self.assertTrue(session.artifacts.get("file_missing"))
            self.assertTrue(session.artifacts.get("report_missing"))
            self.assertNotIn("file_name", session.artifacts)
            self.assertNotIn("report", session.artifacts)

    def test_cleanup_is_idempotent_when_another_worker_deletes_file_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_dir = os.path.join(tmp, "satellite_imgs")
            report_dir = os.path.join(tmp, "reports")
            os.makedirs(save_dir, exist_ok=True)
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(save_dir, "race_cleanup.jpg")
            with open(path, "wb") as handle:
                handle.write(b"race")
            os.utime(path, (0, 0))
            with patch("map_api.views.SAVE_DIR", save_dir), patch("map_api.views.REPORT_DIR", report_dir), \
                    patch("map_api.views.os.remove", side_effect=FileNotFoundError):
                response = self.client.post("/api/satellite/cleanup/", data={"days": 0}, content_type="application/json")
            self.assertEqual(response.status_code, 200)


class SmokePipelineCommandTests(TestCase):
    def test_smoke_pipeline_command_exercises_core_loop_and_cleans_artifacts(self):
        out = StringIO()

        call_command("smoke_pipeline", stdout=out)

        data = json.loads(out.getvalue())
        self.assertEqual(data["status"], "passed")
        step_ids = [step["id"] for step in data["steps"]]
        self.assertEqual(step_ids, ["health", "source_recommendation", "ai_analysis", "history", "report"])
        self.assertTrue(all(step["ok"] for step in data["steps"]))
        image_file = data["artifacts"]["image_file"]
        report_file = data["artifacts"]["report_file"]
        self.assertFalse(ChatHistory.objects.filter(image_file=image_file).exists())
        self.assertFalse(DownloadTask.objects.filter(file_name=image_file).exists())
        self.assertFalse(ImageryScene.objects.filter(file_name=image_file).exists())
        self.assertFalse(os.path.exists(os.path.join(settings.MEDIA_ROOT, "satellite_imgs", image_file)))
        self.assertFalse(os.path.exists(os.path.join(settings.MEDIA_ROOT, report_file)))

    def test_smoke_pipeline_help_lists_live_dependency_checks(self):
        from map_api.management.commands.smoke_pipeline import Command

        help_text = Command().create_parser("", "smoke_pipeline").format_help()
        self.assertIn("--live-mapbox", help_text)
        self.assertIn("--live-sentinel", help_text)
        self.assertIn("--live-ai", help_text)
        self.assertIn("--keep-artifacts", help_text)


class ImagerySceneApiTests(TestCase):
    def _jpg_bytes(self, color=(80, 120, 160), size=(1024, 1024)):
        buf = BytesIO()
        Image.new("RGB", size, color).save(buf, "JPEG")
        return buf.getvalue()

    def test_scene_detail_returns_metadata(self):
        scene = ImageryScene.objects.create(
            file_name="sat_scene.jpg",
            source_label="Mapbox Satellite Basemap",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            limitations="仅作参考",
        )
        r = self.client.get(f"/api/imagery/scenes/{scene.id}/")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["decision_grade"], "reference")
        self.assertEqual(data["limitations"], "仅作参考")

    def test_scene_list_returns_scenes(self):
        ImageryScene.objects.create(file_name="sat_list.jpg", min_lng=1, min_lat=2, max_lng=3, max_lat=4)
        r = self.client.get("/api/imagery/scenes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["data"]), 1)

    def test_imagery_search_returns_candidates(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "eo:cloud_cover": 8.5,
                "s2:product_uri": "S2A_PRODUCT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        with patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]) as mocked:
            r = self.client.get(
                "/api/imagery/search/?min_lng=1&min_lat=2&max_lng=3&max_lat=4&limit=5&max_cloud=20"
            )
        self.assertEqual(r.status_code, 200)
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["limit"], 5)
        self.assertEqual(kwargs["max_cloud"], 20.0)
        data = r.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["candidates"][0]["item_id"], "S2A_TEST")

    def test_imagery_search_rejects_unknown_provider(self):
        r = self.client.get(
            "/api/imagery/search/?provider=x&min_lng=1&min_lat=2&max_lng=3&max_lat=4"
        )
        self.assertEqual(r.status_code, 400)

    def test_imagery_recommend_source_for_detail_and_macro_questions(self):
        detail = self.client.get(
            "/api/imagery/recommend-source/",
            {"question": "数一下停车场有多少辆车", "current_source": "sentinel2"},
        )
        self.assertEqual(detail.status_code, 200)
        detail_rec = detail.json()["data"]["recommendation"]
        self.assertEqual(detail_rec["recommended_source"], "mapbox")
        self.assertEqual(detail_rec["alignment"], "switch_recommended")

        macro = self.client.post(
            "/api/imagery/recommend-source/",
            data={"question": "分析这片区域的水体和岸线", "current_source": "mapbox"},
            content_type="application/json",
        )
        self.assertEqual(macro.status_code, 200)
        macro_rec = macro.json()["data"]["recommendation"]
        self.assertEqual(macro_rec["recommended_source"], "sentinel2")
        self.assertEqual(macro_rec["alignment"], "switch_recommended")
        self.assertIn("水体", macro_rec["reason"])

    def test_imagery_recommend_source_validates_question_and_method(self):
        empty = self.client.get("/api/imagery/recommend-source/")
        self.assertEqual(empty.status_code, 400)

        unsupported = self.client.delete("/api/imagery/recommend-source/")
        self.assertEqual(unsupported.status_code, 405)

    def test_imagery_recommend_source_post_does_not_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        r = client.post(
            "/api/imagery/recommend-source/",
            data={"question": "分析这片区域的水体和岸线", "current_source": "mapbox"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["recommendation"]["recommended_source"], "sentinel2")

    def test_sentinel_image_endpoint_creates_scene_and_file(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "updated": "2026-06-01T08:00:00Z",
                "eo:cloud_cover": 8.5,
                "s2:product_uri": "S2A_PRODUCT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        with patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=self._jpg_bytes()):
            r = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["file_name"].startswith("sentinel_"))
        self.assertEqual(data["scene"]["source"], "sentinel2")
        self.assertEqual(data["scene"]["product_id"], "S2A_PRODUCT.SAFE")
        self.assertEqual(data["scene"]["decision_grade"], "screening")
        expected_gsd = round(
            image_plan_for_bbox_and_size(
                {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                data["render_width"],
                data["render_height"],
            )["gsd_m"],
            2,
        )
        self.assertEqual(data["gsd_m"], expected_gsd)
        self.assertEqual(data["scene"]["gsd_m"], expected_gsd)
        self.assertEqual(data["scene"]["metadata"]["source_asset_gsd_m"], 10.0)
        self.assertFalse(data["cache_hit"])
        self.assertIn("sentinel_cache_key", data["scene"]["metadata"])
        self.assertEqual(data["scene"]["metadata"]["render_size_px"]["width"], data["render_width"])
        self.assertTrue(ImageryScene.objects.filter(file_name=data["file_name"]).exists())
        self.assertTrue(DownloadTask.objects.filter(file_name=data["file_name"], status="done").exists())
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", data["file_name"])
        self.assertTrue(os.path.exists(img_path))
        os.remove(img_path)

    def test_sentinel_image_endpoint_selects_best_candidate_from_recent_pool(self):
        provider = EarthSearchProvider()
        cloudy = provider.candidate_from_item({
            "id": "S2A_CLOUDY",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-02T03:17:00Z",
                "eo:cloud_cover": 55,
                "s2:product_uri": "S2A_CLOUDY.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/cloudy.tif", "gsd": 10}},
        })
        clear = provider.candidate_from_item({
            "id": "S2A_CLEAR",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-05-30T03:17:00Z",
                "eo:cloud_cover": 3,
                "s2:product_uri": "S2A_CLEAR.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/clear.tif", "gsd": 10}},
        })
        with patch("map_api.views.EarthSearchProvider.search", return_value=[cloudy, clear]) as search_mock, \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=self._jpg_bytes()) as render_mock:
            r = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(search_mock.call_args.kwargs["limit"], 10)
        render_mock.assert_called_once()
        self.assertEqual(render_mock.call_args.args[0].product_id, "S2A_CLEAR.SAFE")
        self.assertEqual(data["candidate_count"], 2)
        self.assertEqual(data["candidate"]["product_id"], "S2A_CLEAR.SAFE")
        self.assertEqual(data["scene"]["metadata"]["candidate_count"], 2)
        self.assertEqual(data["scene"]["metadata"]["selection_method"], "single_scene")
        self.assertIn("云量低于 10%", "；".join(data["selection_reasons"]))
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", data["file_name"])
        if os.path.exists(img_path):
            os.remove(img_path)

    def test_sentinel_image_endpoint_falls_back_when_best_candidate_fails_to_render(self):
        provider = EarthSearchProvider()
        best = provider.candidate_from_item({
            "id": "S2A_BEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-02T03:17:00Z",
                "eo:cloud_cover": 2,
                "s2:product_uri": "S2A_BEST.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/best.tif", "gsd": 10}},
        })
        fallback = provider.candidate_from_item({
            "id": "S2A_FALLBACK",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-05-30T03:17:00Z",
                "eo:cloud_cover": 8,
                "s2:product_uri": "S2A_FALLBACK.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/fallback.tif", "gsd": 10}},
        })
        with patch("map_api.views.EarthSearchProvider.search", return_value=[fallback, best]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", side_effect=[ValueError("bad cog"), self._jpg_bytes()]) as render_mock:
            r = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(render_mock.call_count, 2)
        self.assertEqual(data["candidate"]["product_id"], "S2A_FALLBACK.SAFE")
        self.assertEqual(data["scene"]["product_id"], "S2A_FALLBACK.SAFE")
        self.assertEqual(data["scene"]["metadata"]["selection_rank"], 2)
        self.assertIn("S2A_BEST.SAFE: bad cog", data["scene"]["metadata"]["render_fallback_errors"])
        self.assertFalse(ImageryScene.objects.filter(product_id="S2A_BEST.SAFE").exists())
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", data["file_name"])
        if os.path.exists(img_path):
            os.remove(img_path)

    def test_sentinel_image_endpoint_validates_request_parameters(self):
        base = {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}

        high_cloud = self.client.post(
            "/api/satellite/get-sentinel-img/",
            data={**base, "max_cloud": 150},
            content_type="application/json",
        )
        low_limit = self.client.post(
            "/api/satellite/get-sentinel-img/",
            data={**base, "candidate_limit": 0},
            content_type="application/json",
        )
        bad_resolution = self.client.post(
            "/api/satellite/get-sentinel-img/",
            data={**base, "target_resolution": "large"},
            content_type="application/json",
        )

        self.assertEqual(high_cloud.status_code, 400)
        self.assertIn("max_cloud 不能大于 100", high_cloud.json()["msg"])
        self.assertEqual(low_limit.status_code, 400)
        self.assertIn("candidate_limit 不能小于 1", low_limit.json()["msg"])
        self.assertEqual(bad_resolution.status_code, 400)
        self.assertIn("target_resolution 必须是整数", bad_resolution.json()["msg"])

    def test_sentinel_image_endpoint_reuses_cached_scene(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "updated": "2026-06-01T08:00:00Z",
                "eo:cloud_cover": 8.5,
                "s2:product_uri": "S2A_PRODUCT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        payload = {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}
        with patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=self._jpg_bytes()) as render_mock:
            first = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data=payload,
                content_type="application/json",
            )
            second = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data=payload,
                content_type="application/json",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["data"]["cache_hit"])
        self.assertTrue(second.json()["data"]["cache_hit"])
        self.assertEqual(first.json()["data"]["file_name"], second.json()["data"]["file_name"])
        render_mock.assert_called_once()
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", first.json()["data"]["file_name"])
        if os.path.exists(img_path):
            os.remove(img_path)

    def test_sentinel_image_endpoint_creates_mosaic_for_partial_tiles(self):
        provider = EarthSearchProvider()
        left = provider.candidate_from_item({
            "id": "S2A_LEFT",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 2, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "eo:cloud_cover": 5,
                "s2:product_uri": "S2A_LEFT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/left.tif", "gsd": 10}},
        })
        right = provider.candidate_from_item({
            "id": "S2A_RIGHT",
            "collection": "sentinel-2-l2a",
            "bbox": [2, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "eo:cloud_cover": 5,
                "s2:product_uri": "S2A_RIGHT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/right.tif", "gsd": 10}},
        })
        left_img = Image.new("RGB", (64, 64), (0, 0, 0))
        right_img = Image.new("RGB", (64, 64), (0, 0, 0))
        for x in range(32):
            for y in range(64):
                left_img.putpixel((x, y), (100, 120, 150))
        for x in range(32, 64):
            for y in range(64):
                right_img.putpixel((x, y), (80, 130, 100))
        left_buf = BytesIO()
        right_buf = BytesIO()
        left_img.save(left_buf, "JPEG")
        right_img.save(right_buf, "JPEG")

        with patch("map_api.views.EarthSearchProvider.search", return_value=[left, right]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", side_effect=[left_buf.getvalue(), right_buf.getvalue()]) as render_mock:
            r = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["mosaic"])
        self.assertEqual(data["total_tiles"], 2)
        self.assertEqual(data["scene"]["metadata"]["selection_method"], "coverage_mosaic")
        self.assertEqual(data["scene"]["metadata"]["mosaic_candidate_count"], 2)
        self.assertGreaterEqual(data["scene"]["metadata"]["valid_image_ratio"], 0.95)
        self.assertEqual(render_mock.call_count, 2)
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", data["file_name"])
        if os.path.exists(img_path):
            os.remove(img_path)

    def test_sentinel_image_endpoint_returns_friendly_502_on_render_failure(self):
        item = {
            "id": "S2A_TEST",
            "collection": "sentinel-2-l2a",
            "bbox": [1, 2, 3, 4],
            "properties": {
                "datetime": "2026-06-01T03:17:00Z",
                "eo:cloud_cover": 8.5,
                "s2:product_uri": "S2A_PRODUCT.SAFE",
            },
            "assets": {"visual": {"href": "https://example.com/visual.tif", "gsd": 10}},
        }
        candidate = EarthSearchProvider().candidate_from_item(item)
        with patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", side_effect=ValueError("not image")):
            r = self.client.post(
                "/api/satellite/get-sentinel-img/",
                data={"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["msg"], "近期公开影像源暂时不可用，可切回高清底图继续分析")
        self.assertFalse(ImageryScene.objects.filter(source="sentinel2").exists())

    def test_sentinel_image_endpoint_keeps_bad_input_as_400(self):
        r = self.client.post(
            "/api/satellite/get-sentinel-img/",
            data={"min_lng": "bad", "min_lat": 2, "max_lng": 3, "max_lat": 4},
            content_type="application/json",
        )

        self.assertEqual(r.status_code, 400)


_AGENT_PLAN_STEPS = [
    {"id": "understand", "label": "理解调查目标"},
    {"id": "locate", "label": "定位调查范围"},
    {"id": "select_source", "label": "选择图像源"},
    {"id": "retrieve_imagery", "label": "检索并生成影像"},
    {"id": "quality_check", "label": "检查影像质量"},
    {"id": "ndwi", "label": "轻量 NDWI 水体量化"},
    {"id": "vl_analysis", "label": "视觉模型解译"},
    {"id": "complete", "label": "整理结果"},
]

_AGENT_STEP_FOR = {
    "geocode_place": "locate",
    "search_sentinel_imagery": "retrieve_imagery",
    "fetch_mapbox_imagery": "retrieve_imagery",
    "compute_ndwi": "ndwi",
    "analyze_imagery": "vl_analysis",
}


def _agent_call(tool_name, args=None, current_step=None, thought=None):
    return {
        "thought": thought or f"调用 {tool_name}",
        "current_step": current_step or _AGENT_STEP_FOR.get(tool_name, "understand"),
        "plan": _AGENT_PLAN_STEPS,
        "tool_call": {"name": tool_name, "args": args or {}},
        "final_answer": None,
    }


def _agent_final(text, current_step="complete", thought=None):
    return {
        "thought": thought or "整理复核结论",
        "current_step": current_step,
        "plan": _AGENT_PLAN_STEPS,
        "tool_call": None,
        "final_answer": text,
    }


class AgentSessionApiTests(TestCase):
    GOAL = "帮我调查南宁市在2026年四月的水体情况"

    def setUp(self):
        super().setUp()
        reset_rate_limit_state()

    def _owned_session(self, **kwargs):
        browser_session = self.client.session
        if not browser_session.session_key:
            browser_session.save()
        kwargs["owner_session_key"] = browser_session.session_key
        return AgentSession.objects.create(**kwargs)

    def _jpg_bytes(self):
        buf = BytesIO()
        Image.new("RGB", (64, 64), (80, 120, 160)).save(buf, "JPEG")
        return buf.getvalue()

    def _candidate(self, cloud=8.5):
        return EarthSearchProvider().candidate_from_item({
            "id": "S2A_AGENT",
            "collection": "sentinel-2-l2a",
            "bbox": [108.1, 22.1, 108.5, 22.9],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "updated": "2026-04-12T08:00:00Z",
                "eo:cloud_cover": cloud,
                "s2:product_uri": f"S2A_AGENT_{cloud}.SAFE",
            },
            "assets": {
                "visual": {"href": "https://example.com/visual.tif", "gsd": 10},
                "green": {"href": "https://example.com/green.tif", "gsd": 10},
                "nir": {"href": "https://example.com/nir.tif", "gsd": 10},
            },
        })

    def _candidate_with_bbox(self, item_id, bbox, cloud=8.5):
        return EarthSearchProvider().candidate_from_item({
            "id": item_id,
            "collection": "sentinel-2-l2a",
            "bbox": bbox,
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "updated": "2026-04-12T08:00:00Z",
                "eo:cloud_cover": cloud,
                "s2:product_uri": f"{item_id}.SAFE",
            },
            "assets": {
                "visual": {"href": f"https://example.com/{item_id}.tif", "gsd": 10},
                "green": {"href": f"https://example.com/{item_id}_green.tif", "gsd": 10},
                "nir": {"href": f"https://example.com/{item_id}_nir.tif", "gsd": 10},
            },
        })

    def _domain_patches(self, candidate=None, cloud=8.5):
        """工具内部调用的领域函数 mock（不含 agent_step，由各测试脚本化）。"""
        candidate = candidate or self._candidate(cloud)
        return [
            patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "DASHSCOPE_API_KEY": "test-dashscope-key",
                "AMAP_KEY": "test-amap-key",
            }, clear=False),
            patch("map_api.utils.agent_tools.call_deepseek_json", return_value={
                "place_name": "南宁市",
                "date_start": "2026-04-01",
                "date_end": "2026-04-30",
                "task": "water",
                "source": "sentinel2",
                "mode": "precise",
            }),
            patch("map_api.views.resolve_district_bbox", return_value={
                "name": "南宁市",
                "adcode": "450100",
                "level": "city",
                "bbox": {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.5, "max_lat": 22.9},
                "candidate_count": 1,
                "bbox_policy": "行政区 bbox 筛查，不做精确行政边界裁剪",
            }),
            patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]),
            patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=self._jpg_bytes()),
            patch("map_api.views.compute_ndwi_summary", return_value={
                "available": True,
                "method": "NDWI=(Green-NIR)/(Green+NIR)",
                "threshold": 0.1,
                "water_percent": 18.5,
                "water_ratio": 0.185,
                "limitations": "轻量 NDWI 仅用于 bbox 内水体线索筛查。",
            }),
            patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                status_code=HTTPStatus.OK,
                output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": "<answer>水体主要分布在河道和坑塘。</answer>"}]))]),
                message="",
            )),
        ]

    def _post_session(self, st, script, extra_patches=None, **post_data):
        for p in self._domain_patches():
            st.enter_context(p)
        st.enter_context(patch("map_api.agent.loop.agent_step", side_effect=script))
        for p in (extra_patches or []):
            st.enter_context(p)
        body = {"goal": self.GOAL, "sync": True}
        body.update(post_data)
        return self.client.post("/api/agent/sessions/", data=body, content_type="application/json")

    @staticmethod
    def _opt_labels(options):
        return [o["label"] if isinstance(o, dict) else o for o in (options or [])]

    def _cleanup_img(self, data):
        image_file = (data or {}).get("artifacts", {}).get("file_name")
        if image_file:
            p = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", image_file)
            if os.path.exists(p):
                os.remove(p)

    def test_agent_session_requires_deepseek_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": self.GOAL},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("DEEPSEEK_API_KEY", r.json()["msg"])

    def test_agent_queue_mode_leaves_persisted_claim_for_worker(self):
        """生产 queue 模式不能启动易丢失的 Web daemon thread。"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key", "AGENT_EXECUTION_MODE": "queue"}, clear=False), \
             patch("map_api.views._run_agent_background") as background:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": self.GOAL},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], AgentSession.STATUS_RUNNING)
        self.assertFalse(data["artifacts"].get("worker_claim"))
        self.assertTrue(data["artifacts"].get("queued_at"))
        background.assert_called_once()
        AgentSession.objects.filter(id=data["id"]).delete()

    def test_run_agent_background_queue_mode_does_not_spawn_thread(self):
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "queue"}, clear=False), \
             patch("map_api.orchestrator.threading.Thread") as thread:
            started = _run_agent_background(123, {"worker_claim": "worker-1"})
        self.assertFalse(started)
        thread.assert_not_called()

    def test_queue_report_job_is_persistent_and_worker_completes_it(self):
        session = self._owned_session(
            goal="队列报告实验", status=AgentSession.STATUS_COMPLETED,
            artifacts={"file_name": "agent_queue_report.jpg", "bbox": {"min_lng": 1}},
            messages=[{"role": "user", "content": "队列报告实验"}],
        )
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "queue"}, clear=False):
            response = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "queue-report-1"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 202)
        job = ReportJob.objects.get(agent_session=session)
        self.assertEqual(job.status, ReportJob.STATUS_QUEUED)
        session.refresh_from_db()
        self.assertEqual(session.message_request_states["queue-report-1"]["status"], "processing")
        fake = JsonResponse({"code": 200, "data": {"file_name": "agent_queue_report.docx"}})
        with patch("map_api.views.build_report", return_value=fake):
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        job.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(job.status, ReportJob.STATUS_COMPLETED)
        self.assertEqual(session.artifacts["report"]["file_name"], "agent_queue_report.docx")
        self.assertEqual(session.message_request_states["queue-report-1"], "done")
        session.delete()

    def test_failed_queue_report_rolls_back_message_for_retry(self):
        session = self._owned_session(
            goal="报告失败重试", status=AgentSession.STATUS_COMPLETED,
            artifacts={"file_name": "agent_queue_retry.jpg"},
        )
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "queue"}, clear=False):
            response = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "queue-retry-1"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 202)
        failed = JsonResponse({"code": 500, "msg": "模拟报告失败"}, status=500)
        with patch("map_api.views.build_report", return_value=failed):
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        session.refresh_from_db()
        self.assertNotIn("queue-retry-1", session.message_request_states)
        self.assertFalse(session.artifacts.get("report_generating"))
        self.assertEqual(session.artifacts.get("report_error"), "模拟报告失败")
        self.assertEqual(ReportJob.objects.get(agent_session=session).status, ReportJob.STATUS_FAILED)

    def test_parallel_queue_report_requests_share_one_job_without_stuck_message(self):
        session = self._owned_session(
            goal="并发报告幂等", status=AgentSession.STATUS_COMPLETED,
            artifacts={"file_name": "agent_queue_parallel.jpg"},
        )
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "queue"}, clear=False):
            first = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "parallel-report-1"},
                content_type="application/json",
            )
            second = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "parallel-report-2"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(ReportJob.objects.filter(agent_session=session).count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.message_request_states["parallel-report-1"]["status"], "processing")
        self.assertEqual(session.message_request_states["parallel-report-2"], "done")

    def test_stale_report_worker_deletes_unclaimed_generated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = AgentSession.objects.create(goal="旧报告 worker", status=AgentSession.STATUS_COMPLETED)
            job = ReportJob.objects.create(
                agent_session=session, request_key="stale-report-owner", payload={},
                status=ReportJob.STATUS_RUNNING, worker_claim="old-worker", claimed_at=timezone.now(),
            )
            report_name = "report_stale_owner.docx"
            report_path = os.path.join(tmp, report_name)

            def build_and_lose_claim(_payload):
                with open(report_path, "wb") as handle:
                    handle.write(b"orphan")
                ReportJob.objects.filter(id=job.id).update(worker_claim="new-worker")
                return JsonResponse({"code": 200, "data": {"file_name": report_name}})

            with patch.object(settings, "MEDIA_ROOT", tmp), patch("map_api.views.build_report", side_effect=build_and_lose_claim):
                completed = execute_report_job(job.id, "old-worker")
            self.assertFalse(completed)
            self.assertFalse(os.path.exists(report_path))
            job.refresh_from_db()
            self.assertEqual(job.worker_claim, "new-worker")

    def test_agent_rejects_non_object_json_without_creating_session(self):
        before = AgentSession.objects.count()
        r = self.client.post(
            "/api/agent/sessions/",
            data="[]",
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["msg"], "请求体必须是 JSON 对象")
        self.assertEqual(AgentSession.objects.count(), before)

    def test_agent_rejects_oversized_goal_without_creating_session(self):
        before = AgentSession.objects.count()
        r = self.client.post(
            "/api/agent/sessions/",
            data={"goal": "x" * 4001},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能超过 4000", r.json()["msg"])
        self.assertEqual(AgentSession.objects.count(), before)

    def test_agent_rejects_oversized_message_without_mutating_session(self):
        session = self._owned_session(
            goal=self.GOAL,
            status=AgentSession.STATUS_COMPLETED,
            messages=[{"role": "user", "content": self.GOAL}],
        )
        before = list(session.messages)
        r = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "x" * 8001},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能超过 8000", r.json()["msg"])
        session.refresh_from_db()
        self.assertEqual(session.messages, before)

    def test_mocked_agent_full_loop_completes(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
                _agent_call("compute_ndwi", current_step="ndwi"),
                _agent_call("analyze_imagery", current_step="vl_analysis"),
                _agent_final("复核结论：南宁市 2026 年 4 月 bbox 范围内可见河流、湖库和坑塘水体，NDWI 显示可能水体约 18.5%，该比例仅作筛查。"),
            ])
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["slots"]["task"], "water")
        self.assertEqual(data["slots"]["source"], "sentinel2")
        self.assertEqual(data["artifacts"]["ndwi"]["water_percent"], 18.5)
        self.assertIn("复核结论", data["artifacts"]["final_answer"])
        self.assertEqual(data["observer"]["current_step"], "complete")
        self.assertIn("整理", data["observer"]["public_thought"])
        self.assertTrue(data["observer"]["plan_steps"])
        self.assertTrue(ChatHistory.objects.filter(id=data["history_id"]).exists())
        self.assertTrue(AgentSession.objects.filter(id=data["id"], status=AgentSession.STATUS_COMPLETED).exists())
        self._cleanup_img(data)

    def test_agent_uses_sentinel_mosaic_for_city_bbox(self):
        left = self._candidate_with_bbox("S2A_LEFT", [108.1, 22.1, 108.3, 22.9])
        right = self._candidate_with_bbox("S2A_RIGHT", [108.3, 22.1, 108.5, 22.9])
        left_img = Image.new("RGB", (64, 64), (0, 0, 0))
        right_img = Image.new("RGB", (64, 64), (0, 0, 0))
        for x in range(32):
            for y in range(64):
                left_img.putpixel((x, y), (90, 120, 150))
        for x in range(32, 64):
            for y in range(64):
                right_img.putpixel((x, y), (80, 130, 100))
        left_buf, right_buf = BytesIO(), BytesIO()
        left_img.save(left_buf, "JPEG")
        right_img.save(right_buf, "JPEG")
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
                _agent_call("compute_ndwi", current_step="ndwi"),
                _agent_call("analyze_imagery", current_step="vl_analysis"),
                _agent_final("复核结论：多景拼接后水体约 21.0%，该比例仅作筛查。"),
            ], extra_patches=[
                patch("map_api.views.EarthSearchProvider.search", return_value=[left, right]),
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", side_effect=[left_buf.getvalue(), right_buf.getvalue()]),
                patch("map_api.views.compute_ndwi_mosaic_summary", return_value={
                    "available": True,
                    "method": "NDWI=(Green-NIR)/(Green+NIR)",
                    "aggregation": "按每景有效像元数加权汇总",
                    "water_percent": 21.0,
                    "water_ratio": 0.21,
                    "sample_size_px": 4096,
                    "limitations": "多景 NDWI 为 bbox 筛查级加权结果。",
                }),
            ])
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "completed")
        scene = ImageryScene.objects.get(id=data["scene_id"])
        self.assertTrue(scene.metadata["mosaic"])
        self.assertEqual(scene.metadata["mosaic_candidate_count"], 2)
        self.assertGreaterEqual(scene.metadata["target_coverage_ratio"], 0.99)
        self.assertGreaterEqual(scene.metadata["valid_image_ratio"], 0.95)
        self.assertEqual(data["artifacts"]["ndwi"]["water_percent"], 21.0)
        self._cleanup_img(data)

    def test_agent_waits_when_sentinel_has_no_candidate(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[patch("map_api.views.EarthSearchProvider.search", return_value=[])])
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["status"], "waiting_user")
        self.assertIn("扩大时间范围", self._opt_labels(data["artifacts"]["waiting"]["options"]))
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertEqual(data["observer"]["current_status"], "waiting_user")
        retrieve_step = [s for s in data["observer"]["plan_steps"] if s["id"] == "retrieve_imagery"][0]
        self.assertEqual(retrieve_step["status"], "waiting_user")

    def test_agent_waits_when_sentinel_provider_errors(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[patch("map_api.views.EarthSearchProvider.search", side_effect=requests.HTTPError("bad request"))])
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertIn("切换高清底图", self._opt_labels(data["artifacts"]["waiting"]["options"]))

    def test_agent_waits_when_sentinel_candidate_coverage_is_too_low(self):
        low_coverage = EarthSearchProvider().candidate_from_item({
            "id": "S2A_LOW_COVERAGE",
            "collection": "sentinel-2-l2a",
            "bbox": [108.1, 22.1, 108.2, 108.2],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "eo:cloud_cover": 8,
                "s2:product_uri": "S2A_LOW_COVERAGE.SAFE",
            },
            "assets": {
                "visual": {"href": "https://example.com/visual.tif", "gsd": 10},
                "green": {"href": "https://example.com/green.tif", "gsd": 10},
                "nir": {"href": "https://example.com/nir.tif", "gsd": 10},
            },
        })
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[
                patch("map_api.views.EarthSearchProvider.search", return_value=[low_coverage]),
            ])
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertIn("Sentinel-2 候选覆盖不足", data["artifacts"]["waiting"]["message"])

    def test_agent_waits_on_high_cloud(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[
                patch("map_api.views.EarthSearchProvider.search", return_value=[self._candidate(cloud=45)]),
            ])
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["status"], "waiting_user")
        self.assertIn("云量", data["artifacts"]["waiting"]["message"])
        self.assertEqual(data["observer"]["current_step"], "quality_check")
        self.assertEqual(data["observer"]["current_status"], "waiting_user")
        self.assertEqual(data["observer"]["next"], "等待用户确认")
        self._cleanup_img(data)

    def test_agent_waits_when_vision_output_is_unstable(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
                _agent_call("compute_ndwi", current_step="ndwi"),
                _agent_call("analyze_imagery", current_step="vl_analysis"),
            ], extra_patches=[
                patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                    status_code=HTTPStatus.OK,
                    output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": ""}]))]),
                    message="",
                )),
            ])
        data = r.json()["data"]
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "vl_analysis")
        self.assertIn("快速模式重试", self._opt_labels(data["artifacts"]["waiting"]["options"]))
        self._cleanup_img(data)

    def test_agent_cancel_updates_observer(self):
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[patch("map_api.views.EarthSearchProvider.search", return_value=[])])
        session_id = r.json()["data"]["id"]
        r2 = self.client.post(
            f"/api/agent/sessions/{session_id}/messages/",
            data={"content": "取消任务"},
            content_type="application/json",
        )
        self.assertEqual(r2.status_code, 200)
        data = r2.json()["data"]
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["observer"]["current_step"], "failed")
        self.assertEqual(data["observer"]["current_status"], "failed")
        self.assertNotIn("waiting", data["artifacts"])

    def test_agent_cancel_running_session_sets_persistent_cancel_flag(self):
        session = self._owned_session(
            goal="运行中取消测试",
            status=AgentSession.STATUS_RUNNING,
            artifacts={"tool_history": []},
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"action": "cancel", "content": "取消调查"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_FAILED)
        self.assertTrue(session.cancel_requested)
        self.assertTrue((session.artifacts or {}).get("cancel_requested"))
        self.assertIn("调查已取消", session.error + " " + str(session.messages))

    def test_stale_worker_writes_preserve_cancel_flag(self):
        session = self._owned_session(
            goal="旧快照覆盖测试",
            status=AgentSession.STATUS_RUNNING,
            artifacts={"tool_history": [], "observer": {"current_step": "locate"}},
            slots={"place_name": "测试区域"},
        )
        stale = AgentSession.objects.get(id=session.id)
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"action": "cancel", "content": "取消调查"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        # 模拟后台线程仍持有取消前的旧 ORM 快照并继续写 observer/持久化。
        _agent_step(stale, "quality_check", "质量检查", "running", "旧线程写入")
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentSession.STATUS_FAILED)
        self.assertTrue(stale.cancel_requested)
        self.assertTrue((stale.artifacts or {}).get("cancel_requested"))
        self.assertNotIn("final_answer", stale.artifacts or {})

    def test_stale_wait_write_cannot_resurrect_cancelled_session(self):
        session = self._owned_session(
            goal="等待竞态测试", status=AgentSession.STATUS_RUNNING,
            artifacts={"observer": {"current_step": "retrieve_imagery"}},
        )
        stale = AgentSession.objects.get(id=session.id)
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"action": "cancel", "content": "取消调查"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        _agent_wait(stale, "旧线程等待确认")
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentSession.STATUS_FAILED)
        self.assertTrue((stale.artifacts or {}).get("cancel_requested"))
        self.assertNotIn("waiting", stale.artifacts or {})

    def test_agent_message_append_preserves_latest_worker_messages(self):
        session = self._owned_session(
            goal="消息并发测试", status=AgentSession.STATUS_RUNNING,
            messages=[{"role": "assistant", "content": "后台已进入检索"}],
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "继续"}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        contents = [m.get("content") for m in session.messages]
        self.assertIn("后台已进入检索", contents)
        self.assertIn("继续", contents)

    def test_agent_message_request_id_is_idempotent(self):
        session = self._owned_session(
            goal="消息幂等测试", status=AgentSession.STATUS_RUNNING,
            messages=[{"role": "assistant", "content": "等待输入"}],
        )
        payload = {"content": "继续分析", "message_id": "message-idem-001"}
        first = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data=payload, content_type="application/json",
        )
        second = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data=payload, content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        session.refresh_from_db()
        self.assertEqual([m.get("content") for m in session.messages].count("继续分析"), 1)
        self.assertEqual(second.json()["msg"], "已忽略重复消息请求")

    def test_agent_message_rejects_conflicting_idempotency_header(self):
        session = self._owned_session(goal="消息冲突键", status=AgentSession.STATUS_RUNNING)
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "继续", "message_id": "body-key"},
            HTTP_IDEMPOTENCY_KEY="header-key",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不一致", response.json()["msg"])

    def test_agent_message_processing_state_returns_202_without_reexecution(self):
        session = self._owned_session(
            goal="消息处理中测试", status=AgentSession.STATUS_RUNNING,
            message_request_states={"in-flight-1": "processing"},
            messages=[{"role": "assistant", "content": "处理中"}],
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "继续", "message_id": "in-flight-1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        session.refresh_from_db()
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(session.message_request_states["in-flight-1"], "processing")

    def test_running_action_is_not_appended_as_a_fake_user_message(self):
        session = self._owned_session(
            goal="运行中 action 测试", status=AgentSession.STATUS_RUNNING,
            messages=[{"role": "assistant", "content": "执行中"}],
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "继续", "action": "continue", "message_id": "running-action-1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        session.refresh_from_db()
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(session.message_request_states, {})

    def test_stale_processing_message_state_can_be_reclaimed(self):
        session = self._owned_session(
            goal="过期消息状态接管", status=AgentSession.STATUS_RUNNING,
            message_request_states={"stale-1": {"status": "processing", "started_at": (timezone.now() - timedelta(seconds=1200)).isoformat()}},
            messages=[{"role": "assistant", "content": "旧进程已退出"}],
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "重试", "message_id": "stale-1"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.message_request_states["stale-1"], "done")
        self.assertEqual([m.get("content") for m in session.messages].count("重试"), 1)

    def test_failed_message_action_can_retry_same_message_id(self):
        session = self._owned_session(
            goal="动作失败重试", status=AgentSession.STATUS_WAITING_USER,
            artifacts={"waiting": {"message": "确认"}},
        )
        with patch("map_api.views.resume_waiting_agent_session", side_effect=[RuntimeError("temporary"), None]) as resume_mock:
            first = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"content": "继续", "message_id": "retry-action-1"},
                content_type="application/json",
            )
            second = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"content": "继续", "message_id": "retry-action-1"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 400)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(resume_mock.call_count, 2)
        session.refresh_from_db()
        self.assertEqual(session.message_request_states["retry-action-1"], "done")
        self.assertEqual([m.get("content") for m in session.messages].count("继续"), 1)

    def test_new_session_claim_prevents_duplicate_recovery_worker(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session") as run_mock:
            response = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "重复 worker 竞争测试"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        session_id = response.json()["data"]["id"]
        session = AgentSession.objects.get(id=session_id)
        self.assertTrue(str((session.artifacts or {}).get("worker_claim", "")).startswith("thread:"))
        out = StringIO()
        with patch("map_api.management.commands.run_agent_worker.run_agent_session") as worker_run:
            call_command("run_agent_worker", "--once", stdout=out)
        worker_run.assert_not_called()
        self.assertIn("本轮处理 0 个 Agent 会话", out.getvalue())

    def test_agent_start_request_id_is_idempotent(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session") as run_mock:
            payload = {"goal": "幂等启动测试", "request_id": "req-idempotent-001"}
            first = self.client.post("/api/agent/sessions/", data=payload, content_type="application/json")
            second = self.client.post("/api/agent/sessions/", data=payload, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])
        self.assertEqual(AgentSession.objects.filter(request_id="req-idempotent-001").count(), 1)
        run_mock.assert_called_once()

    def test_agent_start_request_id_rejects_payload_mismatch(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session"):
            first = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "同一键原始目标", "request_id": "req-conflict-001", "mode": "precise"},
                content_type="application/json",
            )
            second = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "同一键不同目标", "request_id": "req-conflict-001", "mode": "precise"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertIn("不同的调查请求", second.json()["msg"])

    def test_agent_start_rejects_conflicting_idempotency_header(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            response = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "冲突键", "request_id": "body-key"},
                HTTP_IDEMPOTENCY_KEY="header-key",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不一致", response.json()["msg"])

    def test_idempotent_retry_returns_existing_session_without_api_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session"):
            first = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "密钥撤销后的幂等重试", "request_id": "req-key-retry-001"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            second = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "密钥撤销后的幂等重试", "request_id": "req-key-retry-001"},
                content_type="application/json",
            )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])

    def test_agent_start_rejects_invalid_bbox_before_creating_session(self):
        before = AgentSession.objects.count()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            response = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "非法区域", "bbox": {"min_lng": 181, "min_lat": 0, "max_lng": 182, "max_lat": 1}},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AgentSession.objects.count(), before)

    def test_agent_different_request_ids_create_independent_sessions(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session"):
            first = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "同目标独立实验", "request_id": "req-independent-1"},
                content_type="application/json",
            )
            second = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "同目标独立实验", "request_id": "req-independent-2"},
                content_type="application/json",
            )
        self.assertNotEqual(first.json()["data"]["id"], second.json()["data"]["id"])

    def test_agent_session_isolation_hides_detail_from_other_browser(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session"):
            first = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "浏览器甲任务", "request_id": "owner-isolation-1"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        session_id = first.json()["data"]["id"]
        other_client = Client()
        detail = other_client.get(f"/api/agent/sessions/{session_id}/")
        self.assertEqual(detail.status_code, 404)
        listing = other_client.get("/api/agent/sessions/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["data"], [])

    def test_legacy_unowned_agent_is_quarantined_from_public_api(self):
        legacy = AgentSession.objects.create(
            goal="历史无归属任务", request_id="legacy-null-owner-1",
            status=AgentSession.STATUS_WAITING_USER,
        )
        self.assertEqual(self.client.get(f"/api/agent/sessions/{legacy.id}/").status_code, 404)
        message = self.client.post(
            f"/api/agent/sessions/{legacy.id}/messages/",
            data={"content": "尝试认领"}, content_type="application/json",
        )
        self.assertEqual(message.status_code, 404)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            replay = self.client.post(
                "/api/agent/sessions/",
                data={"goal": legacy.goal, "request_id": legacy.request_id},
                content_type="application/json",
            )
        self.assertEqual(replay.status_code, 404)
        legacy.refresh_from_db()
        self.assertIsNone(legacy.owner_session_key)

    def test_legacy_unowned_agent_files_are_quarantined(self):
        image_name = "agent_legacy_null_owner.jpg"
        report_name = "report_legacy_null_owner.docx"
        image_path = os.path.join(SAVE_DIR, image_name)
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        os.makedirs(SAVE_DIR, exist_ok=True)
        with open(image_path, "wb") as handle:
            handle.write(self._jpg_bytes())
        with open(report_path, "wb") as handle:
            handle.write(b"legacy report")
        scene = ImageryScene.objects.create(
            file_name=image_name, min_lng=0, min_lat=0, max_lng=1, max_lat=1,
        )
        AgentSession.objects.create(
            goal="历史文件隔离", status=AgentSession.STATUS_COMPLETED, scene=scene,
            artifacts={"report": {"file_name": report_name}},
        )
        try:
            image_response = self.client.get(f"/api/satellite/show-img/?file={image_name}")
            report_response = self.client.get(f"/api/report/download/?file={report_name}")
            try:
                self.assertEqual(image_response.status_code, 404)
                self.assertEqual(report_response.status_code, 404)
            finally:
                image_response.close()
                report_response.close()
        finally:
            for path in (image_path, report_path):
                if os.path.exists(path):
                    os.remove(path)

    def test_agent_read_probe_does_not_create_unbounded_sessions(self):
        session = AgentSession.objects.create(
            goal="探测不应创建 session",
            status=AgentSession.STATUS_COMPLETED,
            owner_session_key="owner-key-for-probe",
        )
        before = Session.objects.count()
        probe_client = Client()
        response = probe_client.get(f"/api/agent/sessions/{session.id}/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Session.objects.count(), before)

    def test_agent_request_id_cannot_be_replayed_from_other_browser(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views.run_agent_session"):
            first = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "浏览器甲幂等任务", "request_id": "owner-idempotency-1"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        other_client = Client()
        replay = other_client.post(
            "/api/agent/sessions/",
            data={"goal": "浏览器甲幂等任务", "request_id": "owner-idempotency-1"},
            content_type="application/json",
        )
        self.assertEqual(replay.status_code, 404)

    def test_agent_files_are_hidden_from_other_browser(self):
        self.client.get("/api/agent/sessions/")  # 创建并持久化 Django session cookie
        owner_key = self.client.session.session_key
        image_name = "agent_secure_isolation_test.jpg"
        report_name = "report_secure_isolation_test.docx"
        image_path = os.path.join(SAVE_DIR, image_name)
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        os.makedirs(SAVE_DIR, exist_ok=True)
        with open(image_path, "wb") as handle:
            handle.write(self._jpg_bytes())
        with open(report_path, "wb") as handle:
            handle.write(b"test report")
        scene = ImageryScene.objects.create(
            file_name=image_name,
            source="mapbox",
            min_lng=0,
            min_lat=0,
            max_lng=1,
            max_lat=1,
        )
        AgentSession.objects.create(
            goal="文件隔离实验",
            status=AgentSession.STATUS_COMPLETED,
            owner_session_key=owner_key,
            scene=scene,
            artifacts={"report": {"file_name": report_name}},
        )
        try:
            other = Client()
            self.assertEqual(other.get(f"/api/satellite/show-img/?file={image_name}").status_code, 404)
            self.assertEqual(other.get(f"/api/report/download/?file={report_name}").status_code, 404)
            owned_image = self.client.get(f"/api/satellite/show-img/?file={image_name}")
            owned_report = self.client.get(f"/api/report/download/?file={report_name}")
            self.assertEqual(owned_image.status_code, 200)
            self.assertEqual(owned_report.status_code, 200)
            owned_image.close()
            owned_report.close()
        finally:
            for path in (image_path, report_path):
                if os.path.exists(path):
                    os.remove(path)

    def test_worker_lease_heartbeat_and_cancel_guard(self):
        session = AgentSession.objects.create(
            goal="租约心跳测试", status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "thread:heartbeat", "worker_claimed_at": (timezone.now() - timedelta(seconds=1200)).isoformat()},
        )
        before = session.artifacts["worker_claimed_at"]
        self.assertTrue(_touch_worker_lease(session))
        session.refresh_from_db()
        self.assertNotEqual(session.artifacts["worker_claimed_at"], before)
        session.cancel_requested = True
        session.status = AgentSession.STATUS_FAILED
        session.save(update_fields=["cancel_requested", "status"])
        self.assertFalse(_touch_worker_lease(session))

    def test_stale_claim_cannot_write_observer_or_waiting_state(self):
        session = AgentSession.objects.create(
            goal="过期令牌写入测试", status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "worker-new"},
        )
        self.assertFalse(_agent_step(session, "locate", "定位", "running", "旧 worker", expected_claim="worker-old"))
        self.assertFalse(_agent_wait(session, "旧 worker 等待", expected_claim="worker-old"))
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_RUNNING)
        self.assertNotIn("waiting", session.artifacts or {})

    def test_recovery_worker_marks_unhandled_exception_terminal(self):
        session = AgentSession.objects.create(
            goal="worker 异常终态测试", status=AgentSession.STATUS_RUNNING,
        )
        with patch("map_api.management.commands.run_agent_worker.run_agent_session", side_effect=RuntimeError("模拟 worker 崩溃")):
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_FAILED)
        self.assertIn("worker 执行异常", session.error)
        self.assertNotIn("worker_claim", session.artifacts or {})

    def test_recovery_worker_skips_claimed_head_and_processes_next(self):
        claimed = AgentSession.objects.create(
            goal="队列头已被占用", status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "thread:other", "worker_claimed_at": timezone.now().isoformat()},
        )
        available = AgentSession.objects.create(goal="队列下一条可执行", status=AgentSession.STATUS_RUNNING)
        with patch("map_api.management.commands.run_agent_worker.run_agent_session") as run_mock:
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        run_mock.assert_called_once()
        call_args = run_mock.call_args.args
        self.assertEqual(call_args[0], available.id)
        self.assertFalse(call_args[1]["resume_with_scene"])
        available.refresh_from_db()
        self.assertEqual(call_args[1]["worker_claim"], available.artifacts.get("worker_claim"))
        claimed.refresh_from_db()
        self.assertEqual(claimed.artifacts.get("worker_claim"), "thread:other")

    def test_recovery_worker_stops_cleanly_on_keyboard_interrupt(self):
        out = StringIO()
        with patch("map_api.management.commands.run_agent_worker.time.sleep", side_effect=KeyboardInterrupt):
            call_command("run_agent_worker", "--poll-seconds", "1", stdout=out)
        self.assertIn("安全退出", out.getvalue())

    def test_agent_cancel_completed_session_does_not_rewrite_terminal_state(self):
        session = self._owned_session(
            goal="已完成取消测试",
            status=AgentSession.STATUS_COMPLETED,
            artifacts={"final_answer": "已完成"},
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"action": "cancel", "content": "取消调查"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_COMPLETED)
        self.assertFalse(session.cancel_requested)
        self.assertFalse((session.artifacts or {}).get("cancel_requested", False))

    def test_failed_agent_rejects_new_message_without_mutation(self):
        session = self._owned_session(
            goal="失败消息拒绝测试", status=AgentSession.STATUS_FAILED,
            error="模拟失败", messages=[{"role": "assistant", "content": "任务失败"}],
        )
        response = self.client.post(
            f"/api/agent/sessions/{session.id}/messages/",
            data={"content": "继续", "message_id": "failed-new-message"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        session.refresh_from_db()
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(session.message_request_ids, [])

    def test_agent_report_generation_is_idempotent(self):
        session = self._owned_session(
            goal="报告幂等测试", status=AgentSession.STATUS_COMPLETED,
            messages=[{"role": "user", "content": "报告幂等测试"}],
            artifacts={"file_name": "report-idem.jpg", "bbox": {"min_lng": 1}},
        )
        fake = JsonResponse({"code": 200, "data": {"file_name": "report-idem.docx"}})
        with patch("map_api.views.build_report", return_value=fake) as build_mock:
            first = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "report-1"},
                content_type="application/json",
            )
            second = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "report-2"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(build_mock.call_count, 1)
        session.refresh_from_db()
        self.assertEqual(session.artifacts["report"]["file_name"], "report-idem.docx")
        self.assertEqual([m.get("content") for m in session.messages].count("Word 报告已生成。"), 1)

    def test_failed_report_request_can_retry_same_message_id(self):
        session = self._owned_session(
            goal="报告失败重试", status=AgentSession.STATUS_COMPLETED,
            messages=[{"role": "user", "content": "报告失败重试"}],
            artifacts={"file_name": "report-retry.jpg"},
        )
        failed = JsonResponse({"code": 500, "msg": "temporary report failure"}, status=500)
        success = JsonResponse({"code": 200, "data": {"file_name": "report-retry.docx"}})
        with patch("map_api.views.build_report", side_effect=[failed, success]) as build_mock:
            first = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "report-retry-1"},
                content_type="application/json",
            )
            second = self.client.post(
                f"/api/agent/sessions/{session.id}/messages/",
                data={"action": "generate_report", "message_id": "report-retry-1"},
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 500)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(build_mock.call_count, 2)
        session.refresh_from_db()
        self.assertEqual(session.message_request_states["report-retry-1"], "done")
        self.assertNotIn("report_generating", session.artifacts)

    def test_agent_uses_current_scene_context(self):
        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        image_file = "agent_current_scene.jpg"
        Image.new("RGB", (64, 64), (80, 120, 160)).save(os.path.join(save_dir, image_file), "JPEG")
        scene = ImageryScene.objects.create(
            file_name=image_file,
            source="mapbox",
            source_label="Mapbox Satellite Basemap",
            min_lng=1, min_lat=2, max_lng=3, max_lat=4,
            gsd_m=1.5, area_km2=10,
        )
        with ExitStack() as st:
            st.enter_context(patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "DASHSCOPE_API_KEY": "test-dashscope-key",
                "AMAP_KEY": "test-amap-key",
            }, clear=False))
            st.enter_context(patch("map_api.utils.agent_tools.call_deepseek_json", return_value={"task": "built_up", "source": "mapbox"}))
            st.enter_context(patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                status_code=HTTPStatus.OK,
                output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": "<answer>当前区域以建设用地为主。</answer>"}]))]),
                message="",
            )))
            st.enter_context(patch("map_api.agent.loop.agent_step", side_effect=[
                _agent_call("analyze_imagery", current_step="vl_analysis"),
                _agent_final("复核结论：当前区域以建设用地为主。"),
            ]))
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "调查当前区域建设用地", "scene_id": scene.id, "file_name": image_file, "sync": True},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["scene_id"], scene.id)
        self.assertEqual(data["slots"]["bbox"], {"min_lng": 1.0, "min_lat": 2.0, "max_lng": 3.0, "max_lat": 4.0})
        os.remove(os.path.join(save_dir, image_file))

    def test_agent_fails_when_exceeding_max_iterations(self):
        # 模型一直只调用 geocode_place（不收敛），耗尽迭代上限应 failed
        loop_forever = [_agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate")] * 20
        with ExitStack() as st:
            r = self._post_session(st, loop_forever)
        data = r.json()["data"]
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["observer"]["current_step"], "failed")

    def test_agent_resume_via_action_code(self):
        # 高云量触发 waiting，用结构化 action code 恢复（而非中文 label）
        with ExitStack() as st:
            r = self._post_session(st, [
                _agent_call("geocode_place", {"place_name": "南宁市"}, current_step="locate"),
                _agent_call("search_sentinel_imagery", current_step="retrieve_imagery"),
            ], extra_patches=[patch("map_api.views.EarthSearchProvider.search", return_value=[self._candidate(cloud=45)])])
        session_id = r.json()["data"]["id"]
        self.assertEqual(r.json()["data"]["status"], "waiting_user")
        self._cleanup_img(r.json()["data"])

        def _sync_run(sid, context=None):
            run_agent_session(sid, context or {})

        with ExitStack() as st2:
            for p in self._domain_patches():
                st2.enter_context(p)
            st2.enter_context(patch("map_api.views.EarthSearchProvider.search", return_value=[self._candidate(cloud=8.5)]))
            st2.enter_context(patch("map_api.views._run_agent_background", side_effect=_sync_run))
            st2.enter_context(patch("map_api.agent.loop.agent_step", side_effect=[
                _agent_call("compute_ndwi", current_step="ndwi"),
                _agent_call("analyze_imagery", current_step="vl_analysis"),
                _agent_final("复核结论：继续分析后水体约 18.5%。"),
            ]))
            r2 = self.client.post(
                f"/api/agent/sessions/{session_id}/messages/",
                data={"content": "继续分析", "action": "continue"},
                content_type="application/json",
            )
        data2 = r2.json()["data"]
        self.assertEqual(r2.status_code, 200, r2.content.decode())
        self.assertEqual(data2["status"], "completed")
        self.assertIn("复核结论", data2["artifacts"]["final_answer"])
        self._cleanup_img(data2)


class ReportSceneTests(TestCase):
    def test_report_rejects_missing_image_file(self):
        r = self.client.post(
            "/api/report/generate/",
            data={"file_name": "missing_report.jpg", "messages": []},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["code"], 410)

    def test_report_rejects_non_post(self):
        r = self.client.get("/api/report/generate/")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(r.json()["code"], 405)

    def test_report_includes_imagery_data_section(self):
        from PIL import Image
        from docx import Document

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, "sat_report_scene.jpg")
        Image.new("RGB", (32, 32), (80, 120, 160)).save(img_path, "JPEG")
        scene = ImageryScene.objects.create(
            file_name="sat_report_scene.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=1.2,
            limitations="测试限制说明",
        )
        r = self.client.post(
            "/api/report/generate/",
            data={"file_name": scene.file_name, "scene_id": scene.id, "messages": []},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        report_name = r.json()["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        doc = Document(report_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("影像数据说明", text)
        self.assertIn("测试限制说明", text)
        self.assertIn("质量摘要", text)
        self.assertIn("Mapbox 高清底图", text)
        self.assertIn("不能作为可复核的时效性证据", text)
        os.remove(img_path)
        os.remove(report_path)

    def test_report_includes_sentinel_candidate_selection_basis(self):
        from PIL import Image
        from docx import Document

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, "sentinel_report_selection.jpg")
        Image.new("RGB", (32, 32), (80, 120, 160)).save(img_path, "JPEG")
        scene = ImageryScene.objects.create(
            file_name="sentinel_report_selection.jpg",
            source="sentinel2",
            source_label="Sentinel-2 L2A",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=10,
            cloud_percent=3,
            processing_level="sentinel-2-l2a",
            license_type="sentinel_data_terms",
            decision_grade="screening",
            limitations="公开影像筛查限制",
            metadata={
                "selection_method": "suitability_score",
                "suitability_score": 88,
                "candidate_count": 3,
                "selection_rank": 2,
                "score_reasons": ["拍摄时间在 7 天内，时效性很好", "云量低于 10%，可视条件较好"],
                "render_fallback_errors": ["S2A_BEST.SAFE: bad cog"],
            },
        )
        r = self.client.post(
            "/api/report/generate/",
            data={"file_name": scene.file_name, "scene_id": scene.id, "messages": []},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        report_name = r.json()["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        doc = Document(report_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("候选优选方法：suitability_score", text)
        self.assertIn("候选优选分数：88", text)
        self.assertIn("候选池数量：3", text)
        self.assertIn("最终采用排序：第 2 个可渲染候选", text)
        self.assertIn("云量低于 10%，可视条件较好", text)
        self.assertIn("候选渲染降级记录：S2A_BEST.SAFE: bad cog", text)
        os.remove(img_path)
        os.remove(report_path)

    def test_report_includes_analysis_method_section(self):
        from PIL import Image
        from docx import Document

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, "sat_report_method.jpg")
        Image.new("RGB", (32, 32), (80, 120, 160)).save(img_path, "JPEG")
        scene = ImageryScene.objects.create(
            file_name="sat_report_method.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        messages = [{
            "role": "ai",
            "content": "分析结论",
            "analysis_method": {
                "mode": "precise",
                "model": "qwen3-vl-plus",
                "source": "mapbox",
                "task_label": "农业耕地与作物长势解译",
                "active_perception": True,
                "active_stages": 2,
                "strengths": ["高清视觉底图"],
                "limits": ["底图拍摄时间不透明"],
                "method_notes": ["问题偏细节，建议启用主动感知进行局部放大"],
                "imagery_quality": {
                    "summary": "Mapbox 高清底图，视觉细节较强",
                    "best_for": "适合建筑形态、道路结构和空间格局分析",
                    "cautions": ["不能作为可复核的时效性证据"],
                },
                "confidence": {
                    "level": "reference",
                    "label": "视觉参考级",
                    "basis": ["底图视觉细节较强，适合形态和空间格局判断"],
                    "required_checks": ["涉及时效性或行政决策时，需使用可追溯公开影像或现场资料复核"],
                },
                "source_recommendation": {
                    "recommended_source": "mapbox",
                    "recommended_label": "建议使用高清底图",
                    "current_source": "mapbox",
                    "alignment": "matched",
                    "reason": "问题包含建筑、道路、设施或计数等细节判读需求，需要更高视觉细节。",
                    "action": "当前图像源与任务匹配。",
                },
                "output_quality": {
                    "structured_answer": False,
                    "fallback_used": True,
                    "self_check_enabled": True,
                    "self_check_applied": False,
                    "warnings": ["模型未按 <answer> 结构化格式输出，已使用原文作为结论"],
                },
            },
        }]
        r = self.client.post(
            "/api/report/generate/",
            data={"file_name": scene.file_name, "scene_id": scene.id, "messages": messages},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        report_name = r.json()["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        doc = Document(report_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("AI 分析方法说明", text)
        self.assertIn("qwen3-vl-plus", text)
        self.assertIn("农业耕地与作物长势解译", text)
        self.assertIn("底图拍摄时间不透明", text)
        self.assertIn("影像质量摘要", text)
        self.assertIn("适合建筑形态", text)
        self.assertIn("结论可信度", text)
        self.assertIn("视觉参考级", text)
        self.assertIn("复核要求", text)
        self.assertIn("图像源建议", text)
        self.assertIn("建议使用高清底图", text)
        self.assertIn("输出稳定性", text)
        self.assertIn("使用兜底解析", text)
        self.assertIn("已请求但未触发", text)
        os.remove(img_path)
        os.remove(report_path)

    def test_report_sanitizes_ai_text_for_docx(self):
        from PIL import Image
        from docx import Document

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, "sat_report_safe_text.jpg")
        Image.new("RGB", (32, 32), (80, 120, 160)).save(img_path, "JPEG")
        scene = ImageryScene.objects.create(
            file_name="sat_report_safe_text.jpg",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
        )
        messages = [{
            "role": "ai",
            "content": "控制字符前\x0b控制字符后",
            "analysis_method": {
                "mode": "fast",
                "model": "qwen3-vl-flash\x0b",
                "source": "mapbox",
                "task_label": "综合土地利用解译",
                "limits": ["底图时效不透明\x0b"],
            },
        }]
        r = self.client.post(
            "/api/report/generate/",
            data={"file_name": scene.file_name, "scene_id": scene.id, "messages": messages},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 200)
        report_name = r.json()["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        doc = Document(report_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("控制字符前控制字符后", text)
        self.assertIn("qwen3-vl-flash", text)
        os.remove(img_path)
        os.remove(report_path)


class SafeMediaPathSecurityTests(SimpleTestCase):
    """safe_media_path 对抗性测试:路径穿越的各种姿势都必须被拒绝或限制在 base 内。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name

    def check(self, name):
        result = safe_media_path(self.base, name, (".jpg", ".jpeg", ".png"))
        if result is not None:
            base_real = os.path.realpath(self.base)
            self.assertTrue(
                result == base_real or result.startswith(base_real + os.sep),
                f"路径逃逸出 base:{name!r} -> {result!r}",
            )
        return result

    def test_dotdot_forward_slash(self):
        self.assertIsNone(self.check("../../etc/passwd"))

    def test_dotdot_backslash(self):
        # 反斜杠穿越:要么拒绝,要么落在 base 内(Linux 下反斜杠是合法文件名字符)
        self.assertIsNone(self.check("..\\..\\windows\\system32\\cmd.exe"))

    def test_absolute_unix(self):
        self.assertIsNone(self.check("/etc/passwd"))

    def test_absolute_windows(self):
        self.assertIsNone(self.check("C:\\Windows\\System32\\cmd.exe"))

    def test_bare_dotdot(self):
        self.assertIsNone(self.check(".."))

    def test_trailing_dotdot_component(self):
        self.assertIsNone(self.check("sat_1.jpg/.."))

    def test_empty_and_none(self):
        self.assertIsNone(self.check(""))
        self.assertIsNone(self.check(None))

    def test_disallowed_extension(self):
        self.assertIsNone(self.check("shell.php"))
        self.assertIsNone(self.check("page.html"))

    def test_null_byte(self):
        self.assertIsNone(self.check("a.jpg\x00.php"))

    def test_uppercase_extension_allowed_inside_base(self):
        result = self.check("A.JPG")
        self.assertIsNotNone(result)
        self.assertEqual(os.path.basename(result), "A.JPG")

    def test_double_extension_stays_inside_base(self):
        self.assertIsNotNone(self.check("evil.php.jpg"))

    def test_valid_name_resolves_inside_base(self):
        result = self.check("sat_abc123.jpg")
        self.assertIsNotNone(result)
        self.assertEqual(os.path.basename(result), "sat_abc123.jpg")


class RateLimitMiddlewareTests(TestCase):
    """IP 限流中间件:防公网滥用付费 API(R1)。"""

    def setUp(self):
        reset_rate_limit_state()

    def _post_ai(self):
        return self.client.post("/api/ai/query-region/", data={}, content_type="application/json")

    def test_ai_endpoint_rate_limited(self):
        with patch.dict(os.environ, {"RATELIMIT_AI_PER_MINUTE": "2"}):
            r1 = self._post_ai()
            r2 = self._post_ai()
            r3 = self._post_ai()
        self.assertEqual(r1.status_code, 400)  # 前两次到达视图(问题为空 → 400)
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r3.status_code, 429)  # 第三次被限流
        self.assertEqual(r3.json()["code"], 429)
        self.assertFalse(r3.json()["ok"])
        self.assertTrue(r3.has_header("Retry-After"))

    def test_regular_api_scope_limited_independently(self):
        with patch.dict(os.environ, {"RATELIMIT_API_PER_MINUTE": "2", "RATELIMIT_AI_PER_MINUTE": "100"}):
            self._post_ai()  # ai scope 计数,不影响 api scope
            ok = self.client.get("/api/imagery/scenes/")
            self.assertEqual(ok.status_code, 200)
            self.client.get("/api/imagery/scenes/")
            blocked = self.client.get("/api/imagery/scenes/")
        self.assertEqual(blocked.status_code, 429)

    def test_health_endpoint_exempt(self):
        with patch.dict(os.environ, {"RATELIMIT_API_PER_MINUTE": "1"}):
            responses = [self.client.get("/api/system/health/") for _ in range(3)]
        self.assertTrue(all(r.status_code == 200 for r in responses))

    def test_disabled_flag_turns_limiter_off(self):
        with patch.dict(os.environ, {"RATELIMIT_DISABLED": "1", "RATELIMIT_AI_PER_MINUTE": "1"}):
            statuses = [self._post_ai().status_code for _ in range(3)]
        self.assertNotIn(429, statuses)

    def test_zero_limit_disables_scope(self):
        with patch.dict(os.environ, {"RATELIMIT_AI_PER_MINUTE": "0"}):
            statuses = [self._post_ai().status_code for _ in range(3)]
        self.assertNotIn(429, statuses)

    def test_agent_status_get_does_not_consume_ai_quota(self):
        self.client.get("/api/agent/sessions/")
        session = AgentSession.objects.create(
            goal="轮询限流测试", status=AgentSession.STATUS_RUNNING,
            owner_session_key=self.client.session.session_key,
        )
        with patch.dict(os.environ, {"RATELIMIT_AI_PER_MINUTE": "1", "RATELIMIT_API_PER_MINUTE": "100"}):
            responses = [self.client.get(f"/api/agent/sessions/{session.id}/") for _ in range(8)]
        self.assertTrue(all(response.status_code == 200 for response in responses))

    def test_agent_session_list_invalid_limit_falls_back_safely(self):
        response = self.client.get("/api/agent/sessions/?limit=not-a-number")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 200)

    def test_database_bucket_hashes_client_ip(self):
        with patch.dict(os.environ, {"RATELIMIT_API_PER_MINUTE": "5", "RATELIMIT_BACKEND": "database"}):
            response = self.client.get("/api/imagery/scenes/", REMOTE_ADDR="203.0.113.42")
        self.assertEqual(response.status_code, 200)
        bucket = ApiRateLimitBucket.objects.get()
        self.assertNotIn("203.0.113.42", bucket.bucket_key)
        self.assertEqual(bucket.capacity, 5)

    def test_database_failure_falls_back_without_failing_api(self):
        with patch.dict(os.environ, {"RATELIMIT_API_PER_MINUTE": "1", "RATELIMIT_BACKEND": "database"}), \
             patch("map_api.middleware._database_limit", side_effect=DatabaseError("simulated unavailable")):
            first = self.client.get("/api/imagery/scenes/", REMOTE_ADDR="198.51.100.10")
            second = self.client.get("/api/imagery/scenes/", REMOTE_ADDR="198.51.100.10")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_database_token_bucket_refills_after_elapsed_time(self):
        allowed, _ = consume_rate_limit("api", "192.0.2.10", 1)
        blocked, retry_after = consume_rate_limit("api", "192.0.2.10", 1)
        self.assertTrue(allowed)
        self.assertFalse(blocked)
        self.assertGreaterEqual(retry_after, 1)
        ApiRateLimitBucket.objects.update(
            tokens=0,
            last_refill_at=timezone.now() - timedelta(seconds=61),
        )
        refilled, _ = consume_rate_limit("api", "192.0.2.10", 1)
        self.assertTrue(refilled)


class CoordinateChainEndToEndTests(SimpleTestCase):
    """R6:主动感知坐标链端到端数值验证。

    链路:stage1 缩略图(1024px)→ 模型 bbox ×ap_scale → 原图坐标 → cut_image_geom 裁剪
    → 模型在裁剪图再给 bbox → map_bbox_to_original 回溯 → pixel_bbox_to_geo 经纬度。
    用合成影像 + 手算精确值验证每一级换算,任何一级缩放系数写错都会挂。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def _make_image(self, w, h, name="src.jpg"):
        path = os.path.join(self.dir, name)
        Image.new("RGB", (w, h), (40, 90, 140)).save(path, "JPEG")
        return path

    def test_two_level_zoom_round_trip_to_geo(self):
        # 原图 2000×1500,地理范围 108.0–108.4 E / 22.5–22.8 N;
        # 目标像素 (1250,1000) 精确对应 (108.25E, 22.6N)。
        orig = self._make_image(2000, 1500)
        geo_bbox = {"min_lng": 108.0, "max_lng": 108.4, "min_lat": 22.5, "max_lat": 22.8}

        # ── 第 1 级:stage1 缩略图 1024×768,模型定位框 [600,470,680,554] ──
        stage1 = resize_image(orig, max_size=1024)
        with Image.open(stage1) as im:
            self.assertEqual(im.size, (1024, 768))
        self.assertEqual(_stage1_scale(stage1, 2000), 2000 / 1024)
        model_text = '<think>目标在影像右中部 [{"bbox_2d": [600, 470, 680, 554], "label": "可疑设施"}]</think>'
        boxes = extract_bbox_from_response(model_text, scale_factor=2000 / 1024)
        self.assertEqual(boxes, [[1171, 917, 1328, 1082]])

        # ── 裁剪(小于 512 自动扩到 512)──
        crop_path, orig_box, saved_w, saved_h = cut_image_geom(orig, boxes[0])
        self.assertIsNotNone(crop_path)
        self.addCleanup(lambda: os.path.exists(crop_path) and os.remove(crop_path))
        self.assertEqual(orig_box, (993, 743, 1505, 1255))
        self.assertEqual((saved_w, saved_h), (512, 512))

        # ── 第 2 级:模型在 512×512 裁剪图上给 [200,200,314,314] → 回溯原图 ──
        orig_bbox = map_bbox_to_original([200, 200, 314, 314], orig_box, saved_w, saved_h)
        self.assertEqual(orig_bbox, [1193, 943, 1307, 1057])

        # ── 反算经纬度:中心必须精确回到 (108.25, 22.6) ──
        geo = pixel_bbox_to_geo(orig_bbox, 2000, 1500, geo_bbox)
        self.assertIsNotNone(geo)
        self.assertAlmostEqual(geo[0], 108.25, places=9)
        self.assertAlmostEqual(geo[1], 22.6, places=9)

        # ── GSD 测量闭环:gsd=2.0 → 228m × 228m,target 带经纬度 ──
        note, target = _measure_and_locate(orig_bbox, 2.0, {"orig_w": 2000, "orig_h": 1500}, geo_bbox)
        self.assertEqual(target["width_m"], 228.0)
        self.assertEqual(target["height_m"], 228.0)
        self.assertEqual(target["area_m2"], 51984.0)
        self.assertEqual(target["lng"], 108.25)
        self.assertEqual(target["lat"], 22.6)
        self.assertIn("228.0m × 228.0m", note)

    def test_large_crop_downscale_maps_center_back(self):
        # 裁剪框超过 3584 会缩小落盘;缩小后的坐标必须仍能映射回原图
        orig = self._make_image(4000, 3000, name="big.jpg")
        crop_path, orig_box, saved_w, saved_h = cut_image_geom(orig, [100, 100, 3900, 2900])
        self.addCleanup(lambda: os.path.exists(crop_path) and os.remove(crop_path))
        self.assertEqual(orig_box, (100, 100, 3900, 2900))
        self.assertEqual((saved_w, saved_h), (3584, 2640))
        # 裁剪图中心点 [1792,1320] 应映射回原图裁剪框中心 (2000,1500)
        mapped = map_bbox_to_original([1792, 1320, 1792, 1320], orig_box, saved_w, saved_h)
        self.assertAlmostEqual((mapped[0] + mapped[2]) / 2, 2000, delta=1)
        self.assertAlmostEqual((mapped[1] + mapped[3]) / 2, 1500, delta=1)

    def test_no_zoom_intent_yields_empty_bbox(self):
        text = "<think>全局宏观问题,无需放大</think><answer>植被覆盖度约 40%</answer>"
        self.assertEqual(extract_bbox_from_response(text), [])
        self.assertFalse(model_wants_zoom(text))


class NormalizeModelAnswerTests(SimpleTestCase):
    """C2:模型输出归一化的对抗样本——任何格式偏差都必须有确定的兜底行为。"""

    def test_structured_answer(self):
        out = normalize_model_answer("<think>推理</think><answer>结论A</answer>")
        self.assertEqual(out["answer"], "结论A")
        self.assertTrue(out["quality"]["structured_answer"])
        self.assertFalse(out["quality"]["fallback_used"])

    def test_empty_string_falls_back(self):
        out = normalize_model_answer("")
        self.assertIn("模型未返回有效文字结果", out["answer"])
        self.assertTrue(out["quality"]["fallback_used"])

    def test_none_falls_back(self):
        self.assertTrue(normalize_model_answer(None)["quality"]["fallback_used"])

    def test_whitespace_only_falls_back(self):
        self.assertTrue(normalize_model_answer("   \n\t  ")["quality"]["fallback_used"])

    def test_plain_text_kept_but_flagged_fallback(self):
        out = normalize_model_answer("没有标签包裹的裸结论")
        self.assertEqual(out["answer"], "没有标签包裹的裸结论")
        self.assertFalse(out["quality"]["structured_answer"])
        self.assertTrue(out["quality"]["fallback_used"])
        self.assertTrue(out["quality"]["warnings"])

    def test_think_only_takes_post_think_text(self):
        out = normalize_model_answer("<think>推理过程</think>最终结论")
        self.assertEqual(out["answer"], "最终结论")
        self.assertTrue(out["quality"]["fallback_used"])

    def test_multiple_answer_tags_takes_first(self):
        out = normalize_model_answer("<answer>第一</answer><answer>第二</answer>")
        self.assertEqual(out["answer"], "第一")
        self.assertTrue(out["quality"]["structured_answer"])

    def test_answer_with_surrounding_whitespace(self):
        out = normalize_model_answer("  <answer>\n  结论B  \n</answer>  ")
        self.assertEqual(out["answer"], "结论B")


class ReportChainEdgeCaseTests(TestCase):
    """C4:报告链边界——对比模式(无单图)、超长回答、emoji 都必须正常出 docx。"""

    def test_compare_mode_report_without_image(self):
        from docx import Document

        messages = [
            {"role": "user", "content": "对比两个区域的土地利用差异"},
            {"role": "ai", "content": "🛰️ 区域A以建设用地为主 🏙️，区域B以耕地为主 🌾\n\n" + "逐地块对比细节说明。" * 300},
        ]
        r = self.client.post(
            "/api/report/generate/",
            data={
                "file_name": "__compare__",
                "title": "双区域对比报告",
                "messages": messages,
                "spatial_context": "",
                "bbox": {},
            },
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["code"], 200)
        report_name = body["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        self.assertTrue(os.path.exists(report_path))
        doc = Document(report_path)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("🛰️", text)
        self.assertIn("逐地块对比细节说明。", text)
        os.remove(report_path)


class FaultMatrixTests(TestCase):
    """R-1/R7:外部依赖故障矩阵——6 个依赖各自的失败行为必须清晰、不静默挂起。"""

    def setUp(self):
        reset_rate_limit_state()

    # 1. Mapbox:持续 429 → 重试耗尽后抛清晰异常(不静默卡死)
    def test_mapbox_429_retry_exhaustion_raises(self):
        from map_api.utils.get_satellite_image import _fetch_tile

        fake_session = MagicMock()
        fake_session.get.return_value = SimpleNamespace(status_code=429, content=b"")
        with patch("map_api.utils.get_satellite_image.requests.Session", return_value=fake_session), \
                patch("map_api.utils.get_satellite_image.time.sleep"):
            with self.assertRaises(Exception) as ctx:
                _fetch_tile("https://api.mapbox.com/tile.png",
                            proxies={"http": None, "https": None}, retries=3)
        self.assertIn("tile fetch failed after 3 attempts", str(ctx.exception))

    # 2. Earth Search:STAC 500 → HTTPError 向上传播(由调用方降级)
    def test_earth_search_stac_500_raises(self):
        provider = EarthSearchProvider()
        fake_resp = MagicMock()
        fake_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
        with patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp):
            with self.assertRaises(requests.exceptions.HTTPError):
                provider.search({"min_lng": 108.0, "min_lat": 22.5, "max_lng": 108.3, "max_lat": 22.8})

    def test_earth_search_429_opens_circuit_without_repeating_request(self):
        from map_api.utils.service_health import ServiceCircuitOpen

        provider = EarthSearchProvider(endpoint="https://rate-limit.test/search", timeout=1)
        fake_resp = MagicMock(status_code=429)
        fake_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("429 Too Many Requests")
        with patch.dict(os.environ, {
            "EARTH_SEARCH_RETRIES": "0",
            "EARTH_SEARCH_CIRCUIT_FAILURES": "1",
            "EARTH_SEARCH_CIRCUIT_SECONDS": "60",
        }, clear=False), patch(
            "map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp
        ) as post_mock:
            with self.assertRaises(requests.exceptions.HTTPError):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
            with self.assertRaises(ServiceCircuitOpen):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertEqual(post_mock.call_count, 1)

    def test_earth_search_timeout_has_bounded_retry(self):
        provider = EarthSearchProvider(timeout=1)
        with patch.dict(os.environ, {"EARTH_SEARCH_RETRIES": "2"}), \
                patch("map_api.imagery_sources.earth_search.requests.post", side_effect=requests.exceptions.Timeout("read timeout")) as post_mock, \
                patch("map_api.imagery_sources.earth_search.time.sleep") as sleep_mock:
            with self.assertRaises(requests.exceptions.Timeout):
                provider.search({"min_lng": 108.0, "min_lat": 22.5, "max_lng": 108.3, "max_lat": 22.8})
        self.assertEqual(post_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_external_requests_honor_process_proxy_by_default(self):
        provider = EarthSearchProvider()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"features": []}
        with patch.dict(os.environ, {"SATELLITESENSE_DIRECT_HTTP": ""}, clear=False), \
                patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp) as post_mock:
            provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertIsNone(post_mock.call_args.kwargs["proxies"])

    def test_external_requests_can_be_forced_direct(self):
        provider = EarthSearchProvider()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"features": []}
        with patch.dict(os.environ, {"SATELLITESENSE_DIRECT_HTTP": "1"}, clear=False), \
                patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp) as post_mock:
            provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertEqual(post_mock.call_args.kwargs["proxies"], {"http": None, "https": None})

    # 3. TiTiler:返回非图片 → ValueError(触发候选回退链)
    def test_titiler_non_image_response_raises(self):
        provider = EarthSearchProvider()
        candidate = SimpleNamespace(assets={"visual": {"href": "https://example.com/cog.tif"}})
        fake_resp = MagicMock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.headers = {"content-type": "application/json"}
        with patch("map_api.imagery_sources.earth_search.requests.get", return_value=fake_resp):
            with self.assertRaises(ValueError) as ctx:
                provider.render_candidate_jpeg(
                    candidate, {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}, 256, 256)
        self.assertIn("未返回图片", str(ctx.exception))

    def test_titiler_timeout_has_bounded_retry(self):
        provider = EarthSearchProvider(timeout=1)
        candidate = SimpleNamespace(assets={"visual": {"href": "https://example.com/cog.tif"}})
        with patch.dict(os.environ, {"TITILER_RETRIES": "1"}), \
                patch("map_api.imagery_sources.earth_search.requests.get", side_effect=requests.exceptions.Timeout("read timeout")) as get_mock, \
                patch("map_api.imagery_sources.earth_search.time.sleep") as sleep_mock:
            with self.assertRaises(requests.exceptions.Timeout):
                provider.render_candidate_jpeg(
                    candidate, {"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}, 256, 256)
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_earth_search_circuit_opens_after_repeated_network_failures(self):
        from map_api.utils.service_health import ServiceCircuitOpen

        provider = EarthSearchProvider(endpoint="https://circuit.test/search", timeout=1)
        with patch.dict(os.environ, {
            "EARTH_SEARCH_RETRIES": "0",
            "EARTH_SEARCH_CIRCUIT_FAILURES": "1",
            "EARTH_SEARCH_CIRCUIT_SECONDS": "60",
        }, clear=False), \
                patch("map_api.imagery_sources.earth_search.requests.post", side_effect=requests.exceptions.Timeout("timeout")) as post_mock:
            with self.assertRaises(requests.exceptions.Timeout):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
            with self.assertRaises(ServiceCircuitOpen):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertEqual(post_mock.call_count, 1)
        row = ExternalServiceHealth.objects.get(service_key__startswith="earth-search:")
        self.assertEqual(row.failure_count, 1)
        self.assertIsNotNone(row.open_until)

    def test_earth_search_success_closes_previous_circuit(self):
        from map_api.utils.service_health import service_key

        provider = EarthSearchProvider(endpoint="https://circuit-recover.test/search", timeout=1)
        key = service_key("earth-search", provider.endpoint)
        ExternalServiceHealth.objects.create(
            service_key=key, failure_count=3,
            open_until=timezone.now() - timedelta(seconds=1), last_error="old failure",
        )
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"features": []}
        with patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp) as post_mock:
            self.assertEqual(provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4}), [])
        post_mock.assert_called_once()
        row = ExternalServiceHealth.objects.get(service_key=key)
        self.assertEqual(row.failure_count, 0)
        self.assertIsNone(row.open_until)

    def test_earth_search_invalid_json_counts_as_service_failure(self):
        provider = EarthSearchProvider(endpoint="https://invalid-json.test/search", timeout=1)
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.side_effect = ValueError("html")
        with patch.dict(os.environ, {"EARTH_SEARCH_RETRIES": "0", "EARTH_SEARCH_CIRCUIT_FAILURES": "1"}, clear=False), \
                patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp):
            with self.assertRaisesRegex(ValueError, "无效 JSON"):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        row = ExternalServiceHealth.objects.get(service_key__startswith="earth-search:")
        self.assertEqual(row.failure_count, 1)

    def test_earth_search_invalid_stac_shape_counts_as_service_failure(self):
        provider = EarthSearchProvider(endpoint="https://invalid-shape.test/search", timeout=1)
        fake_resp = MagicMock(status_code=200)
        fake_resp.json.return_value = {"features": {"not": "a-list"}}
        with patch.dict(os.environ, {"EARTH_SEARCH_RETRIES": "0", "EARTH_SEARCH_CIRCUIT_FAILURES": "1"}, clear=False), \
                patch("map_api.imagery_sources.earth_search.requests.post", return_value=fake_resp):
            with self.assertRaisesRegex(ValueError, "无效 STAC 响应结构"):
                provider.search({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        row = ExternalServiceHealth.objects.get(service_key__startswith="earth-search:")
        self.assertEqual(row.failure_count, 1)

    def test_service_failure_persists_diagnostic_fields(self):
        from map_api.utils.service_health import record_failure, service_key

        key = service_key("diagnostic", "https://provider.test")
        error = requests.exceptions.Timeout("upstream timeout")
        error.error_type = "timeout"
        record_failure(key, error, threshold=1, cooldown_seconds=45)

        row = ExternalServiceHealth.objects.get(service_key=key)
        self.assertEqual(row.last_error, "upstream timeout")
        self.assertEqual(row.last_error_type, "timeout")
        self.assertIsNotNone(row.last_failure_at)
        self.assertIsNotNone(row.open_until)

    # 4. DashScope:配额耗尽/非 200 → 500 JSON 带失败原因(不把异常吞成空回答)
    def test_dashscope_quota_error_returns_500_json(self):
        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        img_path = os.path.join(save_dir, "sat_fm_quota.jpg")
        Image.new("RGB", (64, 64), (80, 120, 160)).save(img_path, "JPEG")
        self.addCleanup(lambda: os.path.exists(img_path) and os.remove(img_path))

        fake_resp = SimpleNamespace(status_code=429, message="Throttling.RateQuota")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False), \
                patch("map_api.views._call_qwen", return_value=fake_resp):
            r = self.client.post(
                "/api/ai/query-region/",
                data={"file_name": "sat_fm_quota.jpg", "question": "这张图里有什么", "active_perception": False},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("AI 调用失败", r.json()["msg"])
        self.assertIn("Throttling", r.json()["msg"])

    # 5. DeepSeek:返回非 JSON → ValueError 带清晰消息(上层有规则兜底)
    def test_deepseek_malformed_json_raises_valueerror(self):
        from map_api.utils.agent_tools import call_deepseek_json

        with patch("map_api.utils.agent_tools.call_deepseek", return_value="这不是 JSON {"):
            with self.assertRaises(ValueError) as ctx:
                call_deepseek_json([{"role": "user", "content": "解析槽位"}])
        self.assertIn("GLM 未返回有效 JSON", str(ctx.exception))

    def test_deepseek_retries_transient_server_error_once(self):
        from map_api.utils.agent_tools import call_deepseek
        first = SimpleNamespace(status_code=503, raise_for_status=MagicMock())
        second = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "ok"}}]})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.utils.agent_tools.requests.post", side_effect=[first, second]) as post_mock, \
                patch("map_api.utils.agent_tools.time.sleep"):
            result = call_deepseek([{"role": "user", "content": "test"}], timeout=1)
        self.assertEqual(result, "ok")
        self.assertEqual(post_mock.call_count, 2)

    def test_deepseek_does_not_retry_auth_error(self):
        from map_api.utils.agent_tools import call_deepseek
        response = SimpleNamespace(status_code=401)
        error = requests.HTTPError("401", response=response)
        response.raise_for_status = MagicMock(side_effect=error)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False), \
                patch("map_api.utils.agent_tools.requests.post", return_value=response) as post_mock:
            with self.assertRaises(requests.HTTPError):
                call_deepseek([{"role": "user", "content": "test"}], timeout=1)
        self.assertEqual(post_mock.call_count, 1)

    def test_glm_multimodal_request_uses_image_url_block(self):
        from map_api.utils.agent_tools import call_glm_json
        captured = {}
        response = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": '{"current_step":"quality_check"}'}}]},
        )
        def fake_post(url, **kwargs):
            captured.update(kwargs)
            return response
        with patch.dict(os.environ, {"GLM_API_KEY": "test-key", "AGENT_MODEL": "glm-5.3-flash"}, clear=False), \
                patch("map_api.utils.agent_tools.requests.post", side_effect=fake_post):
            result = call_glm_json([{"role": "user", "content": "检查影像"}], image_urls=["data:image/jpeg;base64,abc"])
        self.assertEqual(result["current_step"], "quality_check")
        body = captured["json"]
        self.assertEqual(body["model"], "glm-5.3-flash")
        content = body["messages"][0]["content"]
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertEqual(content[-1]["image_url"]["url"], "data:image/jpeg;base64,abc")

    def test_glm_content_blocks_extract_public_text_only(self):
        from map_api.utils.agent_tools import call_deepseek
        response = SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {
                "reasoning_content": "private reasoning",
                "content": [{"type": "text", "text": '{"ok":true}'}, {"type": "thinking", "text": "hidden"}],
            }}]},
        )
        with patch.dict(os.environ, {"GLM_API_KEY": "test-key"}, clear=False), \
                patch("map_api.utils.agent_tools.requests.post", return_value=response):
            result = call_deepseek([{"role": "user", "content": "test"}])
        self.assertEqual(result, '{"ok":true}')
        self.assertNotIn("private", result)
        self.assertNotIn("hidden", result)

    def test_agent_step_rejects_empty_glm_decision(self):
        from map_api.agent.loop import agent_step
        with patch("map_api.utils.agent_tools.call_glm_json", return_value={}):
            with self.assertRaisesRegex(ValueError, "GLM 返回空决策"):
                agent_step([{"role": "user", "content": "继续"}], [], {"vision_trigger": ""})

    def test_complete_preserves_geocode_root_cause_when_scene_missing(self):
        from map_api.agent.loop import _complete
        session = AgentSession.objects.create(goal="调查不存在区域", mode="fast", status=AgentSession.STATUS_RUNNING, artifacts={})
        _complete(session, {"slots": {"place_name": "不存在区域"}, "file_name": None}, None,
                  [{"role": "tool_result", "content": '{"status":"error","message":"未找到行政区：不存在区域"}'}])
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_FAILED)
        self.assertIn("行政区无法解析", session.error)
        self.assertIn("未找到行政区", session.error)

    def test_resume_step_for_expand_dates_uses_valid_retrieve_step(self):
        from map_api.agent.waiting import resume_step_for
        self.assertEqual(resume_step_for("expand_dates"), "retrieve_imagery")

    def test_retry_step_directive_names_failed_tool(self):
        from map_api.agent.loop import resume_waiting_agent_session
        session = AgentSession.objects.create(
            goal="重试测试", mode="fast", status=AgentSession.STATUS_WAITING_USER,
            artifacts={"waiting": {"data": {"failed_tool": "search_sentinel_imagery"}, "options": []}, "tool_history": []},
            slots={}, messages=[],
        )
        with patch("map_api.agent.loop._views._run_agent_background") as bg_mock, \
                patch("map_api.agent.loop._views._agent_set_observer"):
            resume_waiting_agent_session(session, "retry_step")
        context = bg_mock.call_args.args[1]
        history = context.get("tool_history") or []
        self.assertTrue(history)
        self.assertIn("search_sentinel_imagery", history[-1]["content"])

    # 6a. 高德:缺 key → 立即失败,不发网络请求
    def test_amap_missing_key_fails_fast(self):
        from map_api.utils.agent_tools import resolve_district_bbox

        with patch.dict(os.environ, {"AMAP_KEY": ""}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                resolve_district_bbox("南宁市")
        self.assertIn("AMAP_KEY", str(ctx.exception))

    # 6b. 高德:查无此行政区 → 清晰错误
    def test_amap_empty_districts_raises(self):
        from map_api.utils.agent_tools import resolve_district_bbox

        fake_resp = MagicMock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {"districts": []}
        with patch.dict(os.environ, {"AMAP_KEY": "test-amap-key"}, clear=False), \
                patch("map_api.utils.agent_tools.requests.get", return_value=fake_resp):
            with self.assertRaises(ValueError) as ctx:
                resolve_district_bbox("不存在省")
        self.assertIn("未找到行政区", str(ctx.exception))

    # 7. RemoteCLIP:打分器异常 → 静默回退颜色启发式(不影响主流程)
    def test_clip_failure_falls_back_to_heuristic(self):
        from map_api.utils.smart_query_analyzer import rank_tiles

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, color in enumerate([(20, 120, 30), (140, 140, 140), (30, 60, 160)]):
                p = os.path.join(tmp, f"tile_{i}.jpg")
                Image.new("RGB", (64, 64), color).save(p, "JPEG")
                paths.append(p)
            with patch("map_api.utils.clip_retriever.score_tiles", side_effect=RuntimeError("torch 不可用")):
                selected, analysis = rank_tiles(paths, "识别植被覆盖的农田区域", tile_cols=3, tile_rows=1)
        self.assertEqual(analysis["_ranker"], "heuristic")
        self.assertTrue(selected)
        self.assertTrue(all(p in paths for p in selected))


class BackgroundTaskFaultTests(TransactionTestCase):
    """R-3:后台线程异常必须落库为 error 状态,不能让任务永远 downloading。

    用 TransactionTestCase 而非 TestCase:后台线程走独立数据库连接,
    TestCase 的未提交事务对它不可见,update 会静默落空。
    patch 必须覆盖整个等待期——线程在 POST 返回后才真正执行 fetch。
    """

    def test_download_exception_marks_task_error(self):
        with patch.dict(os.environ, {"MAPBOX_TOKEN": "test-token"}, clear=False), \
                patch("map_api.views.fetch_satellite_image", side_effect=RuntimeError("boom-429")):
            r = self.client.post(
                "/api/satellite/get-img/",
                data={"min_lng": 108.0, "min_lat": 22.5, "max_lng": 108.05, "max_lat": 22.55},
                content_type="application/json",
            )
            self.assertEqual(r.status_code, 200)
            file_name = r.json()["data"]["file_name"]

            task = DownloadTask.objects.get(file_name=file_name)
            deadline = time.time() + 5
            while task.status == "downloading" and time.time() < deadline:
                time.sleep(0.05)
                task.refresh_from_db()
        self.assertEqual(task.status, "error")
        self.assertIn("boom-429", task.error_message)


class StaleTaskCleanupTests(TestCase):
    """僵尸下载/Agent 必须释放旧租约并进入可恢复状态。"""

    def _make_task(self, file_name, status="downloading"):
        return DownloadTask.objects.create(
            file_name=file_name,
            status=status,
            min_lng=108.0, min_lat=22.5, max_lng=108.1, max_lat=22.6,
        )

    def test_stale_downloading_task_released_for_worker_recovery(self):
        task = self._make_task("sat_stale.jpg")
        DownloadTask.objects.filter(id=task.id).update(worker_claim="dead-worker", claimed_at=timezone.now())
        # auto_now 字段只能通过 queryset.update 改成旧时间
        DownloadTask.objects.filter(id=task.id).update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10")
        task.refresh_from_db()
        self.assertEqual(task.status, "downloading")
        self.assertEqual(task.worker_claim, "")
        self.assertIsNone(task.claimed_at)
        self.assertIn("worker 接管", task.error_message)

    def test_cleanup_then_worker_reclaims_same_download(self):
        task = self._make_task("sat_cleanup_worker_chain.jpg")
        DownloadTask.objects.filter(id=task.id).update(
            worker_claim="dead-worker", claimed_at=timezone.now() - timedelta(minutes=30),
            updated_at=timezone.now() - timedelta(minutes=30),
        )
        call_command("cleanup_stale_tasks", "--minutes", "10")
        with patch("map_api.management.commands.run_agent_worker.execute_download_task", return_value=True) as execute:
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], task.id)
        task.refresh_from_db()
        self.assertNotEqual(task.worker_claim, "dead-worker")

    def test_recent_downloading_task_untouched(self):
        task = self._make_task("sat_fresh.jpg")
        call_command("cleanup_stale_tasks", "--minutes", "10")
        task.refresh_from_db()
        self.assertEqual(task.status, "downloading")

    def test_done_task_never_touched(self):
        task = self._make_task("sat_done.jpg", status="done")
        DownloadTask.objects.filter(id=task.id).update(
            updated_at=timezone.now() - timedelta(minutes=60)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10")
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_dry_run_changes_nothing(self):
        task = self._make_task("sat_dry.jpg")
        DownloadTask.objects.filter(id=task.id).update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10", "--dry-run")
        task.refresh_from_db()
        self.assertEqual(task.status, "downloading")

    def test_stale_agent_session_is_released_for_worker_recovery_not_failed(self):
        session = AgentSession.objects.create(
            goal="可恢复的僵尸 Agent",
            status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "dead-worker", "worker_claimed_at": "2026-01-01T00:00:00+00:00"},
        )
        AgentSession.objects.filter(id=session.id).update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10")
        session.refresh_from_db()
        self.assertEqual(session.status, AgentSession.STATUS_RUNNING)
        self.assertNotIn("worker_claim", session.artifacts)
        self.assertIn("recovery_pending_at", session.artifacts)
        self.assertIn("worker 接管", session.artifacts["recovery_reason"])

    def test_stale_agent_dry_run_keeps_claim(self):
        session = AgentSession.objects.create(
            goal="Agent dry run",
            status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "still-owned"},
        )
        AgentSession.objects.filter(id=session.id).update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10", "--dry-run")
        session.refresh_from_db()
        self.assertEqual(session.artifacts.get("worker_claim"), "still-owned")

    def test_cleanup_then_worker_recovers_same_agent_session(self):
        session = AgentSession.objects.create(
            goal="清理后 worker 接管链路",
            status=AgentSession.STATUS_RUNNING,
            artifacts={"worker_claim": "dead-worker", "worker_claimed_at": "2026-01-01T00:00:00+00:00"},
        )
        AgentSession.objects.filter(id=session.id).update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        call_command("cleanup_stale_tasks", "--minutes", "10")
        with patch("map_api.management.commands.run_agent_worker.run_agent_session") as run_mock:
            call_command("run_agent_worker", "--once", "--max-sessions", "1")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[0], session.id)
