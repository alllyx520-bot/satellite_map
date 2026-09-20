"""响应 payload 构造函数(Phase 7 从 views.py 拆出,M-2)。

影像质量/可信度/源推荐/场景/历史/分析方法/输出归一化等纯构造函数,
无模型导入、无文件系统访问。views.py 通过导入保持同名再导出,
既有 patch("map_api.views.X") 与 from map_api.views import X 全部不受影响。
"""
from django.utils import timezone
import re

from .utils.active_perception import extract_answer_text


def _scene_source_key(scene):
    source = (getattr(scene, "source", "") or "").lower()
    if source in ("sentinel2", "earth_search"):
        return "sentinel2"
    return source or "unknown"


def _days_since(value):
    if not value:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return max(0, (timezone.now() - value).days)


def _grade_label(grade):
    return {
        "reference": "参考级",
        "screening": "筛查级",
        "decision_support": "决策辅助级",
        "evidence": "证据级",
    }.get(grade or "", grade or "未知")


def _timeliness_label(days):
    if days is None:
        return "时相未知"
    if days <= 7:
        return f"近7天内（约 {days} 天前）"
    if days <= 30:
        return f"近30天内（约 {days} 天前）"
    return f"历史影像（约 {days} 天前）"


def _cloud_label(cloud_percent):
    if cloud_percent is None:
        return "云量未知"
    if cloud_percent <= 10:
        return f"低云量（{cloud_percent:g}%）"
    if cloud_percent <= 30:
        return f"中等云量（{cloud_percent:g}%）"
    return f"高云量（{cloud_percent:g}%）"


