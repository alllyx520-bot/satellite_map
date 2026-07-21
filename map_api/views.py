from django.http import JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.shortcuts import render
from http import HTTPStatus
import dashscope
import hashlib
import json
import os
import uuid
import math
import threading
import requests
import logging
import time
from datetime import date, timedelta
from io import BytesIO
import numpy as np
from PIL import Image
from django.utils import timezone
from django.db import close_old_connections, connection

from .utils.get_satellite_image import fetch_satellite_image, haversine_distance, get_download_progress, prune_progress, _download_progress
from .utils.image_preprocessor import smart_prepare_image_v2, MAX_DIM_MAP
from .utils.analysis_strategy import build_analysis_strategy
from .utils.active_perception import (
    build_stage1_prompt, extract_bbox_from_response,
    cut_image_geom, map_bbox_to_original, resize_image, build_stage2_prompt,
    extract_answer_text, measure_bbox, pixel_bbox_to_geo
)
from .models import AgentSession, ChatHistory, DownloadTask, ImageryScene
from .imagery_sources.mapbox import MapboxProvider
from .imagery_sources.earth_search import EarthSearchProvider
from .utils.agent_tools import (
    AGENT_MODEL,
    build_agent_plan,
    call_deepseek,
    compute_ndwi_mosaic_summary,
    compute_ndwi_summary,
    resolve_district_bbox,
)

# Phase 7 M-1:几何纯函数已移至 map_api/geo_math.py,这里按原名再导出,
# 既有 patch("map_api.views.X") 与 from map_api.views import X 全部不受影响。
from .geo_math import (
    SENTINEL_MAX_AUTO_CROP_RATIO,
    normalize_bbox, compute_image_plan,
    bbox_area_deg, bbox_intersection_ratio, bbox_intersection, bbox_union_coverage_ratio,
    image_valid_ratio, image_plan_for_bbox_and_size, bbox_for_image_crop,
    crop_sentinel_nodata_border, sentinel_nodata_crop_too_large,
)

# Phase 7 M-2:payload 构造函数已移至 map_api/payloads.py,这里按原名再导出。
from .payloads import (
    _scene_source_key, _days_since, _grade_label, _timeliness_label, _cloud_label,
    imagery_quality_payload, analysis_confidence_payload, analysis_confidence_text,
    source_recommendation_payload, scene_selection_payload, scene_brief_payload,
    sentinel_retrieval_timeline_payload, scene_payload, imagery_context_text,
    analysis_method_payload, latest_analysis_method, normalize_model_answer,
    docx_safe_text, join_method_items, chat_history_payload,
)

# Phase 7:媒体路径与穿越防护统一出自 map_api/media_paths.py(SAVE_DIR/REPORT_DIR 唯一定义处)
from .media_paths import SAVE_DIR, REPORT_DIR, safe_media_path
# Phase 7 M-3:Sentinel-2 管线已移至 map_api/sentinel_pipeline.py,这里按原名再导出。
from .sentinel_pipeline import (
    SENTINEL_FALLBACK_MSG, SENTINEL_DEFAULT_MIN_COVERAGE_RATIO, SENTINEL_DEFAULT_MIN_VALID_IMAGE_RATIO,
    postprocess_cached_sentinel_scene, compose_sentinel_mosaic,
    sentinel_cache_key, sentinel_mosaic_cache_key,
    find_cached_sentinel_scene, find_cached_sentinel_mosaic_scene,
    select_best_sentinel_candidate, sorted_sentinel_candidates,
    greedy_cover_sentinel_candidates, select_sentinel_scene_candidates,
    sentinel_response_data, scene_from_candidate, scene_from_sentinel_mosaic,
    create_done_download_task, sentinel_retrieval_result,
)

# Phase 7 M-4:Agent 编排已移至 map_api/orchestrator.py,这里按原名再导出。
from .orchestrator import (
    AGENT_MODES, AGENT_STAGE_PUBLIC_THOUGHTS, AGENT_OBSERVER_DEFAULT_STEPS,
    agent_session_payload, _agent_observer_payload, _agent_set_observer, _agent_step,
    _agent_fail, _agent_wait, _agent_store_scene_artifacts, _scene_matches_requested_dates,
    _candidate_from_mosaic_metadata, _run_agent_background, _agent_internal_ai_query,
    _agent_internal_report, _agent_fetch_sentinel, _agent_fetch_mapbox,
    run_agent_session, resume_waiting_agent_session,
)

logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("map_api.ai")

# 支持高分辨率图像输入的 VL 模型(需开 vl_high_resolution_images)
VL_MODELS = ('qwen3-vl-plus', 'qwen3-vl-flash', 'qwen-vl-max', 'qwen-vl-plus')
ANALYSIS_MODES = {
    "precise": {"model": "qwen3-vl-plus", "active_perception": True},
    "fast": {"model": "qwen3-vl-flash", "active_perception": False},
}
IMAGERY_STRATEGY = {
    "id": "task_adaptive_dual_source",
    "label": "任务自适应双源影像策略",
    "default_source": "mapbox",
    "recommendation_endpoint": "/api/imagery/recommend-source/",
    "principle": "保留 Mapbox 高清底图作为默认主流程；当问题强调近期态势、宏观地类、水体、植被、农业或变化筛查时，推荐切换 Sentinel-2 近期公开影像。",
    "source_roles": {
        "mapbox": "高清参考底图，适合建筑、道路、设施、空间格局和细节视觉解译。",
        "sentinel2": "近期公开可追溯影像，适合宏观地类、水体、植被、农业和变化线索筛查。",
    },
}
SMART_PIPELINE_PROFILE = [
    {
        "id": "query_understanding",
        "label": "智能问句理解",
        "method": "中文遥感关键词、实体、空间方位与任务意图解析",
        "value": "判断用户问题偏细节、宏观、对比或表格输出，并驱动后续分辨率、图像源和提示词策略。",
    },
    {
        "id": "adaptive_source_routing",
        "label": "双源任务路由",
        "method": "按任务粒度在高清底图与 Sentinel-2 近期公开影像之间给出推荐",
        "value": "把图像源选择从用户经验判断变成系统可解释建议，降低误用时效不足或分辨率不足影像的风险。",
    },
    {
        "id": "remote_sensing_task_rubric",
        "label": "专业任务画像",
        "method": "按水体、植被、农业、建设用地、地形灾害、土地利用等遥感任务组织解译维度",
        "value": "让 AI 回答稳定包含遥感专业术语、判读依据、限制条件和面向决策的结构化结论。",
    },
    {
        "id": "semantic_tile_retrieval",
        "label": "语义分块检索",
        "method": "借鉴 ImageRAG / RemoteCLIP 思路，优先用遥感跨模态语义相似度筛选分块，不可用时降级到颜色纹理启发式",
        "value": "大图分析时优先把最相关的区域送入视觉模型，提升速度和可解释性。",
    },
    {
        "id": "active_perception",
        "label": "主动感知放大",
        "method": "借鉴 ZoomEye / AdaptVision 的先定位再局部放大流程",
        "value": "对建筑、道路、设施等细节问题进行多级放大，并把目标尺寸与经纬度回传到地图。",
    },
    {
        "id": "evidence_confidence",
        "label": "证据等级与报告留痕",
        "method": "结合数据源、时效、云量、GSD 和任务粒度生成可信度与复核要求",
        "value": "下载、AI 分析、历史和 Word 报告都保留同一套方法说明，便于复盘和测试。",
    },
]


def _call_qwen(model_name, messages):
    """统一封装 dashscope 调用,集中处理 VL 模型的高清入参。"""
    kwargs = {"model": model_name, "messages": messages}
    if model_name in VL_MODELS:
        kwargs["vl_high_resolution_images"] = True
    started = time.perf_counter()
    try:
        response = dashscope.MultiModalConversation.call(**kwargs)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ai_logger.info(
            "model=%s status=%s elapsed_ms=%s message_count=%s",
            model_name,
            getattr(response, "status_code", "unknown"),
            elapsed_ms,
            len(messages or []),
        )
        return response
    except Exception:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ai_logger.exception("model=%s status=exception elapsed_ms=%s", model_name, elapsed_ms)
        raise



