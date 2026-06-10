"""核心纯函数单元测试。

只覆盖不依赖数据库/网络/外部 API 的纯逻辑函数,可直接运行:
    python manage.py test map_api
"""
from django.test import Client, SimpleTestCase, TestCase
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
import os
import json
import tempfile
from http import HTTPStatus
from io import BytesIO, StringIO
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import requests
from PIL import Image

from map_api.utils.smart_query_analyzer import analyze_query, adaptive_resolution, _build_clip_query
from map_api.utils.agent_tools import (
    build_agent_plan, compute_ndwi_from_arrays, deterministic_extract_slots,
    merge_agent_slots, parse_amap_boundary
)
from map_api.utils.active_perception import (
    extract_bbox_from_response, extract_answer_text, measure_bbox, pixel_bbox_to_geo,
    map_bbox_to_original
)
from map_api.utils.get_satellite_image import fetch_satellite_image, haversine_distance
from map_api.utils.analysis_strategy import build_analysis_strategy
from map_api.models import AgentSession, ChatHistory, DownloadTask, ImageryScene
from map_api.imagery_sources.mapbox import MapboxProvider
from map_api.imagery_sources.earth_search import EarthSearchProvider, score_candidate, stac_datetime_range
from map_api.views import (
    ANALYSIS_MODES, compute_image_plan, normalize_bbox,
    imagery_quality_payload, analysis_confidence_payload, source_recommendation_payload,
    run_agent_session, select_best_sentinel_candidate, bbox_intersection_ratio,
    bbox_union_coverage_ratio, compose_sentinel_mosaic, image_valid_ratio,
    crop_sentinel_nodata_border, image_plan_for_bbox_and_size,
    select_sentinel_scene_candidates, sentinel_retrieval_result, _download_progress
)
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

        with patch("map_api.views.find_cached_sentinel_scene", return_value=None):
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

    def test_ndwi_from_arrays(self):
        green = np.array([[0.8, 0.2], [0.7, 0.1]], dtype=np.float32)
        nir = np.array([[0.1, 0.5], [0.2, 0.6]], dtype=np.float32)
        out = compute_ndwi_from_arrays(green, nir, threshold=0.1, min_valid_pixels=1)
        self.assertTrue(out["available"])
        self.assertEqual(out["water_percent"], 50.0)
        self.assertEqual(out["method"], "NDWI=(Green-NIR)/(Green+NIR)")

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
    def test_health_reports_core_configuration_without_secret_values(self):
        with patch.dict(os.environ, {
            "MAPBOX_TOKEN": "secret-mapbox-token",
            "DASHSCOPE_API_KEY": "secret-dashscope-key",
            "DEEPSEEK_API_KEY": "secret-deepseek-key",
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
        self.assertEqual(data["agent"]["controller_model"], "deepseek-v4-flash")
        self.assertTrue(data["agent"]["available"])

        raw = json.dumps(r.json(), ensure_ascii=False)
        self.assertNotIn("secret-mapbox-token", raw)
        self.assertNotIn("secret-dashscope-key", raw)
        self.assertNotIn("secret-deepseek-key", raw)
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
                    "question": "数一下这里的建筑细节",
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
                    "question": "数一下这里的建筑细节",
                    "mode": "precise",
                    "history": [],
                },
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["active_stages"], 2)
        self.assertEqual(data["answer"], "局部细节分析结论\n\n📐 **定量信息**（基于 GSD 测算）：尺寸约 16.0m × 16.0m · 占地约 256 m² · 中心约 22.5°N, 11.5°E")
        self.assertEqual(data["targets"][0]["width_m"], 16.0)
        self.assertEqual(data["targets"][0]["height_m"], 16.0)
        self.assertEqual(data["targets"][0]["area_m2"], 256.0)
        self.assertEqual(data["targets"][0]["lat"], 22.5)
        self.assertEqual(data["targets"][0]["lng"], 11.5)


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
        self.assertEqual(r.status_code, 200)
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