def imagery_quality_payload(scene):
    if not scene:
        return None
    source = _scene_source_key(scene)
    acquired_days = _days_since(scene.acquired_at)
    fetched_days = _days_since(scene.fetched_at)
    gsd = scene.gsd_m or 0
    scene_metadata = getattr(scene, "metadata", None) or {}
    try:
        from .data_contract import build_scene_contract
        data_contract = build_scene_contract(scene, polygon=scene_metadata.get("district_polygon"))
    except Exception:
        data_contract = None
    preview_gsd = scene_metadata.get("preview_scale_m") or scene_metadata.get("rendered_gsd_m")
    original_gsd = scene_metadata.get("source_asset_gsd_m") or gsd
    cloud_label = _cloud_label(scene.cloud_percent)
    grade_label = _grade_label(scene.decision_grade)
    timeliness_override = None
    cautions = []

    if source == "sentinel2":
        summary = "近期公开 Sentinel-2 L2A 影像，具备拍摄时间、云量和产品号追溯能力。"
        best_for = "适合宏观地类、水体、植被、农田和大范围建设区变化筛查。"
        spatial_note = f"原始 GSD 约 {float(original_gsd):g} m/像素；当前预览采样约 {float(preview_gsd):g} m/像素。" if original_gsd and preview_gsd else (f"原始 GSD 约 {float(original_gsd):g} m/像素，偏区域级解译。" if original_gsd else "约 10m 级公开影像，偏区域级解译。")
        cautions.append("不适合车辆、小建筑、屋顶材质等细节目标判读。")
        if scene.cloud_percent is None:
            cautions.append("缺少云量指标，需降低结论确定性。")
        elif scene.cloud_percent > 30:
            cautions.append("云量偏高，需警惕云、雾或阴影干扰。")
        if acquired_days is None:
            cautions.append("缺少明确拍摄时间，不能说明时效性。")
        elif acquired_days > 30:
            cautions.append("拍摄时间超过30天，近期态势判断需谨慎。")
    elif source == "sentinel1":
        summary = "Sentinel-1 GRD SAR 后向散射影像，全天候可获取，不受云影响。"
        best_for = "适合水体/洪涝淹没范围、宏观地物和地表变化线索筛查。"
        spatial_note = f"原始 GSD 约 {float(original_gsd):g} m/像素。" if original_gsd else "约 10m 级 SAR 影像。"
        cloud_label = "不受云影响（SAR）"
        cautions.extend([
            "SAR 非光学影像，存在斑点噪声与几何畸变，视觉解译可靠性低于光学影像。",
            "结论限于水体/淹没与宏观地物，不支持光谱指数与细节判读。",
        ])
    elif source == "landsat":
        summary = "Landsat Collection 2 Level-2 地表反射率影像，可回溯至 1982 年，经 Microsoft Planetary Computer 匿名签名访问。"
        best_for = "适合历史回溯、宏观变化筛查和水体/植被长期对比。"
        spatial_note = f"原始 GSD 约 {float(original_gsd):g} m/像素。" if original_gsd else "约 30m 级公开影像。"
        cautions.extend([
            "约 30m 空间分辨率，不适合小建筑、车辆等细节判读。",
            "重访周期 16 天（双星约 8 天），时效性弱于 Sentinel-2。",
            "匿名 SAS 签名有速率限制，服务可持续性依赖微软。",
        ])
    elif source == "copdem":
        summary = "Copernicus DEM GLO-30 静态数字高程模型（采集基线 2011-2015）。"
        best_for = "适合地形、坡度、地势分析和水文背景判断。"
        spatial_note = "原生分辨率约 30 m/像素。"
        cloud_label = "不适用（静态 DEM）"
        acquired_days = None
        timeliness_override = "静态 DEM（采集基线 2011-2015）"
        cautions.extend([
            "静态 DEM，不代表拍摄时相的地表状态，不能用于变化监测。",
            "不支持光谱指数、云量与时相类质量判断。",
        ])
    elif source in ("mapbox", "tianditu", "esri"):
        basemap_name = {"mapbox": "Mapbox 高清底图", "tianditu": "天地图影像高清底图", "esri": "Esri World Imagery 高清底图"}[source]
        summary = f"{basemap_name}，视觉细节较强，但时相和原始产品信息不可追溯。"
        best_for = "适合建筑形态、道路结构、空间格局和地物纹理的视觉解译。"
        spatial_note = (
            f"渲染约 {gsd:g} m/像素；该数值用于当前截图尺度估算，不等同于原始传感器 GSD。"
            if gsd else "底图原始空间分辨率不透明。"
        )
        cautions.extend([
            "不能作为可复核的时效性证据。",
            "不能直接支撑需要明确拍摄日期、云量或传感器产品号的结论。",
        ])
    else:
        summary = "影像来源信息不足。"
        best_for = "仅适合做一般视觉参考。"
        spatial_note = f"约 {gsd:g} m/像素。" if gsd else "空间分辨率未知。"
        cautions.append("需在结论中明确数据来源和质量不确定性。")

    return {
        "source": source,
        "summary": summary,
        "best_for": best_for,
        "spatial_resolution": spatial_note,
        "original_gsd_m": original_gsd or None,
        "preview_scale_m": preview_gsd,
        "timeliness": timeliness_override or _timeliness_label(acquired_days),
        "acquired_days_ago": acquired_days,
        "fetched_days_ago": fetched_days,
        "cloud_quality": cloud_label,
        "decision_grade": scene.decision_grade,
        "decision_grade_label": grade_label,
        "cautions": cautions,
        "data_contract": data_contract,
        "quality": (data_contract or {}).get("quality", {}),
        "spatial": (data_contract or {}).get("spatial", {}),
        "limitations": (data_contract or {}).get("limitations", []),
    }