def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise ValueError("请求体不是有效 JSON")


def parse_number_param(data, name, default=None, min_value=None, max_value=None, as_int=False, allow_blank=False):
    raw = data.get(name, default)
    if allow_blank and raw in ("", None):
        return None
    try:
        value = int(raw) if as_int else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是{'整数' if as_int else '数字'}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} 不能小于 {min_value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} 不能大于 {max_value}")
    return value


def _task_progress(task):
    data = {
        "total": task.total,
        "done": task.done,
        "failed": task.failed,
        "status": task.status,
        "error": task.error_message,
    }
    if task.scene_id:
        data["scene_id"] = task.scene_id
    return data


def _persist_progress(file_name, info):
    close_old_connections()
    fields = {
        "status": info.get("status", "downloading"),
        "total": int(info.get("total", 1) or 1),
        "done": int(info.get("done", 0) or 0),
        "failed": int(info.get("failed", 0) or 0),
        "error_message": str(info.get("error", ""))[:500],
    }
    try:
        DownloadTask.objects.filter(file_name=file_name).update(**fields)
    finally:
        close_old_connections()


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _dir_writable(path):
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, f".health_{uuid.uuid4().hex}.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        return True
    except OSError:
        return False
    finally:
        if os.path.exists(probe):
            try:
                os.remove(probe)
            except OSError:
                pass


def system_health(request):
    if request.method != "GET":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)

    checks = {}
    errors = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = True
    except Exception as e:
        checks["database"] = False
        errors["database"] = str(e)[:160]

    checks["media_root_writable"] = _dir_writable(settings.MEDIA_ROOT)
    checks["satellite_image_dir_writable"] = _dir_writable(SAVE_DIR)

    config = {
        "mapbox_token": bool(os.environ.get("MAPBOX_TOKEN")),
        "dashscope_api_key": bool(os.environ.get("DASHSCOPE_API_KEY")),
        "deepseek_api_key": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "amap_key": bool(os.environ.get("AMAP_KEY")),
        "titiler_endpoint": os.environ.get("TITILER_ENDPOINT") or "https://titiler.xyz",
    }
    required_ok = checks["database"] and checks["media_root_writable"] and checks["satellite_image_dir_writable"]
    required_ok = required_ok and config["mapbox_token"] and config["dashscope_api_key"]

    return JsonResponse({
        "code": 200,
        "data": {
            "status": "ok" if required_ok else "degraded",
            "checks": checks,
            "config": config,
            "imagery_strategy": IMAGERY_STRATEGY,
            "smart_pipeline": SMART_PIPELINE_PROFILE,
            "imagery_sources": {
                "mapbox": {
                    "role": "default_high_resolution_reference",
                    "available": config["mapbox_token"],
                    "label": "Mapbox 高清底图",
                    "recommended_for": ["建筑形态", "道路结构", "设施识别", "空间格局", "细节视觉解译"],
                    "limitations": ["拍摄时间、云量和原始产品号不可追溯", "不适合作为近期态势的单一证据"],
                },
                "sentinel2": {
                    "role": "optional_recent_traceable_public",
                    "available": True,
                    "label": "Sentinel-2 近期公开影像",
                    "provider": "Element84 Earth Search / Sentinel-2 L2A",
                    "renderer": config["titiler_endpoint"],
                    "recommended_for": ["近期态势", "宏观地类", "水体岸线", "植被农业", "变化线索筛查"],
                    "limitations": ["约 10m 空间分辨率，不适合车辆、小建筑等细节目标", "云量、重访周期和公开服务可用性会影响稳定性"],
                },
            },
            "analysis_modes": {
                mode: {
                    "model": cfg["model"],
                    "active_perception": cfg["active_perception"],
                }
                for mode, cfg in ANALYSIS_MODES.items()
            },
            "agent": {
                "available": config["deepseek_api_key"] and config["dashscope_api_key"] and config["amap_key"],
                "controller_model": os.environ.get("AGENT_MODEL", AGENT_MODEL),
                "modes": AGENT_MODES,
                "workflow": [
                    "任务理解",
                    "行政区 bbox 定位",
                    "图像源选择",
                    "影像检索",
                    "质量检查",
                    "NDWI 轻量量化",
                    "VL 解译",
                    "DeepSeek 复核",
                ],
            },
            "errors": errors,
        },
    })





def history_image_available(image_file):
    path = safe_media_path(SAVE_DIR, image_file, ('.jpg', '.jpeg', '.png'))
    return bool(path and os.path.exists(path))



def index_view(request):
    """展示产品首页。"""
    return render(request, 'home.html')


def workbench_view(request):
    """负责展示前端地图页面。"""
    return render(request, 'browser.html')


def design_view(request):
    """SPECTRA 设计系统预览页（设计验收用）。"""
    return render(request, 'design.html')

# ----------------------
# 卫星图下载接口
# ----------------------
@csrf_exempt
def get_satellite_img_api(request):
    if request.method != 'POST':
        return JsonResponse({'code':405,'msg':'仅支持POST','data':None},status=405)

    try:
        data = _json_body(request)
        if not os.environ.get('MAPBOX_TOKEN'):
            return JsonResponse({"code": 500, "msg": "缺少 MAPBOX_TOKEN，请先在 .env 中配置", "data": None}, status=500)

        min_lng, min_lat, max_lng, max_lat = normalize_bbox(data)
        resolution = int(data.get('target_resolution', 0))

        diag_m = haversine_distance(min_lng, min_lat, max_lng, max_lat)
        if resolution <= 0:
            if diag_m > 20000:
                resolution = 3072
            elif diag_m > 5000:
                resolution = 2048
            else:
                resolution = 1280

        plan = compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution)
        total_tiles = plan["total_tiles"]
        file_name = f"sat_{uuid.uuid4().hex[:8]}.jpg"
        gsd = plan["gsd_m"]
        area_km2 = plan["area_km2"]
        bbox_payload = {"min_lng": min_lng, "min_lat": min_lat, "max_lng": max_lng, "max_lat": max_lat}
        metadata = MapboxProvider().metadata_for_bbox(bbox_payload).as_dict()
        scene = ImageryScene.objects.create(
            file_name=file_name,
            min_lng=min_lng,
            min_lat=min_lat,
            max_lng=max_lng,
            max_lat=max_lat,
            gsd_m=round(gsd, 2),
            area_km2=round(area_km2, 4),
            **metadata,
        )

        task = DownloadTask.objects.create(
            scene=scene,
            file_name=file_name,
            status="downloading",
            total=total_tiles,
            done=0,
            failed=0,
            min_lng=min_lng,
            min_lat=min_lat,
            max_lng=max_lng,
            max_lat=max_lat,
            gsd_m=round(gsd, 2),
            area_km2=round(area_km2, 4),
            resolution_px=resolution,
        )
        prune_progress()
        _download_progress[file_name] = {"total": total_tiles, "done": 0, "failed": 0, "status": "downloading"}

        def _download():
            close_old_connections()
            try:
                img_path = fetch_satellite_image(
                    min_lng, min_lat, max_lng, max_lat,
                    save_dir=SAVE_DIR,
                    file_name=file_name,
                    target_resolution=resolution,
                    progress_callback=_persist_progress,
                )
                if not img_path:
                    _download_progress[file_name]["status"] = "error"
                    DownloadTask.objects.filter(id=task.id).update(status="error")
            except Exception as e:
                logger.exception("satellite download failed: %s", file_name)
                _download_progress[file_name]["status"] = "error"
                _download_progress[file_name]["error"] = str(e)[:200]
                DownloadTask.objects.filter(id=task.id).update(status="error", error_message=str(e)[:500])
            finally:
                close_old_connections()

        threading.Thread(target=_download, daemon=True).start()

        return JsonResponse({
            "code": 200,
            "msg": "下载已启动",
            "data": {
                "file_name": file_name,
                "total_tiles": total_tiles,
                "scene_id": scene.id,
                "scene": scene_payload(scene),
                "gsd_m": round(gsd, 2),
                "area_km2": round(area_km2, 4),
                "resolution_px": resolution,
            }
        })

    except Exception as e:
        return JsonResponse({"code":400,"msg":str(e),"data":None},status=400)


