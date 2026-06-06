from django.http import JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.shortcuts import render
from http import HTTPStatus
import dashscope
import json
import os
import uuid
import math
import threading
import requests
import logging
import time
from datetime import timedelta
from PIL import Image
from django.utils import timezone
from django.db import close_old_connections

from .utils.get_satellite_image import fetch_satellite_image, haversine_distance, get_download_progress, prune_progress, _download_progress
from .utils.image_preprocessor import smart_prepare_image_v2, MAX_DIM_MAP
from .utils.active_perception import (
    build_stage1_prompt, extract_bbox_from_response,
    cut_image_geom, map_bbox_to_original, resize_image, build_stage2_prompt,
    extract_answer_text, measure_bbox, pixel_bbox_to_geo
)
from .models import ChatHistory, DownloadTask, ImageryScene
from .imagery_sources.mapbox import MapboxProvider
from .imagery_sources.earth_search import EarthSearchProvider

# 规范化保存目录
SAVE_DIR = os.path.join(settings.MEDIA_ROOT, 'satellite_imgs')
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

REPORT_DIR = settings.MEDIA_ROOT
logger = logging.getLogger(__name__)
ai_logger = logging.getLogger("map_api.ai")

# 支持高分辨率图像输入的 VL 模型(需开 vl_high_resolution_images)
VL_MODELS = ('qwen3-vl-plus', 'qwen3-vl-flash', 'qwen-vl-max', 'qwen-vl-plus')
ANALYSIS_MODES = {
    "precise": {"model": "qwen3-vl-plus", "active_perception": True},
    "fast": {"model": "qwen3-vl-flash", "active_perception": False},
}


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


def safe_media_path(base_dir, name, allowed_ext):
    """把用户传入的文件名安全地解析为 base_dir 内的路径,防止 ../ 路径穿越。
    去掉路径成分、校验后缀、并确认最终路径确实落在 base_dir 内;不合法返回 None。"""
    name = os.path.basename(name or '')
    if not name or not name.lower().endswith(allowed_ext):
        return None
    full = os.path.realpath(os.path.join(base_dir, name))
    base_real = os.path.realpath(base_dir)
    if full != base_real and not full.startswith(base_real + os.sep):
        return None
    return full


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise ValueError("请求体不是有效 JSON")


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


def normalize_bbox(data):
    raw_min_lng = float(data['min_lng'])
    raw_min_lat = float(data['min_lat'])
    raw_max_lng = float(data['max_lng'])
    raw_max_lat = float(data['max_lat'])
    min_lng, max_lng = sorted((raw_min_lng, raw_max_lng))
    min_lat, max_lat = sorted((raw_min_lat, raw_max_lat))
    if max_lng - min_lng <= 0 or max_lat - min_lat <= 0:
        raise ValueError("框选区域过小，请重新选择")
    return min_lng, min_lat, max_lng, max_lat


def compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution):
    lon_span = max_lng - min_lng
    lat_span = max_lat - min_lat
    center_lat_rad = math.radians((min_lat + max_lat) / 2)
    target = min(4096, max(1, resolution))
    aspect_ratio = (lon_span * math.cos(center_lat_rad)) / lat_span if lat_span else 1
    if aspect_ratio >= 1:
        total_w = target
        total_h = max(1, int(target / aspect_ratio))
    else:
        total_h = target
        total_w = max(1, int(target * aspect_ratio))
    total_tiles = 1 if total_w <= 1280 and total_h <= 1280 else math.ceil(total_w / 1280) * math.ceil(total_h / 1280)
    gsd_lon = (lon_span * 111320 * math.cos(center_lat_rad)) / total_w
    gsd_lat_val = (lat_span * 110574) / total_h
    area_km2 = (lon_span * 111320 * math.cos(center_lat_rad)) * (lat_span * 110574) / 1e6
    return {
        "total_w": total_w,
        "total_h": total_h,
        "total_tiles": total_tiles,
        "gsd_m": (gsd_lon + gsd_lat_val) / 2,
        "area_km2": area_km2,
    }