def analysis_confidence_payload(strategy=None, imagery_quality=None):
    strategy = strategy or {}
    imagery_quality = imagery_quality or {}
    source = strategy.get("source") or imagery_quality.get("source") or "unknown"
    query = strategy.get("query") or {}
    task = strategy.get("task_profile") or {}
    task_label = task.get("label", "综合遥感解译")
    is_detail = bool(query.get("is_detail"))
    cloud = imagery_quality.get("cloud_quality") or "云量未知"
    acquired_days = imagery_quality.get("acquired_days_ago")
    cautions = imagery_quality.get("cautions") or []
    basis = []
    required_checks = []

    if source in ("mapbox", "tianditu", "esri"):
        level = "reference"
        label = "视觉参考级"
        basis.extend([
            "底图视觉细节较强，适合形态和空间格局判断",
            "拍摄时间、云量和原始产品号不可追溯",
        ])
        required_checks.append("涉及时效性或行政决策时，需使用可追溯公开影像或现场资料复核")
    elif source == "sentinel1":
        level = "screening"
        label = "筛查级"
        basis.extend([
            "Sentinel-1 SAR 全天候影像，不受云影响，可追溯拍摄时间",
            "SAR 非光学影像，VL 解译可靠性低，结论限于水体/淹没与宏观地物",
        ])
        required_checks.append("洪水/淹没范围等重要结论建议结合光学影像或现场资料复核")
    elif source == "copdem":
        level = "reference"
        label = "参考级"
        basis.append("Copernicus DEM 为静态地形数据（采集基线 2011-2015），不代表拍摄时相地表状态")
        required_checks.append("涉及时相变化或地表现状的结论需改用可追溯时相影像")
    elif source == "landsat":
        level = "screening"
        label = "筛查级"
        basis.extend([
            "Landsat C2 L2 可追溯历史影像（1982 年起），经 Planetary Computer 匿名签名访问",
            "30m 分辨率限制细节判读，结论限于宏观地物与变化线索",
        ])
        required_checks.append("细节目标需切换高清底图复核；重要结论建议结合 Sentinel-2 或现场资料复核")
    elif source == "sentinel2":
        basis.append(f"Sentinel-2 L2A 可追溯公开影像，{cloud}")
        if acquired_days is not None:
            basis.append(f"拍摄时间约 {acquired_days} 天前")
        if is_detail:
            level = "low"
            label = "低置信细节判读"
            required_checks.append("细节目标需切换高清底图或更高分辨率影像复核")
        elif acquired_days is not None and acquired_days <= 7 and "低云量" in cloud:
            level = "decision_support"
            label = "决策辅助级"
            required_checks.append("可作为区域筛查和辅助判断依据，正式结论仍建议结合多时相或地面资料")
        else:
            level = "screening"
            label = "筛查级"
            required_checks.append("适合发现宏观线索，需结合多时相影像或其他数据源复核")
    else:
        level = "unknown"
        label = "来源不足"
        basis.append("影像来源或质量信息不足")
        required_checks.append("需补充数据来源、拍摄时间和空间分辨率后再形成结论")

    if cautions:
        basis.append("主要限制：" + "；".join(cautions[:2]))

    return {
        "level": level,
        "label": label,
        "task_label": task_label,
        "basis": basis,
        "required_checks": required_checks,
    }


def analysis_confidence_text(confidence):
    if not confidence:
        return ""
    lines = [
        "## 结论可信度与证据层级",
        f"证据层级：{confidence.get('label', '未知')}",
        f"对应任务：{confidence.get('task_label', '综合遥感解译')}",
    ]
    basis = confidence.get("basis") or []
    checks = confidence.get("required_checks") or []
    if basis:
        lines.append("判定依据：" + "；".join(basis))
    if checks:
        lines.append("复核要求：" + "；".join(checks))
    return "\n".join(lines)