@csrf_exempt
def get_sentinel_img_api(request):
    if request.method != 'POST':
        return JsonResponse({'code': 405, 'msg': '仅支持POST', 'data': None}, status=405)

    try:
        data = _json_body(request)
        min_lng, min_lat, max_lng, max_lat = normalize_bbox(data)
        max_cloud = parse_number_param(data, "max_cloud", default=30, min_value=0, max_value=100)
        start_date = data.get("start")
        end_date = data.get("end")
        resolution = parse_number_param(
            data,
            "target_resolution",
            default=1024,
            min_value=256,
            max_value=2048,
            as_int=True,
        )
        candidate_limit = parse_number_param(
            data,
            "candidate_limit",
            default=10,
            min_value=1,
            max_value=10,
            as_int=True,
        )
        bbox = {"min_lng": min_lng, "min_lat": min_lat, "max_lng": max_lng, "max_lat": max_lat}
        plan = compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution)
        provider = EarthSearchProvider(titiler_endpoint=os.environ.get("TITILER_ENDPOINT", None) or None)
        try:
            candidates = provider.search(
                bbox,
                start_date=start_date,
                end_date=end_date,
                max_cloud=max_cloud,
                limit=candidate_limit,
                collection="sentinel-2-l2a",
            )
        except (requests.RequestException, ValueError) as e:
            logger.warning("sentinel image search failed: %s", e)
            return JsonResponse({"code": 502, "msg": SENTINEL_FALLBACK_MSG, "data": None}, status=502)
        if not candidates:
            return JsonResponse({"code": 404, "msg": "未找到符合条件的 Sentinel-2 影像，可切回高清底图继续分析", "data": None}, status=404)

        try:
            retrieval = sentinel_retrieval_result(
                provider,
                candidates,
                bbox,
                plan,
                resolution,
                file_prefix="sentinel",
            )
        except ValueError as e:
            logger.warning("sentinel image retrieval failed: %s", e)
            message = SENTINEL_FALLBACK_MSG if "渲染失败" in str(e) else str(e)
            return JsonResponse({"code": 502, "msg": message, "data": None}, status=502)

        scene = retrieval["scene"]
        candidate = retrieval["candidate"]
        msg = "已复用本地 Sentinel-2 影像缓存" if retrieval.get("cache_hit") else "Sentinel-2 影像已生成"
        if retrieval.get("mosaic") and not retrieval.get("cache_hit"):
            msg = "Sentinel-2 多景拼接影像已生成"

        return JsonResponse({
            "code": 200,
            "msg": msg,
            "data": sentinel_response_data(
                scene,
                candidate,
                plan,
                resolution,
                cache_hit=bool(retrieval.get("cache_hit")),
                candidate_count=len(candidates),
                retrieval=retrieval,
            ),
        })
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e), "data": None}, status=400)



@csrf_exempt
def agent_session_list(request):
    if request.method == "GET":
        limit = min(50, max(1, int(request.GET.get("limit", 20))))
        return JsonResponse({"code": 200, "data": [agent_session_payload(s) for s in AgentSession.objects.all()[:limit]]})
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        data = _json_body(request)
        goal = (data.get("goal") or data.get("message") or "").strip()
        if not goal:
            return JsonResponse({"code": 400, "msg": "调查目标不能为空", "data": None}, status=400)
        if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            return JsonResponse({"code": 500, "msg": "缺少 DEEPSEEK_API_KEY，请先在 .env 中配置", "data": None}, status=500)
        mode = data.get("mode", "precise")
        if mode not in AGENT_MODES:
            mode = "precise"
        context = {
            "bbox": data.get("bbox") if isinstance(data.get("bbox"), dict) else None,
            "scene_id": data.get("scene_id"),
            "file_name": os.path.basename(data.get("file_name", "") or ""),
            "force_continue": _as_bool(data.get("force_continue", False)),
        }
        session = AgentSession.objects.create(
            goal=goal,
            mode=mode,
            status=AgentSession.STATUS_RUNNING,
            messages=[{"role": "user", "content": goal}],
            artifacts={"entry": "agent", "context": {k: v for k, v in context.items() if v}},
        )
        if _as_bool(data.get("sync", False)):
            run_agent_session(session.id, context)
        else:
            threading.Thread(target=run_agent_session, args=(session.id, context), daemon=True).start()
        session.refresh_from_db()
        return JsonResponse({"code": 200, "msg": "Agent 调查任务已启动", "data": agent_session_payload(session)})
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e), "data": None}, status=400)


@csrf_exempt
def agent_session_detail(request, session_id):
    try:
        session = AgentSession.objects.get(id=session_id)
    except AgentSession.DoesNotExist:
        return JsonResponse({"code": 404, "msg": "not found", "data": None}, status=404)
    if request.method == "GET":
        return JsonResponse({"code": 200, "data": agent_session_payload(session)})
    return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)


@csrf_exempt
def agent_session_messages(request, session_id):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        session = AgentSession.objects.get(id=session_id)
    except AgentSession.DoesNotExist:
        return JsonResponse({"code": 404, "msg": "not found", "data": None}, status=404)
    try:
        data = _json_body(request)
        content = (data.get("content") or data.get("message") or "").strip()
        action = (data.get("action") or "").strip()
        if not content and not action:
            return JsonResponse({"code": 400, "msg": "消息不能为空", "data": None}, status=400)
        messages = list(session.messages or [])
        if content:
            messages.append({"role": "user", "content": content})
        session.messages = messages
        session.save(update_fields=["messages", "updated_at"])

        wants_report = action == "generate_report" or "生成报告" in content
        if wants_report:
            artifacts = dict(session.artifacts or {})
            if session.status != AgentSession.STATUS_COMPLETED or not artifacts.get("file_name"):
                return JsonResponse({"code": 400, "msg": "Agent 调查尚未完成，不能生成报告", "data": agent_session_payload(session)}, status=400)
            report_payload = {
                "file_name": artifacts["file_name"],
                "scene_id": session.scene_id,
                "title": f"SatelliteSense Agent 调查报告 - {session.slots.get('place_name', '调查区域')}",
                "messages": [m for m in session.messages if m.get("role") in ("user", "ai")],
                "spatial_context": f"Agent 调查：{session.goal}",
                "bbox": artifacts.get("bbox") or session.slots.get("bbox"),
            }
            status_code, report_data = _agent_internal_report(report_payload)
            if status_code != 200 or report_data.get("code") != 200:
                return JsonResponse({"code": 500, "msg": report_data.get("msg", "报告生成失败"), "data": None}, status=500)
            artifacts["report"] = report_data["data"]
            session.artifacts = artifacts
            session.messages = list(session.messages or []) + [{"role": "assistant", "content": "Word 报告已生成。"}]
            session.save(update_fields=["artifacts", "messages", "updated_at"])
        elif session.status == AgentSession.STATUS_WAITING_USER:
            resume_waiting_agent_session(session, content or action)
            session.refresh_from_db()
        return JsonResponse({"code": 200, "data": agent_session_payload(session)})
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e), "data": None}, status=400)

