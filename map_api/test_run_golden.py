import json
import os
import tempfile
from io import BytesIO
from unittest.mock import Mock, patch

import numpy as np
import tifffile
from django.test import Client, TestCase, override_settings

from .agent.providers import GLMProvider
from .models import AgentRun, AgentSession, AnalysisRun
from .run_executor import execute_run


class RunGoldenTests(TestCase):
    bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        setting = override_settings(MEDIA_ROOT=self.temp.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.task = "water"
        self.indices = ["ndwi"]
        self.two_dates = False
        self.bad_scl = False
        self.fail_band = None
        self.corrupt_model = False
        self.calls = []
        # 夹具执行器是 GLMProvider;run 记录的 provider 必须与执行遥测一致,否则验收门
        # 报 unexpected_provider_switch(2026-09-11 控制器默认已切 DeepSeek)。
        self._old_provider = os.environ.get("AGENT_PROVIDER")
        os.environ["AGENT_PROVIDER"] = "glm"
        def _restore_provider():
            if self._old_provider is None:
                os.environ.pop("AGENT_PROVIDER", None)
            else:
                os.environ["AGENT_PROVIDER"] = self._old_provider
        self.addCleanup(_restore_provider)

    def response(self, body=None, content=None):
        response = Mock(status_code=200, content=content, headers={"Content-Type": "image/tiff"})
        response.json.return_value = body
        return response

    def model_request(self, url, *, json: dict, **kwargs):
        self.calls.append("model")
        messages = json["messages"]
        operation = messages[0]["content"].split("当前操作是 ")[1].split("。")[0]
        content = messages[-1]["content"]
        context = __import__("json").loads(content if isinstance(content, str) else content[0]["text"])
        if self.corrupt_model:
            output = "prefix {}"
        elif operation == "plan":
            output = __import__("json").dumps({"task": self.task, "source": "sentinel2", "place_name": "测试区",
                "indices": self.indices, "two_dates": self.two_dates, "physical_measurement": False, "area_ratio": True,
                "date_start": "2026-04-10", "date_end": "2026-04-20", "before_date_start": "2026-04-01", "before_date_end": "2026-04-09"})
        else:
            output = __import__("json").dumps({"type": "final", "decision_id": "review", "content": "复核结论：以所列指标和视觉证据为依据。",
                                               "evidence_refs": context["evidence_refs"], "limitations": ["仅统计 AOI 内有效像元；阈值不是地物真值。"]})
        return self.response({"choices": [{"message": {"content": output}}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}})

    def provider(self, **kwargs):
        kwargs.pop("name", None)
        return GLMProvider(endpoint="https://fixture.test/model", headers=lambda: {}, transport=self.model_request, **kwargs)

    def stac_request(self, url, *, json, **kwargs):
        self.calls.append("stac")
        day = "2026-04-05" if json.get("datetime", "").startswith("2026-04-01") else "2026-04-12"
        assets = {band: {"href": f"https://fixture.test/{day}/{band}.tif", "gsd": 20 if band in {"scl", "swir16"} else 10,
                          "raster:bands": [{"scale": 0.0001, "offset": -0.1, "nodata": 0}]}
                  for band in ["visual", "green", "red", "blue", "nir", "scl", "swir16"]}
        return self.response({"features": [{"id": "S2A_" + day, "collection": "sentinel-2-l2a", "bbox": [0, 0, 1, 1],
                    "properties": {"datetime": day + "T03:00:00Z", "eo:cloud_cover": 5}, "assets": assets}]})

    def cog_request(self, url, *, params, **kwargs):
        band = params["url"].split("/")[-1].split(".")[0]
        self.calls.append(band)
        if band == self.fail_band:
            import requests
            raise requests.Timeout()
        # DN 与 sentinel-2-l2a 的 radiometry_override(scale=1e-4, offset=0)配套:
        # 5000→0.5、1000→0.1,保持 NDVI/NDWI/MNDWI 期望值 2/3 不变;fixture 元数据仍写
        # offset=-0.1,恰好同时验证"profile 覆盖优先于资产元数据"。
        value = {"green": 5000, "red": 1000, "blue": 1000, "nir": 1000 if self.task == "water" else 5000, "swir16": 1000,
                 "scl": 9 if self.bad_scl else 6}[band]
        data = np.full((256, 256), value, dtype=np.uint16)
        buf = BytesIO()
        keys = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
        tifffile.imwrite(buf, data, photometric="minisblack", metadata=None,
                         extratags=[(33550, "d", 3, (1/256, 1/256, 0), False), (33922, "d", 6, (0,0,0,0,1,0), False),
                                    (34735, "H", len(keys), keys, False)])
        return self.response(content=buf.getvalue())

    def create(self):
        response = self.client.post("/api/v2/agent/runs/", {"goal": "fixture 遥感调查", "bbox": self.bbox}, content_type="application/json")
        self.assertEqual(response.status_code, 202, response.content)
        return AgentRun.objects.get(pk=response.json()["data"]["id"])

    def execute(self, run, **kwargs):
        with patch("map_api.imagery_sources.earth_search.requests.post", side_effect=self.stac_request), \
             patch("map_api.utils.agent_tools.requests.get", side_effect=self.cog_request):
            execute_run(run.id, "test-worker", provider_factory=self.provider, **kwargs)
        run.refresh_from_db()

    def test_water_http_to_report_uses_actual_geotiff_math_evidence_and_sse(self):
        run = self.create()
        self.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        detail = self.client.get(f"/api/v2/agent/runs/{run.id}/").json()["data"]
        self.assertTrue(detail["acceptance"]["passed"], detail["acceptance"])
        self.assertEqual(detail["acceptance"]["provider_switch_count"], 0)
        metric = run.evidence_v2.get(kind="computed_metric")
        self.assertAlmostEqual(metric.value["mean"], 2/3, places=4)
        self.assertEqual(metric.value["thresholded_percent"], 100)
        self.assertEqual(metric.mask_statistics["valid_pixel_count"], 65536)
        report = run.artifacts_v2.get(kind="report")
        response = self.client.get(report.uri)
        self.assertEqual(response.status_code, 200)
        from docx import Document
        document = Document(BytesIO(response.content))
        self.assertTrue(any(metric.evidence_id in paragraph.text for paragraph in document.paragraphs))
        self.assertEqual(Client().get(report.uri).status_code, 404)
        sse = self.client.get(f"/api/v2/agent/runs/{run.id}/events/stream/", HTTP_LAST_EVENT_ID=str(run.event_sequence - 1))
        self.assertIn(b"run.completed", b"".join(sse.streaming_content))
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertTrue(AnalysisRun.objects.get().findings.get().evidence.exists())
        session = AgentSession.objects.get(artifacts__run_id=run.id)
        self.assertEqual(session.status, "completed")

    def test_vegetation_and_mndwi_golden_paths(self):
        self.task, self.indices = "vegetation", ["ndvi", "mndwi"]
        run = self.create()
        self.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        for metric in run.evidence_v2.filter(kind="computed_metric"):
            self.assertAlmostEqual(metric.value["mean"], 2/3, places=4)
        self.assertEqual(run.evidence_v2.filter(kind="computed_metric").count(), 2)

    def test_two_date_change_has_common_mask_and_independent_searches(self):
        self.two_dates = True
        run = self.create()
        self.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.calls.count("stac"), 2)
        change = run.evidence_v2.get(kind="temporal_change")
        self.assertTrue(change.value["common_mask"])
        self.assertEqual(change.value["changed_pixel_ratio"], 0)
        dates = {scene["acquired_at"][:10] for scene in change.data_contract["scenes"]}
        self.assertEqual(len(dates), 2)

    def test_asset_timeout_and_low_valid_pixels_cannot_complete(self):
        for cause in ["timeout", "cloud"]:
            self.fail_band = "green" if cause == "timeout" else None
            self.bad_scl = cause == "cloud"
            run = self.create()
            self.execute(run)
            self.assertEqual(run.status, "external_service_unavailable" if cause == "timeout" else "blocked", run.error)
            self.assertFalse(run.artifacts_v2.filter(kind="report").exists())
            self.assertFalse(run.evidence_v2.filter(kind="computed_metric").exists())

    def test_restart_after_compute_does_not_repeat_model_search_or_asset_reads(self):
        run = self.create()
        self.execute(run, max_steps=10)
        self.assertEqual(run.steps.get(step_id="compute_metric").status, "completed", run.error)
        self.calls.clear()
        self.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.calls, ["model", "model"])
        self.assertTrue(all(step.attempt == 1 for step in run.steps.all()))

    def test_invalid_main_model_output_stops_before_search(self):
        self.corrupt_model = True
        run = self.create()
        self.execute(run)
        self.assertEqual(run.status, "external_service_unavailable")
        self.assertNotIn("stac", self.calls)
        self.assertFalse(run.artifacts_v2.exists())
