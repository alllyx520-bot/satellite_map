"""核心纯函数单元测试。

只覆盖不依赖数据库/网络/外部 API 的纯逻辑函数,可直接运行:
    python manage.py test map_api
"""
from django.test import SimpleTestCase, TestCase
from django.conf import settings
import os

from map_api.utils.smart_query_analyzer import analyze_query, adaptive_resolution, _build_clip_query
from map_api.utils.active_perception import (
    extract_bbox_from_response, extract_answer_text, measure_bbox, pixel_bbox_to_geo,
    map_bbox_to_original
)
from map_api.utils.get_satellite_image import haversine_distance
from map_api.models import ChatHistory, DownloadTask, ImageryScene
from map_api.imagery_sources.mapbox import MapboxProvider
from map_api.views import ANALYSIS_MODES, compute_image_plan, normalize_bbox, _download_progress


class AnalyzeQueryTests(SimpleTestCase):
    def test_detail_question_suggests_stages(self):
        r = analyze_query("数一下这个停车场里有多少辆车")
        self.assertTrue(r["is_detail"])
        self.assertTrue(r["suggest_stages"])

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


class ImageryMetadataTests(SimpleTestCase):
    def test_mapbox_provider_marks_scene_as_reference(self):
        meta = MapboxProvider().metadata_for_bbox({"min_lng": 1, "min_lat": 2, "max_lng": 3, "max_lat": 4})
        self.assertEqual(meta.source, "mapbox")
        self.assertEqual(meta.decision_grade, "reference")
        self.assertIn("不应作为单独行政决策", meta.limitations)


class HistoryApiTests(TestCase):
    def test_rejects_compare_history(self):
        r = self.client.post(
            "/api/ai/history/",
            data={"image_file": "__compare__", "messages": []},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 400)

    def test_upserts_history_by_image_file(self):
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
        r = self.client.post("/api/satellite/cleanup/", data={"days": 0}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("sat_old.jpg", _download_progress)


class ImagerySceneApiTests(TestCase):
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


class ReportSceneTests(TestCase):
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
        os.remove(img_path)
        os.remove(report_path)