# ----------------------
# 精准读取图片接口 (防缓存、防串联)
# ----------------------
def show_satellite_image(request):
    file_name = request.GET.get('file')
    if file_name:
        target_path = safe_media_path(SAVE_DIR, file_name, ('.jpg', '.jpeg', '.png'))
        if not target_path or not os.path.exists(target_path):
             return JsonResponse({"code": 404, "msg": "找不到指定的卫星图"}, status=404)
        if os.path.basename(file_name).startswith("sentinel"):
            scene = ImageryScene.objects.filter(file_name=os.path.basename(file_name), source="sentinel2").first()
            if scene:
                try:
                    postprocess_cached_sentinel_scene(scene)
                except Exception:
                    logger.exception("sentinel image postprocess failed before serving: %s", file_name)
    else:
        # 兼容旧版的后备逻辑:只取原始下载图(sat_*.jpg),排除 _hd/_overview/_tile/_crop/_stage1 等派生图
        files = sorted([f for f in os.listdir(SAVE_DIR)
                        if (f.startswith('sat_') or f.startswith('sentinel_')) and f.endswith('.jpg')],
                        key=lambda x: os.path.getmtime(os.path.join(SAVE_DIR, x)), reverse=True)
        if not files:
            return JsonResponse({"code":404,"msg":"无图片"},status=404)
        target_path = os.path.join(SAVE_DIR, files[0])
        
    return FileResponse(open(target_path,'rb'), content_type='image/jpeg')


# ── Active Perception Pipeline Helpers ────────────────────────────────
# Directly adapted from ZoomEarth stage1/stage2 two-pass + AdaptVision tool-call prompt

def _stage1_scale(stage1_img, orig_w):
    """Stage1 缩略图坐标 → 原图坐标的缩放比。模型在 stage1_img 上给的 bbox
    需乘以此比例才能在全分辨率原图(cut_image 的输入)上对齐。"""
    try:
        with Image.open(stage1_img) as im:
            s1_w = im.size[0]
        return orig_w / s1_w if s1_w else 1.0
    except Exception:
        return 1.0


def _build_active_perception_single(image_path, original_path, question, spatial_ctx, system_prompt, orig_w):
    stage1_img = resize_image(image_path, max_size=1024)
    if not stage1_img:
        stage1_img = image_path

    prompt = build_stage1_prompt(question, spatial_ctx)
    content = []
    content.append({"image": f"file://{stage1_img}"})
    content.append({"text": prompt})

    return [{"role": "user", "content": content}], _stage1_scale(stage1_img, orig_w)


def _build_active_perception_tiled(tiles, grid, preprocess, original_path, question, spatial_ctx, system_prompt, orig_w):
    overview = tiles[0]
    stage1_img = resize_image(overview, max_size=1024)
    if not stage1_img:
        stage1_img = overview

    prompt = build_stage1_prompt(question, spatial_ctx)
    content = []
    content.append({"image": f"file://{stage1_img}"})
    content.append({"text": prompt})

    return [{"role": "user", "content": content}], _stage1_scale(stage1_img, orig_w)


MAX_ZOOM_LEVELS = 2   # 主动感知最多额外放大级数(ZoomEye 式迭代)


def _measure_and_locate(orig_bbox, gsd, preprocess, geo_bbox):
    """由原图像素 bbox 算真实尺寸(GSD)+ 中心经纬度。返回 (measure_note, target_dict)。"""
    target = {"label": "AI 关注区域"}
    measure_note = ""
    if gsd:
        try:
            w_m, h_m, area_m2 = measure_bbox(orig_bbox, float(gsd))
            target.update({"width_m": round(w_m, 1), "height_m": round(h_m, 1),
                           "area_m2": round(area_m2, 1)})
            measure_note = f"该放大区域在原图中约 {round(w_m,1)}m × {round(h_m,1)}m(GSD {float(gsd):.2f} m/像素)。请在判断目标大小/类型时参考此真实尺度。"
        except (TypeError, ValueError):
            pass
    if preprocess:
        geo = pixel_bbox_to_geo(orig_bbox, preprocess.get('orig_w'), preprocess.get('orig_h'), geo_bbox)
        if geo:
            target["lng"], target["lat"] = round(geo[0], 6), round(geo[1], 6)
    return measure_note, target