def scene_payload(scene):
    return {
        "id": scene.id,
        "file_name": scene.file_name,
        "source": scene.source,
        "source_label": scene.source_label,
        "product_id": scene.product_id,
        "acquired_at": scene.acquired_at.isoformat() if scene.acquired_at else None,
        "published_at": scene.published_at.isoformat() if scene.published_at else None,
        "fetched_at": scene.fetched_at.isoformat() if scene.fetched_at else None,
        "bbox": {
            "min_lng": scene.min_lng,
            "min_lat": scene.min_lat,
            "max_lng": scene.max_lng,
            "max_lat": scene.max_lat,
        },
        "gsd_m": scene.gsd_m,
        "area_km2": scene.area_km2,
        "cloud_percent": scene.cloud_percent,
        "processing_level": scene.processing_level,
        "license_type": scene.license_type,
        "decision_grade": scene.decision_grade,
        "limitations": scene.limitations,
        "metadata": scene.metadata,
    }


def scene_from_candidate(file_name, candidate, bbox, area_km2):
    return ImageryScene.objects.create(
        file_name=file_name,
        source="sentinel2",
        source_label="Sentinel-2 L2A",
        product_id=candidate.product_id,
        acquired_at=candidate.acquired_at,
        published_at=candidate.published_at,
        min_lng=bbox["min_lng"],
        min_lat=bbox["min_lat"],
        max_lng=bbox["max_lng"],
        max_lat=bbox["max_lat"],
        gsd_m=candidate.gsd_m,
        area_km2=round(area_km2, 4),
        cloud_percent=candidate.cloud_percent,
        processing_level=candidate.processing_level,
        license_type=candidate.license_type,
        decision_grade=candidate.decision_grade,
        limitations=candidate.limitations,
        metadata={
            **candidate.metadata,
            "collection": candidate.collection,
            "item_id": candidate.item_id,
            "suitability_score": candidate.suitability_score,
            "score_reasons": candidate.score_reasons,
            "assets": candidate.assets,
            "links": candidate.links,
            "rendered_by": "titiler",
        },
    )


def imagery_context_text(scene):
    if not scene:
        return ""
    acquired = scene.acquired_at.strftime("%Y-%m-%d %H:%M") if scene.acquired_at else "未知"
    cloud = f"{scene.cloud_percent}%" if scene.cloud_percent is not None else "未知"
    return (
        "## 影像元数据\n"
        f"数据源：{scene.source_label}\n"
        f"拍摄时间：{acquired}\n"
        f"GSD：约 {scene.gsd_m} m/像素\n"
        f"云量：{cloud}\n"
        f"处理级别：{scene.processing_level}\n"
        f"决策等级：{scene.decision_grade}\n"
        f"数据限制：{scene.limitations}\n"
        "回答时必须基于上述数据限制说明不确定性，不得把参考级底图结论表述为已复核证据。"
    )