def source_recommendation_payload(strategy=None, imagery_quality=None, question=""):
    strategy = strategy or {}
    imagery_quality = imagery_quality or {}
    query = strategy.get("query") or {}
    task = strategy.get("task_profile") or {}
    source = strategy.get("source") or imagery_quality.get("source") or "unknown"
    entities = set((query.get("entities") or {}).keys())
    is_detail = bool(query.get("is_detail"))
    task_key = task.get("task", "")
    task_label = task.get("label", "综合遥感解译")
    question_text = str(question or "")

    time_keywords = ("近期", "最新", "现在", "当前", "变化", "变迁", "新增", "扩张", "退化", "灾情", "汛情")
    needs_timeliness = any(word in question_text for word in time_keywords)
    sentinel_macro_tasks = {"land_use", "water", "vegetation", "agriculture", "terrain_hazard"}
    flood_hit = task_key == "flood" or any(word in question_text for word in ("洪水", "洪涝", "淹没", "内涝", "汛情"))
    terrain_hit = task_key == "terrain" or any(word in question_text for word in ("地形", "坡度", "高程", "山地", "地势"))
    history_hit = any(word in question_text for word in ("历史", "十年前", "五年前", "多年前", "往年", "上世纪", "回溯", "热岛"))

    if flood_hit:
        recommended_source = "sentinel1"
        label = "建议使用 Sentinel-1 SAR 影像"
        reason = "问题涉及洪水/淹没/汛情，Sentinel-1 SAR 全天候可获取、对水体敏感，不受云雨影响。"
    elif terrain_hit:
        recommended_source = "copdem"
        label = "建议使用 Copernicus DEM 高程数据"
        reason = "问题涉及地形/坡度/高程，静态 DEM 更适合地势分析；注意其不代表拍摄时相地表状态。"
    elif history_hit:
        recommended_source = "landsat"
        label = "建议使用 Landsat 历史回溯影像"
        reason = "问题涉及历史回溯或多年前对比，Landsat Collection 2 存档可回溯至 1982 年（30m 分辨率，经 Planetary Computer 匿名签名访问），适合宏观变化筛查。"
    elif is_detail or entities & {"building", "road", "infrastructure", "vehicle"}:
        recommended_source = "mapbox"
        label = "建议使用高清底图"
        reason = "问题包含建筑、道路、设施或计数等细节判读需求，需要更高视觉细节。"
    elif needs_timeliness or task_key in sentinel_macro_tasks:
        recommended_source = "sentinel2"
        label = "建议使用近期公开影像"
        reason = "问题偏宏观地类、水体、生态农业、地形灾害或变化筛查，Sentinel-2 的拍摄时间和云量更可追溯。"
    else:
        recommended_source = source if source in ("mapbox", "tianditu", "esri", "sentinel2", "sentinel1", "copdem", "landsat") else "mapbox"
        label = "当前图像源可用于初步分析"
        reason = "问题未表现出强时效或强细节偏好，可先按当前图像源进行初步判读。"

    alignment = "matched" if source == recommended_source else "switch_recommended"
    if source == "unknown":
        alignment = "unknown"

    if alignment == "matched":
        action = "当前图像源与任务匹配。"
    elif recommended_source == "sentinel2":
        action = "建议切换到“近期公开影像”获取可追溯时相后再分析。"
    elif recommended_source == "mapbox":
        action = "建议切换到“高清底图”（mapbox/天地图/Esri）观察细节后再分析。"
    elif recommended_source == "sentinel1":
        action = "建议切换到 Sentinel-1 SAR 影像，全天候获取水体/淹没线索。"
    elif recommended_source == "copdem":
        action = "建议切换到 Copernicus DEM 高程数据进行地形分析。"
    elif recommended_source == "landsat":
        action = "建议切换到 Landsat 历史回溯影像（1982 年起，30m）进行宏观变化筛查。"
    else:
        action = "建议先补充图像源信息。"

    return {
        "recommended_source": recommended_source,
        "recommended_label": label,
        "current_source": source,
        "alignment": alignment,
        "task_label": task_label,
        "reason": reason,
        "action": action,
    }


def scene_selection_payload(scene):
    if not scene:
        return None
    metadata = scene.metadata or {}
    if not (metadata.get("selection_method") or metadata.get("suitability_score") is not None):
        return None
    candidate_count = metadata.get("candidate_count")
    selection_rank = metadata.get("selection_rank")
    score = metadata.get("suitability_score")
    parts = []
    if candidate_count:
        parts.append(f"候选池 {candidate_count} 景")
    if selection_rank:
        parts.append(f"采用第 {selection_rank} 个可渲染候选")
    if score is not None:
        parts.append(f"评分 {score}")
    summary = "；".join(parts) if parts else "已记录候选优选依据"
    return {
        "method": metadata.get("selection_method", "suitability_score"),
        "candidate_count": candidate_count,
        "selection_rank": selection_rank,
        "suitability_score": score,
        "score_reasons": metadata.get("score_reasons") or [],
        "render_fallback_errors": metadata.get("render_fallback_errors") or [],
        "summary": summary,
    }


def scene_brief_payload(scene):
    if not scene:
        return None
    quality = imagery_quality_payload(scene) or {}
    return {
        "id": scene.id,
        "file_name": scene.file_name,
        "source": scene.source,
        "source_label": scene.source_label,
        "product_id": scene.product_id,
        "acquired_at": scene.acquired_at.isoformat() if scene.acquired_at else None,
        "gsd_m": scene.gsd_m,
        "original_gsd_m": (scene.metadata or {}).get("source_asset_gsd_m") or scene.gsd_m,
        "preview_scale_m": (scene.metadata or {}).get("preview_scale_m") or (scene.metadata or {}).get("rendered_gsd_m") or scene.gsd_m,
        "cloud_percent": scene.cloud_percent,
        "decision_grade": scene.decision_grade,
        "decision_grade_label": quality.get("decision_grade_label"),
        "timeliness": quality.get("timeliness"),
        "cloud_quality": quality.get("cloud_quality"),
        "selection": scene_selection_payload(scene),
    }