# ----------------------
# AI 多轮视觉推理接口 (Qwen3.5-Plus)
# ----------------------
@csrf_exempt
def ai_query_region(request):
    try:
        data = _json_body(request)
        file_name = data.get("file_name")
        file_names = data.get("file_names")
        question = (data.get("question") or "").strip()
        if not question:
            return JsonResponse({"code": 400, "msg": "问题不能为空", "data": None}, status=400)
        front_history = data.get("history", [])
        if not isinstance(front_history, list):
            front_history = []
        mode = data.get("mode", "precise")
        if mode not in ANALYSIS_MODES:
            mode = "precise"
        mode_cfg = ANALYSIS_MODES[mode]
        model_name = mode_cfg["model"]
        gsd = data.get("gsd")              # 米/像素(用于 GSD 测量)
        geo_bbox = data.get("bbox")        # 图像地理范围 {min_lng,max_lng,min_lat,max_lat}(用于坐标接地)
        scene = None

        dashscope.api_key = os.environ.get('DASHSCOPE_API_KEY', '')
        if not dashscope.api_key:
            return JsonResponse({"code": 500, "msg": "缺少 DASHSCOPE_API_KEY，请先在 .env 中配置", "data": None}, status=500)

        SYSTEM_PROMPT = """你是SatelliteSense，顶级的遥感图像分析专家。精通地理学、城市规划、农学、水文学等多领域。

## 回答原则
- 专业客观，使用遥感标准术语
- 用数据说话（如"约30%植被覆盖"）
- 根据问题灵活组织：开放性分析可分段阐述，具体问题直接精准回答
- 中文回答，简洁有力"""

        max_dim = MAX_DIM_MAP.get(model_name, 2560)

        used_ap = False          # 是否走了主动感知两阶段(question 已嵌入 prompt)
        ap_scale = 1.0           # stage1 缩略图 → 原图的 bbox 缩放比
        targets = []             # AI 定位到的目标(含 GSD 测量 + 经纬度),回传前端标点
        preprocess = None        # 单图/分块预处理结果(compare 路径为 None)
        strategy = None
        imagery_quality = None

        # 多图对比
        if file_names and isinstance(file_names, list) and len(file_names) >= 2:
            labels = "ABCDEFGH"
            content_parts = []
            for i, fn in enumerate(file_names):
                fp = safe_media_path(SAVE_DIR, fn, ('.jpg', '.jpeg', '.png'))
                if fp and os.path.exists(fp):
                    label = labels[i] if i < len(labels) else f"区域{i+1}"
                    content_parts.append({"image": f"file://{fp}"})
                    content_parts.append({"text": f"这是区域 {label}。"})
            if sum(1 for p in content_parts if "image" in p) < 2:
                return JsonResponse({"code": 400, "msg": "至少需要 2 张有效图片", "data": None}, status=400)
            content_parts.append({"text": f"{SYSTEM_PROMPT}\n\n共有 {len(file_names)} 个区域的遥感卫星影像，请对比分析。"})
            messages = [{"role": "user", "content": content_parts}]
        # 单图
        else:
            if not file_name:
                return JsonResponse({"code": 400, "msg": "缺少图片标识", "data": None}, status=400)
            target_path = safe_media_path(SAVE_DIR, file_name, ('.jpg', '.jpeg', '.png'))
            if not target_path or not os.path.exists(target_path):
                return JsonResponse({"code": 404, "msg": "卫星图文件已丢失，请重新框选", "data": None}, status=404)

            scene_id = data.get("scene_id")
            if scene_id:
                scene = ImageryScene.objects.filter(id=scene_id, file_name=os.path.basename(file_name)).first()
            if not scene:
                scene = ImageryScene.objects.filter(file_name=os.path.basename(file_name)).first()
            if scene:
                if not gsd and scene.gsd_m:
                    gsd = scene.gsd_m
                if not isinstance(geo_bbox, dict):
                    geo_bbox = {
                        "min_lng": scene.min_lng,
                        "min_lat": scene.min_lat,
                        "max_lng": scene.max_lng,
                        "max_lat": scene.max_lat,
                    }

            spatial_ctx = data.get("spatial_context", "")
            requested_active = _as_bool(data.get("active_perception", mode_cfg["active_perception"]))
            strategy = build_analysis_strategy(question, scene=scene, gsd=gsd, requested_active=requested_active)
            imagery_quality = imagery_quality_payload(scene) if scene else None
            confidence = analysis_confidence_payload(strategy, imagery_quality)
            source_recommendation = source_recommendation_payload(strategy, imagery_quality, question)
            context_bits = []
            context_bits.append(strategy["prompt"])
            scene_context = imagery_context_text(scene)
            if scene_context:
                context_bits.append(scene_context)
            confidence_context = analysis_confidence_text(confidence)
            if confidence_context:
                context_bits.append(confidence_context)
            context_bits.append(
                "## 图像源选择建议\n"
                f"建议：{source_recommendation.get('recommended_label')}\n"
                f"原因：{source_recommendation.get('reason')}\n"
                f"当前状态：{source_recommendation.get('action')}"
            )
            if gsd:
                context_bits.append(f"GSD: {gsd} m/像素")
            if isinstance(geo_bbox, dict):
                context_bits.append(
                    "bbox: "
                    f"min_lng={geo_bbox.get('min_lng')}, max_lng={geo_bbox.get('max_lng')}, "
                    f"min_lat={geo_bbox.get('min_lat')}, max_lat={geo_bbox.get('max_lat')}"
                )
            if context_bits:
                spatial_ctx = (spatial_ctx + "\n" if spatial_ctx else "") + "\n".join(context_bits)
            use_active_perception = strategy["active_perception"]

            preprocess = smart_prepare_image_v2(target_path, max_dim=max_dim, question=question)
            if not preprocess:
                return JsonResponse({"code": 500, "msg": "图像预处理失败", "data": None}, status=500)

            if "single" in preprocess:
                if use_active_perception:
                    messages, ap_scale = _build_active_perception_single(
                        preprocess['single'], target_path, question,
                        spatial_ctx, SYSTEM_PROMPT, preprocess['orig_w']
                    )
                    used_ap = True
                else:
                    effective_spatial = ""
                    if preprocess["eff_w"] != preprocess["orig_w"]:
                        ratio_px = preprocess["eff_w"] / preprocess["orig_w"]
                        effective_spatial = f"（原始 {preprocess['orig_w']}×{preprocess['orig_h']} px，已优化缩放至 {preprocess['eff_w']}×{preprocess['eff_h']} px）"
                    first_msg = SYSTEM_PROMPT + "\n\n---\n\n请分析这张遥感卫星影像。"
                    if spatial_ctx:
                        first_msg += "\n\n## 空间上下文（供参考）\n" + spatial_ctx
                    if effective_spatial:
                        first_msg += "\n" + effective_spatial
                    messages = [{
                        "role": "user",
                        "content": [{"image": f"file://{preprocess['single']}"}, {"text": first_msg}]
                    }]
            else:
                tiles = preprocess["tiles"]
                grid = preprocess["grid"]
                was_smart = preprocess.get("_smart_select", False)

                if use_active_perception:
                    messages, ap_scale = _build_active_perception_tiled(
                        tiles, grid, preprocess, target_path,
                        question, spatial_ctx, SYSTEM_PROMPT, preprocess['orig_w']
                    )
                    used_ap = True
                else:
                    content_parts = []
                    overview = tiles[0]
                    content_parts.append({"image": f"file://{overview}"})
                    content_parts.append({"text": "这是该区域的概览缩略图。"})
                    labels = "ABCDEFGHIJKLMNOP"
                    for i, tp in enumerate(tiles[1:]):
                        label = labels[i] if i < len(labels) else str(i + 1)
                        content_parts.append({"image": f"file://{tp}"})
                        content_parts.append({"text": f"这是分块 {label}（{grid[0]}×{grid[1]} 网格中的一块，{preprocess['eff_w']}×{preprocess['eff_h']} px 原始分辨率）。"})
                    first_msg = SYSTEM_PROMPT
                    smart_note = ""
                    if was_smart:
                        smart_note = f"\n\n（系统已根据你的问题智能筛选了 {preprocess['_selected_count']} 个相关分块（共 {preprocess['_total_tiles']} 块），以加快分析速度。）"
                    first_msg += f"\n\n---\n\n这是一张超大遥感影像（原始 {preprocess['orig_w']}×{preprocess['orig_h']} px），已分割为 1 张概览图 + {len(tiles)-1} 个 {preprocess['eff_w']}×{preprocess['eff_h']} px 分块（{grid[0]}×{grid[1]} 网格）。请结合概览图的整体布局和各分块的原始细节，综合分析这片区域。{smart_note}"
                    if spatial_ctx:
                        first_msg += "\n\n## 空间上下文（供参考）\n" + spatial_ctx
                    content_parts.append({"text": first_msg})
                    messages = [{"role": "user", "content": content_parts}]

        history_msgs = []
        for msg in front_history:
            if not isinstance(msg, dict):
                continue
            role = "user" if msg.get("role") == "user" else "assistant"
            history_msgs.append({"role": role, "content": [{"text": str(msg.get("content", ""))}]})

        if used_ap:
            # 主动感知:question 已在 stage1 prompt 内,该 prompt 必须是最后一条 user 消息,
            # 历史插在它之前;不再单独追加裸 question(否则会架空 <think>/<answer> 格式约束)。
            messages = history_msgs + messages
        else:
            # 普通路径:图片消息在前,历史居中,当前问题单独作为最后一条 user 消息。
            messages = messages + history_msgs + [{"role": "user", "content": [{"text": question}]}]

        response = _call_qwen(model_name, messages)

        if response.status_code != HTTPStatus.OK:
            return JsonResponse({"code": 500, "msg": f"AI 调用失败: {response.message}", "data": None}, status=500)

        content = response.output.choices[0].message.content
        if isinstance(content, list):
            stage1_text = content[0].get('text', '')
        else:
            stage1_text = content

        # Active Perception 迭代放大(ZoomEye 式):模型逐级决定是否继续放大,最多 MAX_ZOOM_LEVELS 级
        active_stages = 1
        answer_parse = normalize_model_answer(stage1_text)
        real_answer = answer_parse["answer"]
        output_quality = {
            **answer_parse["quality"],
            "self_check_enabled": bool(data.get("self_check")),
            "self_check_applied": False,
            "stage_count": 1,
        }
        last_target = None
        if used_ap and not file_names and file_name and os.path.exists(target_path):
            # stage1 的 bbox 已是原图坐标(scale_factor=ap_scale)
            next_orig_bboxes = extract_bbox_from_response(stage1_text, scale_factor=ap_scale)
            cur_text = stage1_text
            level = 0
            while next_orig_bboxes and level < MAX_ZOOM_LEVELS:
                orig_bbox = next_orig_bboxes[0]
                # target_path 已是单图分支校验过的安全路径;每级都从原图裁,保留最高分辨率
                crop_path, ob, sw, sh = cut_image_geom(target_path, orig_bbox)
                if not crop_path:
                    break
                level += 1
                measure_note, last_target = _measure_and_locate(orig_bbox, gsd, preprocess, geo_bbox)
                label = f"[{orig_bbox[0]},{orig_bbox[1]}]-[{orig_bbox[2]},{orig_bbox[3]}]"
                zoom_prompt = build_stage2_prompt(question, cur_text, label)
                if measure_note:
                    zoom_prompt += f"\n\n## 尺度参考\n{measure_note}"
                if level < MAX_ZOOM_LEVELS:
                    zoom_prompt += "\n\n若放大后仍看不清关键细节,可在 <think> 内再给一个 bbox_2d 继续放大;已看清则直接给 <answer>。"
                messages.append({"role": "user", "content": [{"image": f"file://{crop_path}"}, {"text": zoom_prompt}]})

                resp = _call_qwen(model_name, messages)
                if resp.status_code != HTTPStatus.OK:
                    break
                c = resp.output.choices[0].message.content
                cur_text = c[0].get('text', '') if isinstance(c, list) else c
                answer_parse = normalize_model_answer(cur_text)
                real_answer = answer_parse["answer"]
                stage_warnings = list(output_quality.get("warnings") or [])
                stage_warnings.extend(answer_parse["quality"].get("warnings") or [])
                output_quality.update(answer_parse["quality"])
                output_quality["warnings"] = stage_warnings
                active_stages = level + 1   # 1(stage1) + 已放大级数
                output_quality["stage_count"] = active_stages

                # 模型是否要求再放大?返回的 bbox 是相对【当前裁剪图】的,映射回原图坐标
                view_bboxes = extract_bbox_from_response(cur_text)
                next_orig_bboxes = [map_bbox_to_original(view_bboxes[0], ob, sw, sh)] if view_bboxes else []
            if last_target:
                targets.append(last_target)

        # P2.2 自一致性校验(默认关,前端传 self_check=true 才开):放大结论对照全局自检
        if data.get("self_check") and active_stages >= 2:
            check_messages = messages + [{"role": "user", "content": [{"text": "请对照整体场景,自检你上面的结论有无与全局明显矛盾之处。若一致,简要确认并给出最终结论;若有偏差请修正。直接输出最终结论。"}]}]
            resp_c = _call_qwen(model_name, check_messages)
            if resp_c.status_code == HTTPStatus.OK:
                cc = resp_c.output.choices[0].message.content
                ct = cc[0].get('text', '') if isinstance(cc, list) else cc
                checked_parse = normalize_model_answer(ct)
                if checked_parse["answer"]:
                    real_answer = checked_parse["answer"]
                    output_quality["self_check_applied"] = True
                    check_warnings = list(output_quality.get("warnings") or [])
                    check_warnings.extend(checked_parse["quality"].get("warnings") or [])
                    output_quality["warnings"] = check_warnings

        # 把定量信息追加到答案末尾,确保"可测量/可定位"这个卖点在文字里也可见
        if targets:
            t = targets[0]
            bits = []
            if 'width_m' in t:
                bits.append(f"尺寸约 {t['width_m']}m × {t['height_m']}m")
            if 'area_m2' in t:
                a = t['area_m2']
                bits.append(f"占地约 {a/10000:.2f} 公顷" if a >= 10000 else f"占地约 {a:.0f} m²")
            if 'lat' in t:
                bits.append(f"中心约 {t['lat']}°N, {t['lng']}°E")
            if bits:
                real_answer += "\n\n📐 **定量信息**（基于 GSD 测算）：" + " · ".join(bits)

        return JsonResponse({
            "code": 200,
            "msg": "success",
            "data": {
                "answer": real_answer,
                "active_stages": active_stages,
                "targets": targets,
                "scene": scene_payload(scene) if scene else None,
                "analysis_strategy": strategy,
                "analysis_method": analysis_method_payload(
                    mode,
                    model_name,
                    strategy,
                    active_stages,
                    imagery_quality,
                    question,
                    output_quality,
                ),
            }
        })

    except Exception as e:
        return JsonResponse({"code": 400, "msg": f"系统异常: {str(e)}", "data": None}, status=400)