def index_view(request):
    """负责展示前端地图页面"""
    return render(request, 'browser.html')

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
        max_cloud = float(data.get("max_cloud", 30))
        start_date = data.get("start")
        end_date = data.get("end")
        resolution = min(2048, max(256, int(data.get("target_resolution", 1024))))
        bbox = {"min_lng": min_lng, "min_lat": min_lat, "max_lng": max_lng, "max_lat": max_lat}
        plan = compute_image_plan(min_lng, min_lat, max_lng, max_lat, resolution)
        provider = EarthSearchProvider(titiler_endpoint=os.environ.get("TITILER_ENDPOINT", None) or None)
        candidates = provider.search(
            bbox,
            start_date=start_date,
            end_date=end_date,
            max_cloud=max_cloud,
            limit=1,
            collection="sentinel-2-l2a",
        )
        if not candidates:
            return JsonResponse({"code": 404, "msg": "未找到符合条件的 Sentinel-2 影像", "data": None}, status=404)

        candidate = candidates[0]
        image_bytes = provider.render_candidate_jpeg(candidate, bbox, plan["total_w"], plan["total_h"])
        file_name = f"sentinel_{uuid.uuid4().hex[:8]}.jpg"
        full_path = os.path.join(SAVE_DIR, file_name)
        scene = None
        with open(full_path, "wb") as f:
            f.write(image_bytes)
        try:
            scene = scene_from_candidate(file_name, candidate, bbox, plan["area_km2"])
            DownloadTask.objects.create(
                scene=scene,
                file_name=file_name,
                status="done",
                total=1,
                done=1,
                failed=0,
                min_lng=min_lng,
                min_lat=min_lat,
                max_lng=max_lng,
                max_lat=max_lat,
                gsd_m=candidate.gsd_m,
                area_km2=round(plan["area_km2"], 4),
                resolution_px=resolution,
            )
        except Exception:
            if os.path.exists(full_path):
                os.remove(full_path)
            if scene:
                scene.delete()
            raise
        _download_progress[file_name] = {"total": 1, "done": 1, "failed": 0, "status": "done", "scene_id": scene.id}

        return JsonResponse({
            "code": 200,
            "msg": "Sentinel-2 影像已生成",
            "data": {
                "file_name": file_name,
                "total_tiles": 1,
                "scene_id": scene.id,
                "scene": scene_payload(scene),
                "candidate": candidate.as_dict(),
                "gsd_m": candidate.gsd_m,
                "area_km2": round(plan["area_km2"], 4),
                "resolution_px": resolution,
            }
        })
    except requests.RequestException as e:
        logger.warning("sentinel image provider failed: %s", e)
        return JsonResponse({"code": 502, "msg": "Sentinel-2 影像源或渲染服务暂时不可用", "data": None}, status=502)
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
        mode_cfg = ANALYSIS_MODES.get(mode, ANALYSIS_MODES["precise"])
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
                return JsonResponse({"code": 400, "msg": "至少需要 2 张有效图片", "data": None})
            content_parts.append({"text": f"{SYSTEM_PROMPT}\n\n共有 {len(file_names)} 个区域的遥感卫星影像，请对比分析。"})
            messages = [{"role": "user", "content": content_parts}]
        # 单图
        else:
            if not file_name:
                return JsonResponse({"code": 400, "msg": "缺少图片标识", "data": None})
            target_path = safe_media_path(SAVE_DIR, file_name, ('.jpg', '.jpeg', '.png'))
            if not target_path or not os.path.exists(target_path):
                return JsonResponse({"code": 404, "msg": "卫星图文件已丢失，请重新框选", "data": None})

            scene_id = data.get("scene_id")
            if scene_id:
                scene = ImageryScene.objects.filter(id=scene_id, file_name=os.path.basename(file_name)).first()
            if not scene:
                scene = ImageryScene.objects.filter(file_name=os.path.basename(file_name)).first()

            spatial_ctx = data.get("spatial_context", "")
            context_bits = []
            scene_context = imagery_context_text(scene)
            if scene_context:
                context_bits.append(scene_context)
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
            use_active_perception = _as_bool(data.get("active_perception", mode_cfg["active_perception"]))

            preprocess = smart_prepare_image_v2(target_path, max_dim=max_dim, question=question)
            if not preprocess:
                return JsonResponse({"code": 500, "msg": "图像预处理失败", "data": None})

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
        real_answer = extract_answer_text(stage1_text)
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
                real_answer = extract_answer_text(cur_text)
                active_stages = level + 1   # 1(stage1) + 已放大级数

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
                checked = extract_answer_text(ct)
                if checked:
                    real_answer = checked

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
        return JsonResponse({"code": 400, "msg": "请输入搜索关键词", "data": []})

    if not AMAP_KEY:
        return JsonResponse({"code": 500, "msg": "请先在 views.py 中配置 AMAP_KEY（免费获取: https://lbs.amap.com/）", "data": []})

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
        return JsonResponse({"code": 500, "msg": str(e), "data": []})


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
            return JsonResponse({"code": 404, "msg": "no progress found"})
    return JsonResponse({"code": 200, "data": info})


@csrf_exempt
def cleanup_cache(request):
    if request.method != "POST":
        return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)
    try:
        data = _json_body(request)
        days = int(data.get("days", 7))
        clean_all = _as_bool(data.get("all", False))
        cutoff = timezone.now() - timedelta(days=max(0, days))
        cutoff_ts = cutoff.timestamp()

        deleted_files = 0
        freed_bytes = 0
        roots = [SAVE_DIR, REPORT_DIR]
        report_ext = (".docx",)

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
                    os.remove(path)
                    deleted_files += 1
                    freed_bytes += size

        if clean_all:
            DownloadTask.objects.all().delete()
            ImageryScene.objects.all().delete()
            _download_progress.clear()
        else:
            DownloadTask.objects.filter(updated_at__lt=cutoff).delete()
            for key, info in list(_download_progress.items()):
                if info.get("status") in ("done", "partial", "error"):
                    _download_progress.pop(key, None)

        return JsonResponse({
            "code": 200,
            "data": {
                "deleted_files": deleted_files,
                "freed_mb": round(freed_bytes / (1024 * 1024), 2),
            }
        })
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


