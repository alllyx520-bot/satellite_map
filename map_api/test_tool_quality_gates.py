import os
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from .agent.tools import _tool_search_sentinel_imagery, _tool_analyze_imagery
from .agent.waiting import WaitingForUser


class ToolQualityGatesTests(SimpleTestCase):
    def setUp(self):
        self.ctx = {"slots": {}, "bbox": {"min_lng": 108, "min_lat": 22, "max_lng": 109, "max_lat": 23}, "force_continue": True}

    def test_legacy_environment_flag_cannot_switch_source_after_provider_failure(self):
        with patch.dict(os.environ, {"AGENT_AUTO_SOURCE_FALLBACK": "1"}), \
             patch("map_api.views._agent_fetch_sentinel", side_effect=requests.Timeout), \
             patch("map_api.views._agent_fetch_mapbox") as mapbox:
            with self.assertRaises(WaitingForUser):
                _tool_search_sentinel_imagery(self.ctx, {})
        mapbox.assert_not_called()
        self.assertNotEqual(self.ctx["slots"].get("source"), "mapbox")

    def test_empty_sentinel_result_cannot_trigger_mapbox(self):
        with patch.dict(os.environ, {"AGENT_AUTO_SOURCE_FALLBACK": "1"}), \
             patch("map_api.views._agent_fetch_sentinel", return_value=(None, None, {})), \
             patch("map_api.views._agent_fetch_mapbox") as mapbox:
            with self.assertRaises(WaitingForUser):
                _tool_search_sentinel_imagery(self.ctx, {})
        mapbox.assert_not_called()

    def test_force_continue_does_not_bypass_cloud_or_date_gate(self):
        scene = SimpleNamespace(id=1, file_name="image.jpg", cloud_percent=45, metadata={})
        for date_result in [(False, "日期不满足"), (True, "")]:
            with self.subTest(date_result=date_result), \
                 patch("map_api.views._agent_fetch_sentinel", return_value=(scene, None, {})), \
                 patch("map_api.views._scene_matches_requested_dates", return_value=date_result), \
                 patch("map_api.agent.tools.scene_brief_payload", return_value={}):
                with self.assertRaises(WaitingForUser) as caught:
                    _tool_search_sentinel_imagery(self.ctx, {})
                self.assertNotIn("continue", [item["code"] for item in caught.exception.options])

    def test_cross_date_retrieval_is_rejected_before_attaching_scene(self):
        scene = SimpleNamespace(metadata={"temporal_consistency": "mixed_dates"})
        with patch("map_api.views._agent_fetch_sentinel", return_value=(scene, None, {})):
            with self.assertRaises(WaitingForUser):
                _tool_search_sentinel_imagery(self.ctx,{})
        self.assertNotIn("scene_id", self.ctx)

    def test_force_continue_does_not_bypass_structured_vision_gate(self):
        from django.http import JsonResponse
        self.ctx.update({"file_name": "image.jpg", "goal": "检查道路"})
        response = JsonResponse({"code": 200, "data": {
            "answer": "unstructured", "analysis_method": {"output_quality": {"fallback_used": True}},
        }})
        with patch("map_api.agent.tools._resolve_scene", return_value=None), \
             patch("map_api.views.run_vl_analysis", return_value=response):
            with self.assertRaises(WaitingForUser):
                _tool_analyze_imagery(self.ctx, {})