# ----------------------
# 地理搜索（高德 POI 搜索，国内网络原生支持）
# ----------------------
AMAP_KEY = os.environ.get('AMAP_KEY', '')

def geo_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 1:
        return JsonResponse({"code": 400, "msg": "请输入搜索关键词", "data": []}, status=400)

    if not AMAP_KEY:
        return JsonResponse({"code": 500, "msg": "请先在 views.py 中配置 AMAP_KEY（免费获取: https://lbs.amap.com/）", "data": []}, status=500)

    try:
        proxies = {"http": None, "https": None}
        url = "https://restapi.amap.com/v3/place/text"
        params = {"keywords": q, "key": AMAP_KEY, "offset": 8, "extensions": "base"}
        resp = requests.get(url, params=params, timeout=8, proxies=proxies)
        data = resp.json()
        results = []
        for poi in data.get("pois", []):
            loc = poi.get("location", "0,0").split(",")
            results.append({
                "name": poi.get("name", ""),
                "display_name": f"{poi.get('pname', '')}{poi.get('cityname', '')}{poi.get('adname', '')}{poi.get('address', '')}",
                "lat": float(loc[1]) if len(loc) == 2 else 0,
                "lon": float(loc[0]) if len(loc) == 2 else 0,
                "type": poi.get("typecode", ""),
            })
        return JsonResponse({"code": 200, "data": results})
    except Exception as e:
        return JsonResponse({"code": 500, "msg": str(e), "data": []}, status=500)


# ----------------------
# 下载进度查询
# ----------------------
def get_progress(request):
    file_name = request.GET.get("file", "")
    info = get_download_progress(file_name)
    if not info:
        try:
            info = _task_progress(DownloadTask.objects.get(file_name=os.path.basename(file_name or "")))
        except DownloadTask.DoesNotExist:
            return JsonResponse({"code": 404, "msg": "no progress found"}, status=404)
    return JsonResponse({"code": 200, "data": info})


def cleanup_media_core(days=7, clean_all=False, dry_run=False):
    """删除过期媒体文件并同步清理相关 DB 记录,返回统计。
    cleanup_cache 视图与 cleanup_media 管理命令共用。dry_run 只统计不删除。"""
    cutoff = timezone.now() - timedelta(days=max(0, days))
    cutoff_ts = cutoff.timestamp()

    deleted_files = 0
    freed_bytes = 0
    deleted_image_files = []
    roots = [SAVE_DIR, REPORT_DIR]
    report_ext = (".docx",)
    image_ext = (".jpg", ".jpeg", ".png")
    save_real = os.path.realpath(SAVE_DIR)

    for root in roots:
        if not os.path.isdir(root):
            continue
        root_real = os.path.realpath(root)
        for name in os.listdir(root):
            path = os.path.realpath(os.path.join(root, name))
            if not path.startswith(root_real + os.sep):
                continue
            if os.path.isdir(path):
                continue
            if root == REPORT_DIR and not name.lower().endswith(report_ext):
                continue
            if clean_all or os.path.getmtime(path) < cutoff_ts:
                size = os.path.getsize(path)
                if not dry_run:
                    os.remove(path)
                deleted_files += 1
                freed_bytes += size
                if root_real == save_real and name.lower().endswith(image_ext):
                    deleted_image_files.append(name)

    if not dry_run:
        if clean_all:
            DownloadTask.objects.all().delete()
            ImageryScene.objects.all().delete()
            ChatHistory.objects.all().delete()
            _download_progress.clear()
        else:
            if deleted_image_files:
                DownloadTask.objects.filter(file_name__in=deleted_image_files).delete()
                ImageryScene.objects.filter(file_name__in=deleted_image_files).delete()
                ChatHistory.objects.filter(image_file__in=deleted_image_files).delete()
            DownloadTask.objects.filter(updated_at__lt=cutoff).delete()
            for key, info in list(_download_progress.items()):
                if info.get("status") in ("done", "partial", "error"):
                    _download_progress.pop(key, None)

    return {
        "deleted_files": deleted_files,
        "deleted_image_records": len(deleted_image_files),
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "dry_run": dry_run,
    }


@csrf_exempt
def cleanup_cache(request):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        data = _json_body(request)
        days = int(data.get("days", 7))
        clean_all = _as_bool(data.get("all", False))
        return JsonResponse({"code": 200, "data": cleanup_media_core(days, clean_all)})
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e)}, status=400)


def imagery_scene_list(request):
    limit = min(100, max(1, int(request.GET.get("limit", 20))))
    qs = ImageryScene.objects.all()[:limit]
    return JsonResponse({"code": 200, "data": [scene_payload(scene) for scene in qs]})