def sentinel_retrieval_timeline_payload(retrieval):
    if not retrieval:
        return {}
    payload = {}
    for key in (
        "candidate_count",
        "cache_hit",
        "mosaic",
        "selection_method",
        "target_coverage_ratio",
        "valid_image_ratio",
        "coverage_filtered_count",
        "render_errors",
    ):
        if key in retrieval:
            payload[key] = retrieval[key]
    selected = retrieval.get("selected_candidates") or []
    if selected:
        payload["selected_candidates"] = [candidate.as_dict() for candidate in selected]
    return payload


def scene_payload(scene):
    quality = imagery_quality_payload(scene)
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
        "quality": quality,
        "selection": scene_selection_payload(scene),
        "metadata": scene.metadata,
    }


def imagery_context_text(scene):
    if not scene:
        return ""
    source_key = _scene_source_key(scene)
    acquired = scene.acquired_at.strftime("%Y-%m-%d %H:%M") if scene.acquired_at else "未知"
    cloud = f"{scene.cloud_percent}%" if scene.cloud_percent is not None else "未知"
    if source_key == "sentinel1":
        cloud = "不受云影响（SAR）"
    elif source_key == "copdem":
        cloud = "不适用（静态 DEM）"
        acquired = "静态 DEM（采集基线 2011-2015）"
    scene_metadata = getattr(scene, "metadata", None) or {}
    quality = imagery_quality_payload(scene) or {}
    quality_lines = ""
    if quality:
        quality_lines = (
            f"质量摘要：{quality.get('summary', '')}\n"
            f"时效性：{quality.get('timeliness', '')}\n"
            f"云量质量：{quality.get('cloud_quality', '')}\n"
            f"适合任务：{quality.get('best_for', '')}\n"
            f"空间尺度：{quality.get('spatial_resolution', '')}\n"
            f"使用提醒：{'；'.join(quality.get('cautions') or [])}\n"
        )
    return (
        "## 影像元数据\n"
        f"数据源：{scene.source_label}\n"
        f"拍摄时间：{acquired}\n"
        f"原始 GSD：约 {scene_metadata.get('source_asset_gsd_m') or scene.gsd_m} m/像素\n"
        f"当前预览采样尺度：约 {scene_metadata.get('preview_scale_m') or scene_metadata.get('rendered_gsd_m') or '未知'} m/像素\n"
        f"云量：{cloud}\n"
        f"处理级别：{scene.processing_level}\n"
        f"决策等级：{scene.decision_grade}\n"
        f"数据限制：{scene.limitations}\n"
        f"{quality_lines}"
        "回答时必须基于上述数据限制说明不确定性，不得把参考级底图结论表述为已复核证据。"
    )


def analysis_method_payload(
    mode,
    model_name,
    strategy,
    active_stages=1,
    imagery_quality=None,
    question="",
    output_quality=None,
):
    strategy = strategy or {}
    task = strategy.get("task_profile") or {}
    confidence = analysis_confidence_payload(strategy, imagery_quality)
    source_recommendation = source_recommendation_payload(strategy, imagery_quality, question)
    return {
        "mode": mode,
        "model": model_name,
        "source": strategy.get("source", "unknown"),
        "task": task.get("task", ""),
        "task_label": task.get("label", "综合遥感解译"),
        "active_perception": bool(strategy.get("active_perception")),
        "active_stages": active_stages,
        "strengths": strategy.get("strengths") or [],
        "limits": strategy.get("limits") or [],
        "method_notes": strategy.get("method_notes") or [],
        "imagery_quality": imagery_quality,
        "confidence": confidence,
        "source_recommendation": source_recommendation,
        "output_quality": output_quality or {},
    }


def latest_analysis_method(messages):
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        method = msg.get("analysis_method")
        if isinstance(method, dict):
            return method
    return None


def normalize_model_answer(response_text):
    raw = "" if response_text is None else str(response_text).strip()
    answer = extract_answer_text(raw).strip()
    lowered = raw.lower()
    has_answer_tag = bool(raw and answer and "<answer" in lowered and "</answer>" in lowered)
    fallback_used = False
    warnings = []
    if not answer:
        answer = raw or "模型未返回有效文字结果，请重新分析或更换图像源。"
        fallback_used = True
        warnings.append("模型输出为空，已使用兜底提示")
    elif not has_answer_tag:
        fallback_used = True
        warnings.append("模型未按 <answer> 结构化格式输出，已使用原文作为结论")
    return {
        "answer": answer,
        "quality": {
            "structured_answer": has_answer_tag,
            "fallback_used": fallback_used,
            "warnings": warnings,
        },
    }


