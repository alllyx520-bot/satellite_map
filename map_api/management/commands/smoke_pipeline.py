import json
import os
import time
import uuid
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.test.utils import override_settings
from django.utils import timezone
from PIL import Image

from map_api.models import AgentSession, ChatHistory, DownloadTask, ImageryScene
from map_api.imagery_sources.earth_search import EarthSearchProvider
from map_api.views import _download_progress


def _fake_qwen_response(text):
    message = SimpleNamespace(content=[{"text": text}])
    choice = SimpleNamespace(message=message)
    output = SimpleNamespace(choices=[choice])
    return SimpleNamespace(status_code=HTTPStatus.OK, output=output, message="")


class Command(BaseCommand):
    help = "Run a local smoke test for health, source recommendation, AI, history, report, and report download."

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-artifacts",
            action="store_true",
            help="Keep the generated smoke image, report, and database records for manual inspection.",
        )
        parser.add_argument(
            "--live-sentinel",
            action="store_true",
            help="Also call the public Sentinel-2 search/render service. This depends on external network availability.",
        )
        parser.add_argument(
            "--live-mapbox",
            action="store_true",
            help="Also call the live Mapbox download workflow and poll progress. Requires MAPBOX_TOKEN.",
        )
        parser.add_argument(
            "--live-ai",
            action="store_true",
            help="Call the real DashScope vision model instead of a mocked response. Requires DASHSCOPE_API_KEY.",
        )
        parser.add_argument(
            "--agent",
            action="store_true",
            help="Also run a mocked RemoteSensingAgent loop. Does not call live DeepSeek, DashScope, or imagery providers.",
        )

    def handle(self, *args, **options):
        keep_artifacts = options["keep_artifacts"]
        live_sentinel = options["live_sentinel"]
        live_mapbox = options["live_mapbox"]
        live_ai = options["live_ai"]
        run_agent = options["agent"]
        client = Client()
        smoke_id = uuid.uuid4().hex[:8]
        file_name = f"smoke_{smoke_id}.jpg"
        report_name = ""
        live_sentinel_file = ""
        live_mapbox_file = ""
        agent_session_id = None
        agent_file = ""
        scene = None
        history_id = None
        messages = []
        results = []
        bbox = {"min_lng": 116.38, "min_lat": 39.90, "max_lng": 116.40, "max_lat": 39.92}

        save_dir = os.path.join(settings.MEDIA_ROOT, "satellite_imgs")
        os.makedirs(save_dir, exist_ok=True)
        image_path = os.path.join(save_dir, file_name)

        def record(step_id, ok, message="", data=None):
            payload = {"id": step_id, "ok": ok}
            if message:
                payload["message"] = message
            if data is not None:
                payload["data"] = data
            results.append(payload)

        def require(condition, message):
            if not condition:
                raise AssertionError(message)

        def response_json(resp):
            try:
                return resp.json()
            except ValueError as exc:
                raise AssertionError(f"响应不是 JSON: {resp.status_code}") from exc

        def run(step_id, func):
            try:
                data = func()
                record(step_id, True, data=data)
                return True
            except Exception as exc:
                record(step_id, False, str(exc))
                return False

        def cleanup():
            if keep_artifacts:
                return
            if history_id:
                ChatHistory.objects.filter(id=history_id).delete()
            if scene:
                DownloadTask.objects.filter(scene=scene).delete()
                ChatHistory.objects.filter(scene=scene).delete()
                ImageryScene.objects.filter(id=scene.id).delete()
            else:
                DownloadTask.objects.filter(file_name=file_name).delete()
                ChatHistory.objects.filter(image_file=file_name).delete()
                ImageryScene.objects.filter(file_name=file_name).delete()
            _download_progress.pop(file_name, None)

            image_prefix = f"{os.path.splitext(file_name)[0]}_"
            if os.path.isdir(save_dir):
                for name in os.listdir(save_dir):
                    if name == file_name or name.startswith(image_prefix):
                        path = os.path.join(save_dir, name)
                        if os.path.isfile(path):
                            os.remove(path)
            if report_name:
                report_path = os.path.join(settings.MEDIA_ROOT, report_name)
                if os.path.exists(report_path):
                    os.remove(report_path)
            if live_sentinel_file:
                live_path = os.path.join(save_dir, live_sentinel_file)
                if os.path.exists(live_path):
                    os.remove(live_path)
                DownloadTask.objects.filter(file_name=live_sentinel_file).delete()
                ChatHistory.objects.filter(image_file=live_sentinel_file).delete()
                ImageryScene.objects.filter(file_name=live_sentinel_file).delete()
                _download_progress.pop(live_sentinel_file, None)
            if live_mapbox_file:
                live_path = os.path.join(save_dir, live_mapbox_file)
                if os.path.exists(live_path):
                    os.remove(live_path)
                live_prefix = f"{os.path.splitext(live_mapbox_file)[0]}_"
                if os.path.isdir(save_dir):
                    for name in os.listdir(save_dir):
                        if name.startswith(live_prefix):
                            path = os.path.join(save_dir, name)
                            if os.path.isfile(path):
                                os.remove(path)
                DownloadTask.objects.filter(file_name=live_mapbox_file).delete()
                ChatHistory.objects.filter(image_file=live_mapbox_file).delete()
                ImageryScene.objects.filter(file_name=live_mapbox_file).delete()
                _download_progress.pop(live_mapbox_file, None)
            if agent_session_id:
                session = AgentSession.objects.filter(id=agent_session_id).first()
                if session:
                    agent_file_name = (session.artifacts or {}).get("file_name")
                    AgentSession.objects.filter(id=session.id).delete()
                    if agent_file_name:
                        agent_path = os.path.join(save_dir, agent_file_name)
                        if os.path.exists(agent_path):
                            os.remove(agent_path)
                        DownloadTask.objects.filter(file_name=agent_file_name).delete()
                        ChatHistory.objects.filter(image_file=agent_file_name).delete()
                        ImageryScene.objects.filter(file_name=agent_file_name).delete()
                        _download_progress.pop(agent_file_name, None)
            if agent_file:
                agent_path = os.path.join(save_dir, agent_file)
                if os.path.exists(agent_path):
                    os.remove(agent_path)

        try:
            Image.new("RGB", (96, 96), (80, 120, 160)).save(image_path, "JPEG")
            scene = ImageryScene.objects.create(
                file_name=file_name,
                source="mapbox",
                source_label="Smoke Mapbox Reference",
                min_lng=bbox["min_lng"],
                min_lat=bbox["min_lat"],
                max_lng=bbox["max_lng"],
                max_lat=bbox["max_lat"],
                gsd_m=1.5,
                area_km2=4.0,
                fetched_at=timezone.now(),
            )

            with override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"]):
                steps = [
                    ("health", lambda: self._check_health(client, require, response_json)),
                    ("source_recommendation", lambda: self._check_source_recommendation(client, require, response_json)),
                    (
                        "ai_analysis",
                        lambda: self._check_ai_analysis(
                            client,
                            require,
                            response_json,
                            file_name,
                            scene.id,
                            bbox,
                            messages,
                            live_ai,
                        ),
                    ),
                    (
                        "history",
                        lambda: self._check_history(
                            client,
                            require,
                            response_json,
                            file_name,
                            scene.id,
                            bbox,
                            messages,
                        ),
                    ),
                    (
                        "report",
                        lambda: self._check_report(
                            client,
                            require,
                            response_json,
                            file_name,
                            scene.id,
                            bbox,
                            messages,
                        ),
                    ),
                ]
                if live_mapbox:
                    steps.append(("live_mapbox", lambda: self._check_live_mapbox(client, require, response_json)))
                if live_sentinel:
                    steps.append(("live_sentinel", lambda: self._check_live_sentinel(client, require, response_json)))
                if run_agent:
                    steps.append(("agent", lambda: self._check_agent(client, require, response_json)))

                for step_id, func in steps:
                    ok = run(step_id, func)
                    if not ok:
                        break
                    if step_id == "history":
                        history_id = results[-1]["data"]["history_id"]
                    if step_id == "report":
                        report_name = results[-1]["data"]["report_name"]
                    if step_id == "live_sentinel":
                        live_sentinel_file = results[-1]["data"]["file_name"]
                    if step_id == "live_mapbox":
                        live_mapbox_file = results[-1]["data"]["file_name"]
                    if step_id == "agent":
                        agent_session_id = results[-1]["data"]["session_id"]
                        agent_file = results[-1]["data"].get("file_name", "")

            expected_steps = 5 + int(live_mapbox) + int(live_sentinel) + int(run_agent)
            status = "passed" if all(item["ok"] for item in results) and len(results) == expected_steps else "failed"
            output = {
                "status": status,
                "smoke_id": smoke_id,
                "kept_artifacts": keep_artifacts,
                "live_sentinel": live_sentinel,
                "live_mapbox": live_mapbox,
                "live_ai": live_ai,
                "agent": run_agent,
                "steps": results,
                "artifacts": {
                    "image_file": file_name,
                    "scene_id": scene.id if scene else None,
                    "history_id": history_id,
                    "report_file": report_name,
                    "live_sentinel_file": live_sentinel_file,
                    "live_mapbox_file": live_mapbox_file,
                    "agent_session_id": agent_session_id,
                    "agent_file": agent_file,
                },
            }
            self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
            if status != "passed":
                raise CommandError("smoke_pipeline failed")
        finally:
            cleanup()

    def _check_health(self, client, require, response_json):
        resp = client.get("/api/system/health/")
        data = response_json(resp)
        require(resp.status_code == 200, f"health status={resp.status_code}")
        require(data.get("code") == 200, "health code 不是 200")
        require(data["data"]["imagery_strategy"]["default_source"] == "mapbox", "默认图像源不是 mapbox")
        return {"status": data["data"]["status"]}

    def _check_source_recommendation(self, client, require, response_json):
        resp = client.post(
            "/api/imagery/recommend-source/",
            data={"question": "分析最近农田长势", "current_source": "mapbox"},
            content_type="application/json",
        )
        data = response_json(resp)
        rec = data["data"]["recommendation"]
        require(resp.status_code == 200 and data.get("code") == 200, "图像源推荐接口失败")
        require(rec["recommended_source"] == "sentinel2", "近期农业问题未推荐 Sentinel-2")
        return {"recommended_source": rec["recommended_source"], "task_label": rec["task_label"]}

    def _check_ai_analysis(self, client, require, response_json, file_name, scene_id, bbox, messages, live_ai=False):
        answer_text = "<answer>烟测结论：该区域可进行综合土地利用初判，需结合可追溯影像复核时效性。</answer>"
        payload = {
            "file_name": file_name,
            "scene_id": scene_id,
            "question": "用一句话概括这张遥感影像的主要地物类型。",
            "mode": "fast",
            "active_perception": False,
            "history": [],
            "gsd": 1.5,
            "bbox": bbox,
        }
        if live_ai:
            resp = client.post("/api/ai/query-region/", data=payload, content_type="application/json")
        else:
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "smoke-test-key"}, clear=False), \
                    patch("map_api.views._call_qwen", return_value=_fake_qwen_response(answer_text)):
                resp = client.post("/api/ai/query-region/", data=payload, content_type="application/json")
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "AI 接口失败"))
        require(data["data"].get("answer"), "AI 结论缺失")
        if not live_ai:
            require("烟测结论" in data["data"]["answer"], "mock AI 结论缺失")
        method = data["data"]["analysis_method"]
        require(method["model"] == "qwen3-vl-flash", "快速模式模型不正确")
        require("output_quality" in method, "输出质量元数据缺失")
        messages[:] = [
            {"role": "user", "content": payload["question"]},
            {"role": "ai", "content": data["data"]["answer"], "analysis_method": method},
        ]
        return {
            "model": method["model"],
            "task_label": method["task_label"],
            "live": live_ai,
            "structured_answer": method["output_quality"].get("structured_answer"),
            "fallback_used": method["output_quality"].get("fallback_used"),
        }

    def _check_history(self, client, require, response_json, file_name, scene_id, bbox, messages):
        resp = client.post(
            "/api/ai/history/",
            data={
                "image_file": file_name,
                "scene_id": scene_id,
                "messages": messages,
                "spatial_context": "smoke pipeline",
                "bbox": bbox,
            },
            content_type="application/json",
        )
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "历史保存失败"))
        history_id = data["data"]["id"]
        detail = client.get(f"/api/ai/history/{history_id}/")
        detail_data = response_json(detail)
        require(detail.status_code == 200 and detail_data.get("code") == 200, "历史读取失败")
        require(len(detail_data["data"]["messages"]) == 2, "历史消息数量不正确")
        return {"history_id": history_id}

    def _check_report(self, client, require, response_json, file_name, scene_id, bbox, messages):
        resp = client.post(
            "/api/report/generate/",
            data={
                "file_name": file_name,
                "scene_id": scene_id,
                "title": "SatelliteSense 烟测报告",
                "messages": messages,
                "spatial_context": "smoke pipeline",
                "bbox": bbox,
            },
            content_type="application/json",
        )
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "报告生成失败"))
        report_name = data["data"]["file_name"]
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        require(os.path.exists(report_path), "报告文件未落盘")
        download = client.get(data["data"]["download_url"])
        require(download.status_code == 200, f"报告下载失败: {download.status_code}")
        if hasattr(download, "close"):
            download.close()
        return {"report_name": report_name}

    def _check_live_sentinel(self, client, require, response_json):
        resp = client.post(
            "/api/satellite/get-sentinel-img/",
            data={
                "min_lng": 116.38,
                "min_lat": 39.90,
                "max_lng": 116.40,
                "max_lat": 39.92,
                "target_resolution": 256,
                "candidate_limit": 2,
                "max_cloud": 60,
            },
            content_type="application/json",
        )
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "Sentinel-2 真实链路失败"))
        payload = data["data"]
        file_name = payload["file_name"]
        image_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", file_name)
        require(os.path.exists(image_path), "Sentinel-2 影像未落盘")
        require(payload["scene"]["source"] == "sentinel2", "Sentinel-2 scene source 不正确")
        return {
            "file_name": file_name,
            "candidate_count": payload.get("candidate_count"),
            "selection": payload["scene"].get("selection"),
        }

    def _check_live_mapbox(self, client, require, response_json):
        resp = client.post(
            "/api/satellite/get-img/",
            data={
                "min_lng": 116.38,
                "min_lat": 39.90,
                "max_lng": 116.381,
                "max_lat": 39.901,
                "target_resolution": 256,
            },
            content_type="application/json",
        )
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "Mapbox 下载启动失败"))
        payload = data["data"]
        file_name = payload["file_name"]
        deadline = time.time() + 90
        progress_data = {}
        while time.time() < deadline:
            progress = client.get(f"/api/satellite/progress/?file={file_name}")
            progress_json = response_json(progress)
            progress_data = progress_json.get("data") or {}
            if progress_data.get("status") in ("done", "partial", "error"):
                break
            time.sleep(0.5)
        require(progress_data.get("status") in ("done", "partial"), f"Mapbox 下载未完成: {progress_data}")
        image_path = os.path.join(settings.MEDIA_ROOT, "satellite_imgs", file_name)
        require(os.path.exists(image_path), "Mapbox 影像未落盘")
        scene_payload = payload.get("scene") or {}
        require(scene_payload.get("source") == "mapbox", "Mapbox scene source 不正确")
        return {
            "file_name": file_name,
            "status": progress_data.get("status"),
            "total": progress_data.get("total"),
            "done": progress_data.get("done"),
            "failed": progress_data.get("failed"),
        }

    def _check_agent(self, client, require, response_json):
        buf_path = None

        def jpg_bytes():
            from io import BytesIO

            buf = BytesIO()
            Image.new("RGB", (96, 96), (90, 130, 170)).save(buf, "JPEG")
            return buf.getvalue()

        candidate = EarthSearchProvider().candidate_from_item({
            "id": "S2A_AGENT_SMOKE",
            "collection": "sentinel-2-l2a",
            "bbox": [108.1, 22.1, 108.5, 22.9],
            "properties": {
                "datetime": "2026-04-12T03:17:00Z",
                "eo:cloud_cover": 8,
                "s2:product_uri": "S2A_AGENT_SMOKE.SAFE",
            },
            "assets": {
                "visual": {"href": "https://example.com/visual.tif", "gsd": 10},
                "green": {"href": "https://example.com/green.tif", "gsd": 10},
                "nir": {"href": "https://example.com/nir.tif", "gsd": 10},
            },
        })
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "smoke-deepseek-key",
            "DASHSCOPE_API_KEY": "smoke-dashscope-key",
            "AMAP_KEY": "smoke-amap-key",
        }, clear=False), \
                patch("map_api.utils.agent_tools.call_deepseek_json", return_value={
                    "place_name": "南宁市",
                    "date_start": "2026-04-01",
                    "date_end": "2026-04-30",
                    "task": "water",
                    "source": "sentinel2",
                    "mode": "precise",
                }), \
                patch("map_api.views.resolve_district_bbox", return_value={
                    "name": "南宁市",
                    "adcode": "450100",
                    "level": "city",
                    "bbox": {"min_lng": 108.1, "min_lat": 22.1, "max_lng": 108.5, "max_lat": 22.9},
                    "candidate_count": 1,
                    "bbox_policy": "行政区 bbox 筛查，不做精确行政边界裁剪",
                }), \
                patch("map_api.views.EarthSearchProvider.search", return_value=[candidate]), \
                patch("map_api.views.EarthSearchProvider.render_candidate_jpeg", return_value=jpg_bytes()), \
                patch("map_api.views.compute_ndwi_summary", return_value={
                    "available": True,
                    "method": "NDWI=(Green-NIR)/(Green+NIR)",
                    "water_percent": 12.3,
                    "limitations": "轻量 NDWI 仅用于 bbox 内水体线索筛查。",
                }), \
                patch("map_api.views._call_qwen", return_value=_fake_qwen_response("<answer>水体主要分布在河道和坑塘。</answer>")), \
                patch("map_api.views.call_deepseek", return_value="Agent 复核结论：水体线索明确，NDWI 仅作辅助筛查。"):
            resp = client.post(
                "/api/agent/sessions/",
                data={"goal": "帮我调查南宁市在2026年四月的水体情况", "sync": True},
                content_type="application/json",
            )
        data = response_json(resp)
        require(resp.status_code == 200 and data.get("code") == 200, data.get("msg", "Agent 接口失败"))
        payload = data["data"]
        require(payload["status"] == "completed", f"Agent 未完成: {payload['status']}")
        require(payload["slots"]["task"] == "water", "Agent 未识别水体任务")
        require(payload["artifacts"]["ndwi"]["water_percent"] == 12.3, "NDWI 结果缺失")
        require("Agent 复核结论" in payload["artifacts"]["final_answer"], "Agent 复核结论缺失")
        return {
            "session_id": payload["id"],
            "file_name": payload["artifacts"].get("file_name"),
            "history_id": payload.get("history_id"),
        }