def imagery_scene_detail(request, scene_id):
    try:
        scene = ImageryScene.objects.get(id=scene_id)
        return JsonResponse({"code": 200, "data": scene_payload(scene)})
    except ImageryScene.DoesNotExist:
        return JsonResponse({"code": 404, "msg": "not found"}, status=404)


def imagery_search(request):
    try:
        min_lng, min_lat, max_lng, max_lat = normalize_bbox(request.GET)
        limit = min(20, max(1, int(request.GET.get("limit", 10))))
        max_cloud = request.GET.get("max_cloud", 30)
        max_cloud = None if max_cloud == "" else float(max_cloud)
        provider_name = request.GET.get("provider", "earth_search")
        collection = request.GET.get("collection", "sentinel-2-l2a")
        start_date = request.GET.get("start")
        end_date = request.GET.get("end")

        if provider_name != "earth_search":
            return JsonResponse({"code": 400, "msg": "unsupported provider"}, status=400)

        bbox = {"min_lng": min_lng, "min_lat": min_lat, "max_lng": max_lng, "max_lat": max_lat}
        candidates = EarthSearchProvider().search(
            bbox,
            start_date=start_date,
            end_date=end_date,
            max_cloud=max_cloud,
            limit=limit,
            collection=collection,
        )
        return JsonResponse({
            "code": 200,
            "data": {
                "provider": provider_name,
                "collection": collection,
                "bbox": bbox,
                "count": len(candidates),
                "candidates": [candidate.as_dict() for candidate in candidates],
            }
        })
    except requests.RequestException as e:
        logger.warning("imagery search provider failed: %s", e)
        return JsonResponse({"code": 502, "msg": "影像源查询失败，请稍后重试"}, status=502)
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e)}, status=400)


@csrf_exempt
def imagery_recommend_source(request):
    if request.method not in ("GET", "POST"):
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        data = _json_body(request) if request.method == "POST" else request.GET
        question = (data.get("question") or "").strip()
        if not question:
            return JsonResponse({"code": 400, "msg": "问题不能为空", "data": None}, status=400)
        current_source = (data.get("current_source") or data.get("source") or "mapbox").strip().lower()
        if current_source == "earth_search":
            current_source = "sentinel2"
        if current_source not in ("mapbox", "sentinel2"):
            current_source = "unknown"

        scene = type("Scene", (), {
            "source": current_source,
            "gsd_m": 10 if current_source == "sentinel2" else 1.2,
        })()
        strategy = build_analysis_strategy(question, scene=scene, requested_active=True)
        recommendation = source_recommendation_payload(strategy, {"source": current_source}, question)
        return JsonResponse({
            "code": 200,
            "data": {
                "question": question,
                "current_source": current_source,
                "recommendation": recommendation,
                "task": strategy.get("task_profile"),
                "query": strategy.get("query"),
            }
        })
    except Exception as e:
        return JsonResponse({"code": 400, "msg": str(e), "data": None}, status=400)


# ----------------------
# 聊天历史 CRUD
# ----------------------
@csrf_exempt
def chat_history_list(request):
    if request.method == "GET":
        histories = []
        for obj in ChatHistory.objects.all()[:100]:
            if not history_image_available(obj.image_file):
                continue
            histories.append(chat_history_payload(obj))
            if len(histories) >= 20:
                break
        return JsonResponse({"code": 200, "data": histories})
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            image_file = os.path.basename(data.get("image_file", "") or "")
            if not image_file or image_file == "__compare__":
                return JsonResponse({"code": 400, "msg": "invalid image_file"}, status=400)
            if not image_file.lower().endswith(('.jpg', '.jpeg', '.png')):
                return JsonResponse({"code": 400, "msg": "invalid image_file"}, status=400)
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                return JsonResponse({"code": 400, "msg": "invalid messages"}, status=400)
            if not history_image_available(image_file):
                return JsonResponse({"code": 410, "msg": "历史影像文件已丢失，请重新框选分析"}, status=410)
            scene = None
            if data.get("scene_id"):
                scene = ImageryScene.objects.filter(id=data.get("scene_id"), file_name=image_file).first()
            if not scene:
                scene = ImageryScene.objects.filter(file_name=image_file).first()

            latest = ChatHistory.objects.filter(image_file=image_file).order_by("-updated_at").first()
            if latest:
                ChatHistory.objects.filter(image_file=image_file).exclude(id=latest.id).delete()
            obj, created = ChatHistory.objects.update_or_create(
                image_file=image_file,
                defaults={
                    "scene": scene,
                    "messages": messages,
                    "spatial_context": data.get("spatial_context", ""),
                    "bbox": data.get("bbox", None),
                }
            )
            return JsonResponse({"code": 200, "data": {"id": obj.id, "created": created}})
        except Exception as e:
            return JsonResponse({"code": 400, "msg": str(e)}, status=400)
    return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)


@csrf_exempt
def chat_history_detail(request, history_id):
    if request.method == "GET":
        try:
            obj = ChatHistory.objects.get(id=history_id)
            if not history_image_available(obj.image_file):
                return JsonResponse({"code": 410, "msg": "历史影像文件已丢失，请重新框选分析"}, status=410)
            return JsonResponse({"code": 200, "data": {
                "id": obj.id, "image_file": obj.image_file,
                "messages": obj.messages, "spatial_context": obj.spatial_context,
                "bbox": obj.bbox, "scene_id": obj.scene_id,
                "scene": scene_payload(obj.scene) if obj.scene else None,
                "image_available": True,
                "created_at": obj.created_at.isoformat(),
            }})
        except ChatHistory.DoesNotExist:
            return JsonResponse({"code": 404, "msg": "not found"}, status=404)
    if request.method == "DELETE":
        ChatHistory.objects.filter(id=history_id).delete()
        return JsonResponse({"code": 200, "msg": "deleted"})
    return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)