def apply_quality_guard(answer, *, scene=None, imagery_quality=None):
    """在证据不足时拦截模型过度具体的定量断言。

    免责声明不能抵消正文里的幻觉数字；当云量、覆盖率、有效像素率或输出 GSD
    明显不适合精细判读时，替换“目标属性 + 数字 + 单位”片段，并返回可审计元数据。
    """
    text = "" if answer is None else str(answer)
    if imagery_quality:
        quality = imagery_quality
    elif scene and hasattr(scene, "acquired_at"):
        quality = imagery_quality_payload(scene) or {}
    else:
        quality = {}
    metadata = getattr(scene, "metadata", {}) or {}
    try:
        gsd = float(getattr(scene, "gsd_m", None) or 0)
    except (TypeError, ValueError):
        gsd = 0
    try:
        cloud = float(getattr(scene, "cloud_percent", None)) if getattr(scene, "cloud_percent", None) is not None else None
    except (TypeError, ValueError):
        cloud = None
    try:
        coverage = float(metadata.get("target_coverage_ratio")) if metadata.get("target_coverage_ratio") is not None else None
    except (TypeError, ValueError):
        coverage = None
    try:
        valid = float(metadata.get("valid_image_ratio")) if metadata.get("valid_image_ratio") is not None else None
    except (TypeError, ValueError):
        valid = None

    reasons = []
    if gsd > 50:
        reasons.append(f"GSD约{gsd:g}m/像素")
    if cloud is not None and cloud > 30:
        reasons.append(f"云量约{cloud:g}%")
    if coverage is not None and coverage < 0.85:
        reasons.append(f"有效覆盖约{coverage:.0%}")
    if valid is not None and valid < 0.80:
        reasons.append(f"有效像素约{valid:.0%}")
    if not reasons:
        return text, {"triggered": False, "reasons": [], "redacted_count": 0}

    # 只处理明显的“属性+精确数字”组合，避免误伤普通日期、比例或 NDWI 数值。
    numeric_claim = re.compile(
        r"((?:河道|道路|建筑|养殖塘|池塘|目标|洪泛区|岸线|水面|面积|宽度|长度|直径|水深|流速|含沙量|洪泛面积)"
        r"[^。！？\n]{0,24}?\d+(?:\.\d+)?\s*(?:mg/L|平方米|公顷|公里|厘米|m²|m2|km|cm|米|吨|m|%))",
        flags=re.IGNORECASE,
    )
    def _redact_claim(match):
        label_match = re.match(r"(河道|道路|建筑|养殖塘|池塘|目标|洪泛区|岸线|水面|面积|宽度|长度|直径|水深|流速|含沙量|洪泛面积)", match.group(1))
        label = label_match.group(1) if label_match else "该项"
        return f"{label}：当前影像无法可靠估计"

    guarded, count = numeric_claim.subn(_redact_claim, text)
    # 即使没有命中，也给出可见的证据等级提示，供用户理解为何不能精确回答。
    note = "数据质量门禁：" + "、".join(reasons) + "；本次仅支持区域级趋势/形态判断，精确尺寸、数量和水质参数需更高分辨率或现场数据复核。"
    if note not in guarded:
        guarded = guarded.rstrip() + "\n\n" + note
    return guarded, {"triggered": True, "reasons": reasons, "redacted_count": count}


def docx_safe_text(value):
    text = "" if value is None else str(value)
    safe_chars = []
    for ch in text:
        code = ord(ch)
        if ch in ("\t", "\n", "\r"):
            safe_chars.append(ch)
        elif 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            safe_chars.append(ch)
    return "".join(safe_chars)


def join_method_items(items):
    if not isinstance(items, list):
        return ""
    parts = [docx_safe_text(item) for item in items]
    return "；".join(part for part in parts if part)


def chat_history_payload(obj):
    return {
        "id": obj.id,
        "scene_id": obj.scene_id,
        "image_file": obj.image_file,
        "spatial_context": obj.spatial_context,
        "bbox": obj.bbox,
        "scene": scene_brief_payload(obj.scene) if obj.scene else None,
        "image_available": True,
        "created_at": obj.created_at,
        "updated_at": obj.updated_at,
    }