class AgentSessionApiTests(TestCase):
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

    def _agent_patches(self, candidate=None, cloud=8.5):
        candidate = candidate or self._candidate(cloud)
        return (
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
            patch("map_api.views.call_deepseek", return_value="复核结论：南宁市 2026 年 4 月 bbox 范围内可见河流、湖库和坑塘水体，NDWI 显示可能水体约 18.5%，该比例仅作筛查。"),
        )

    def _agent_base_patches(self):
        return (
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
            patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                status_code=HTTPStatus.OK,
                output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": "<answer>水体主要分布在河道和坑塘。</answer>"}]))]),
                message="",
            )),
            patch("map_api.views.call_deepseek", return_value="复核结论：南宁市 2026 年 4 月 bbox 范围内可见河流、湖库和坑塘水体，NDWI 显示可能水体约 18.5%，该比例仅作筛查。"),
        )

    def test_agent_session_requires_deepseek_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况"},
                content_type="application/json",
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("DEEPSEEK_API_KEY", r.json()["msg"])

    def test_mocked_agent_full_loop_completes(self):
        patches = self._agent_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

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
        image_file = data["artifacts"]["file_name"]
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", image_file)
        if os.path.exists(img_path):
            os.remove(img_path)

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
        left_buf = BytesIO()
        right_buf = BytesIO()
        left_img.save(left_buf, "JPEG")
        right_img.save(right_buf, "JPEG")

        base = self._agent_base_patches()
        with base[0], base[1], base[2], base[3], base[4], \
                patch("map_api.views.EarthSearchProvider.search", return_value=[left, right]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", side_effect=[left_buf.getvalue(), right_buf.getvalue()]), \
                patch("map_api.views.compute_ndwi_mosaic_summary", return_value={
                    "available": True,
                    "method": "NDWI=(Green-NIR)/(Green+NIR)",
                    "aggregation": "按每景有效像元数加权汇总",
                    "water_percent": 21.0,
                    "water_ratio": 0.21,
                    "sample_size_px": 4096,
                    "limitations": "多景 NDWI 为 bbox 筛查级加权结果。",
                }) as ndwi_mock:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "completed")
        scene = ImageryScene.objects.get(id=data["scene_id"])
        self.assertTrue(scene.metadata["mosaic"])
        self.assertEqual(scene.metadata["mosaic_candidate_count"], 2)
        self.assertGreaterEqual(scene.metadata["target_coverage_ratio"], 0.99)
        self.assertGreaterEqual(scene.metadata["valid_image_ratio"], 0.95)
        self.assertEqual(data["artifacts"]["ndwi"]["water_percent"], 21.0)
        ndwi_mock.assert_called_once()
        img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", data["artifacts"]["file_name"])
        if os.path.exists(img_path):
            os.remove(img_path)

    def test_agent_waits_when_sentinel_has_no_candidate(self):
        patches = self._agent_patches()
        with patches[0], patches[1], patches[2], patch("map_api.views.EarthSearchProvider.search", return_value=[]):
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "waiting_user")
        self.assertIn("扩大时间范围", data["artifacts"]["waiting"]["options"])
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertEqual(data["observer"]["current_status"], "waiting_user")
        retrieve_step = [s for s in data["observer"]["plan_steps"] if s["id"] == "retrieve_imagery"][0]
        self.assertEqual(retrieve_step["status"], "waiting_user")

    def test_agent_waits_when_sentinel_provider_errors(self):
        patches = self._agent_patches()
        with patches[0], patches[1], patches[2], patch("map_api.views.EarthSearchProvider.search", side_effect=requests.HTTPError("bad request")):
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertIn("切换高清底图", data["artifacts"]["waiting"]["options"])

    def test_agent_waits_when_sentinel_candidate_coverage_is_too_low(self):
        low_coverage = EarthSearchProvider().candidate_from_item({
            "id": "S2A_LOW_COVERAGE",
            "collection": "sentinel-2-l2a",
            "bbox": [108.1, 22.1, 108.2, 22.2],
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
        patches = self._agent_patches(candidate=low_coverage)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "retrieve_imagery")
        self.assertIn("Sentinel-2 候选覆盖不足", data["artifacts"]["waiting"]["message"])

    def test_agent_waits_on_high_cloud(self):
        patches = self._agent_patches(cloud=45)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "waiting_user")
        self.assertIn("云量", data["artifacts"]["waiting"]["message"])
        self.assertEqual(data["observer"]["current_step"], "quality_check")
        self.assertEqual(data["observer"]["current_status"], "waiting_user")
        self.assertEqual(data["observer"]["next"], "等待用户确认")
        image_file = data.get("artifacts", {}).get("file_name")
        if image_file:
            img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", image_file)
            if os.path.exists(img_path):
                os.remove(img_path)

    def test_agent_waits_when_vision_output_is_unstable(self):
        patches = self._agent_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                    status_code=HTTPStatus.OK,
                    output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": ""}]))]),
                    message="",
                )), patches[7]:
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )

        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data["status"], "waiting_user")
        self.assertEqual(data["observer"]["current_step"], "vl_analysis")
        self.assertIn("快速模式重试", data["artifacts"]["waiting"]["options"])
        image_file = data.get("artifacts", {}).get("file_name")
        if image_file:
            img_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", image_file)
            if os.path.exists(img_path):
                os.remove(img_path)

    def test_agent_cancel_updates_observer(self):
        patches = self._agent_patches()
        with patches[0], patches[1], patches[2], patch("map_api.views.EarthSearchProvider.search", return_value=[]):
            r = self.client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )
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

    def test_agent_uses_current_scene_context(self):
        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        image_file = "agent_current_scene.jpg"
        Image.new("RGB", (64, 64), (80, 120, 160)).save(os.path.join(save_dir, image_file), "JPEG")
        scene = ImageryScene.objects.create(
            file_name=image_file,
            source="mapbox",
            source_label="Mapbox Satellite Basemap",
            min_lng=1,
            min_lat=2,
            max_lng=3,
            max_lat=4,
            gsd_m=1.5,
            area_km2=10,
        )
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-deepseek-key",
            "DASHSCOPE_API_KEY": "test-dashscope-key",
            "AMAP_KEY": "test-amap-key",
        }, clear=False), \
                patch("map_api.utils.agent_tools.call_deepseek_json", return_value={"task": "built_up", "source": "mapbox"}), \
                patch("map_api.views._call_qwen", return_value=SimpleNamespace(
                    status_code=HTTPStatus.OK,
                    output=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=[{"text": "<answer>当前区域以建设用地为主。</answer>"}]))]),
                    message="",
                )), \
                patch("map_api.views.call_deepseek", return_value="复核结论：当前区域以建设用地为主。"):
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