# ----------------------
# 分析报告生成（Word）
# ----------------------
@csrf_exempt
def generate_report(request):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        data = _json_body(request)
        file_name = os.path.basename(data.get("file_name", "") or "")
        if file_name == "__compare__":
            file_name = ""
        if file_name and not file_name.lower().endswith(('.jpg', '.jpeg', '.png')):
            return JsonResponse({"code": 400, "msg": "invalid file_name"}, status=400)
        if file_name and not history_image_available(file_name):
            return JsonResponse({"code": 410, "msg": "卫星图文件已丢失，请重新框选"}, status=410)
        title = docx_safe_text(data.get("title", "遥感分析报告"))
        messages = data.get("messages", [])
        spatial_ctx = docx_safe_text(data.get("spatial_context", ""))
        bbox = data.get("bbox", {})
        scene = None
        if data.get("scene_id"):
            scene_qs = ImageryScene.objects.filter(id=data.get("scene_id"))
            if file_name:
                scene_qs = scene_qs.filter(file_name=file_name)
            scene = scene_qs.first()
        if not scene and file_name:
            scene = ImageryScene.objects.filter(file_name=os.path.basename(file_name)).first()

        from docx import Document
        from docx.shared import Inches, Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        import datetime

        doc = Document()

        for section in doc.sections:
            section.top_margin = Cm(2)
            section.bottom_margin = Cm(2)
            section.left_margin = Cm(2.5)
            section.right_margin = Cm(2.5)

        style = doc.styles['Normal']
        style.font.name = 'Microsoft YaHei'
        style.font.size = Pt(11)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing = 1.25

        for i in range(1, 4):
            hs = doc.styles[f'Heading {i}']
            hs.font.name = 'Microsoft YaHei'
            hs.font.color.rgb = RGBColor(30, 30, 30)
            hs.font.bold = False
            if i == 1:
                hs.font.size = Pt(22)
            elif i == 2:
                hs.font.size = Pt(14)
            else:
                hs.font.size = Pt(12)

        t = doc.styles['Title']
        t.font.name = 'Microsoft YaHei'
        t.font.size = Pt(26)
        t.font.bold = False
        t.font.color.rgb = RGBColor(30, 30, 30)

        h = doc.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p = doc.add_paragraph(f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if bbox and bbox.get("min_lng"):
            doc.add_heading("区域坐标 / 空间数据", level=2)
            loc_text = f"经度：{bbox.get('min_lng', '?')} ~ {bbox.get('max_lng', '?')}  纬度：{bbox.get('min_lat', '?')} ~ {bbox.get('max_lat', '?')}"
            if spatial_ctx:
                loc_text += f"\n{spatial_ctx}"
            doc.add_paragraph(docx_safe_text(loc_text))
        elif spatial_ctx:
            doc.add_heading("空间数据", level=2)
            doc.add_paragraph(docx_safe_text(spatial_ctx))

        if scene:
            doc.add_heading("影像数据说明", level=2)
            acquired = scene.acquired_at.strftime("%Y-%m-%d %H:%M") if scene.acquired_at else "未知"
            published = scene.published_at.strftime("%Y-%m-%d %H:%M") if scene.published_at else "未知"
            cloud = f"{scene.cloud_percent}%" if scene.cloud_percent is not None else "未知"
            quality = imagery_quality_payload(scene) or {}
            quality_text = ""
            if quality:
                quality_text = (
                    f"\n质量摘要：{quality.get('summary', '')}"
                    f"\n时效性：{quality.get('timeliness', '')}"
                    f"\n云量质量：{quality.get('cloud_quality', '')}"
                    f"\n适合任务：{quality.get('best_for', '')}"
                    f"\n空间尺度：{quality.get('spatial_resolution', '')}"
                    f"\n使用提醒：{join_method_items(quality.get('cautions'))}"
                )
            metadata = scene.metadata or {}
            selection_text = ""
            if metadata.get("selection_method") or metadata.get("suitability_score") is not None:
                selection_text = (
                    f"\n候选优选方法：{metadata.get('selection_method', '未知')}"
                    f"\n候选优选分数：{metadata.get('suitability_score', '未知')}"
                )
                if metadata.get("candidate_count"):
                    selection_text += f"\n候选池数量：{metadata.get('candidate_count')}"
                if metadata.get("selection_rank"):
                    selection_text += f"\n最终采用排序：第 {metadata.get('selection_rank')} 个可渲染候选"
                reasons = join_method_items(metadata.get("score_reasons"))
                if reasons:
                    selection_text += f"\n候选优选依据：{reasons}"
                render_errors = join_method_items(metadata.get("render_fallback_errors"))
                if render_errors:
                    selection_text += f"\n候选渲染降级记录：{render_errors}"
            doc.add_paragraph(docx_safe_text(
                f"数据源：{scene.source_label}\n"
                f"拍摄时间：{acquired}\n"
                f"发布时间：{published}\n"
                f"空间分辨率：约 {scene.gsd_m} m/像素\n"
                f"云量：{cloud}\n"
                f"处理级别：{scene.processing_level}\n"
                f"授权类型：{scene.license_type}\n"
                f"决策等级：{scene.decision_grade}\n"
                f"数据限制：{scene.limitations}"
                f"{quality_text}"
                f"{selection_text}"
            ))

        analysis_method = latest_analysis_method(messages)
        if analysis_method:
            doc.add_heading("AI 分析方法说明", level=2)
            mode_label = "精准模式" if analysis_method.get("mode") == "precise" else "快速模式"
            active_text = (
                f"启用，{analysis_method.get('active_stages', 1)} 级分析"
                if analysis_method.get("active_perception") else "未启用"
            )
            method_lines = [
                f"分析模式：{mode_label}",
                f"视觉模型：{docx_safe_text(analysis_method.get('model', '未知'))}",
                f"任务画像：{docx_safe_text(analysis_method.get('task_label', '综合遥感解译'))}",
                f"图像源类型：{docx_safe_text(analysis_method.get('source', 'unknown'))}",
                f"主动感知：{active_text}",
            ]
            strengths = join_method_items(analysis_method.get("strengths"))
            limits = join_method_items(analysis_method.get("limits"))
            notes = join_method_items(analysis_method.get("method_notes"))
            if strengths:
                method_lines.append("可重点分析：" + strengths)
            if limits:
                method_lines.append("判读边界：" + limits)
            if notes:
                method_lines.append("方法提示：" + notes)
            quality = analysis_method.get("imagery_quality") or {}
            if quality:
                method_lines.append("影像质量摘要：" + docx_safe_text(quality.get("summary", "")))
                method_lines.append("适用任务：" + docx_safe_text(quality.get("best_for", "")))
                cautions = join_method_items(quality.get("cautions"))
                if cautions:
                    method_lines.append("使用提醒：" + cautions)
            confidence = analysis_method.get("confidence") or {}
            if confidence:
                method_lines.append("结论可信度：" + docx_safe_text(confidence.get("label", "未知")))
                basis = join_method_items(confidence.get("basis"))
                checks = join_method_items(confidence.get("required_checks"))
                if basis:
                    method_lines.append("可信度依据：" + basis)
                if checks:
                    method_lines.append("复核要求：" + checks)
            source_recommendation = analysis_method.get("source_recommendation") or {}
            if source_recommendation:
                method_lines.append(
                    "图像源建议：" + docx_safe_text(source_recommendation.get("recommended_label", "未知"))
                )
                method_lines.append("建议原因：" + docx_safe_text(source_recommendation.get("reason", "")))
                method_lines.append("建议动作：" + docx_safe_text(source_recommendation.get("action", "")))
            output_quality = analysis_method.get("output_quality") or {}
            if output_quality:
                output_lines = [
                    "结构化输出：" + ("已按 <answer> 解析" if output_quality.get("structured_answer") else "使用兜底解析"),
                    "自一致性校验：" + (
                        "已执行" if output_quality.get("self_check_applied")
                        else ("已请求但未触发" if output_quality.get("self_check_enabled") else "未启用")
                    ),
                ]
                warnings = join_method_items(output_quality.get("warnings"))
                if warnings:
                    output_lines.append("输出整理提示：" + warnings)
                method_lines.append("输出稳定性：" + "；".join(output_lines))
            doc.add_paragraph(docx_safe_text("\n".join(method_lines)))

        if file_name:
            img_path = safe_media_path(SAVE_DIR, file_name, ('.jpg', '.jpeg', '.png'))
            if img_path and os.path.exists(img_path):
                doc.add_heading("卫星影像", level=2)
                doc.add_picture(img_path, width=Cm(14))
                last_paragraph = doc.paragraphs[-1]
                last_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if messages:
            doc.add_heading("AI 分析对话", level=2)
            for msg in messages:
                role = msg.get("role", "user")
                role_label = "用户" if role == "user" else "AI 助手"
                color = RGBColor(0, 122, 255) if role == "user" else RGBColor(60, 180, 75)
                p = doc.add_paragraph()
                run_label = p.add_run(f"[{role_label}]  ")
                run_label.font.name = 'Microsoft YaHei'
                run_label.font.size = Pt(10.5)
                run_label.font.bold = True
                run_label.font.color.rgb = color
                run_content = p.add_run(docx_safe_text(msg.get("content", "")))
                run_content.font.name = 'Microsoft YaHei'
                run_content.font.size = Pt(10.5)

        report_name = f"report_{uuid.uuid4().hex[:8]}.docx"
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
        doc.save(report_path)

        download_url = f"/api/report/download/?file={report_name}"
        return JsonResponse({"code": 200, "data": {"file_name": report_name, "download_url": download_url}})
    except Exception as e:
        logger.exception("report generation failed")
        return JsonResponse({"code": 500, "msg": str(e)}, status=500)


def download_report(request):
    file_name = request.GET.get("file", "")
    path = safe_media_path(settings.MEDIA_ROOT, file_name, ('.docx',))
    if not path or not os.path.exists(path):
        return JsonResponse({"code": 404, "msg": "not found"}, status=404)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=os.path.basename(path),
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