# ----------------------
# 聊天历史 CRUD
# ----------------------
@csrf_exempt
def chat_history_list(request):
    if request.method == "GET":
        histories = ChatHistory.objects.values(
            "id", "scene_id", "image_file", "spatial_context", "bbox", "created_at", "updated_at"
        )[:20]
        return JsonResponse({"code": 200, "data": list(histories)})
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            image_file = os.path.basename(data.get("image_file", "") or "")
            if not image_file or image_file == "__compare__":
                return JsonResponse({"code": 400, "msg": "invalid image_file"})
            if not image_file.lower().endswith(('.jpg', '.jpeg', '.png')):
                return JsonResponse({"code": 400, "msg": "invalid image_file"})
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                return JsonResponse({"code": 400, "msg": "invalid messages"})
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
            return JsonResponse({"code": 400, "msg": str(e)})
    return JsonResponse({"code": 405, "msg": "method not allowed"}, status=405)


@csrf_exempt
def chat_history_detail(request, history_id):
    if request.method == "GET":
        try:
            obj = ChatHistory.objects.get(id=history_id)
            return JsonResponse({"code": 200, "data": {
                "id": obj.id, "image_file": obj.image_file,
                "messages": obj.messages, "spatial_context": obj.spatial_context,
                "bbox": obj.bbox, "scene_id": obj.scene_id,
                "scene": scene_payload(obj.scene) if obj.scene else None,
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
    try:
        data = _json_body(request)
        file_name = data.get("file_name", "")
        title = data.get("title", "遥感分析报告")
        messages = data.get("messages", [])
        spatial_ctx = data.get("spatial_context", "")
        bbox = data.get("bbox", {})
        scene = None
        if data.get("scene_id"):
            scene = ImageryScene.objects.filter(id=data.get("scene_id")).first()
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
            doc.add_paragraph(loc_text)
        elif spatial_ctx:
            doc.add_heading("空间数据", level=2)
            doc.add_paragraph(spatial_ctx)

        if scene:
            doc.add_heading("影像数据说明", level=2)
            acquired = scene.acquired_at.strftime("%Y-%m-%d %H:%M") if scene.acquired_at else "未知"
            published = scene.published_at.strftime("%Y-%m-%d %H:%M") if scene.published_at else "未知"
            cloud = f"{scene.cloud_percent}%" if scene.cloud_percent is not None else "未知"
            doc.add_paragraph(
                f"数据源：{scene.source_label}\n"
                f"拍摄时间：{acquired}\n"
                f"发布时间：{published}\n"
                f"空间分辨率：约 {scene.gsd_m} m/像素\n"
                f"云量：{cloud}\n"
                f"处理级别：{scene.processing_level}\n"
                f"授权类型：{scene.license_type}\n"
                f"决策等级：{scene.decision_grade}\n"
                f"数据限制：{scene.limitations}"
            )

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
                run_content = p.add_run(msg.get("content", ""))
                run_content.font.name = 'Microsoft YaHei'
                run_content.font.size = Pt(10.5)

        report_name = f"report_{uuid.uuid4().hex[:8]}.docx"
        report_path = os.path.join(settings.MEDIA_ROOT, report_name)
        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
        doc.save(report_path)

        download_url = f"/api/report/download/?file={report_name}"
        return JsonResponse({"code": 200, "data": {"file_name": report_name, "download_url": download_url}})
    except Exception as e:
        return JsonResponse({"code": 500, "msg": str(e)})


def download_report(request):
    file_name = request.GET.get("file", "")
    path = safe_media_path(settings.MEDIA_ROOT, file_name, ('.docx',))
    if not path or not os.path.exists(path):
        return JsonResponse({"code": 404, "msg": "not found"}, status=404)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=os.path.basename(path),
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
