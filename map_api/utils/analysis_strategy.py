from .smart_query_analyzer import analyze_query


SENTINEL2_DETAIL_LIMITS = {
    "building",
    "road",
    "infrastructure",
}


def scene_source(scene):
    if not scene:
        return "unknown"
    source = (getattr(scene, "source", "") or "").lower()
    if source in ("sentinel2", "earth_search"):
        return "sentinel2"
    if source == "mapbox":
        return "mapbox"
    return source or "unknown"


def build_analysis_strategy(question, scene=None, gsd=None, requested_active=True):
    query = analyze_query(question or "")
    source = scene_source(scene)
    try:
        gsd_value = float(gsd if gsd is not None else getattr(scene, "gsd_m", 0) or 0)
    except (TypeError, ValueError):
        gsd_value = 0

    strengths = []
    limits = []
    method_notes = []
    should_use_active = bool(requested_active)

    if source == "sentinel2":
        strengths.extend(["近期公开影像", "拍摄时间和云量可追溯", "适合宏观地类、水体、植被和大范围建设区分析"])
        limits.extend(["空间分辨率约 10m，不适合识别小建筑、车辆、屋顶材质等细节目标"])
        if query["is_detail"] or any(k in query["entities"] for k in SENTINEL2_DETAIL_LIMITS):
            should_use_active = False
            method_notes.append("该问题偏细节判读，但 Sentinel-2 分辨率有限，应转为宏观判读并说明不可判读项")
        else:
            method_notes.append("使用 Sentinel-2 的时相、云量和宏观纹理进行区域级解译")
    elif source == "mapbox":
        strengths.extend(["高清视觉底图", "适合建筑形态、道路结构和空间格局分析"])
        limits.extend(["底图拍摄时间、云量和原始产品号不透明，不能把结论表述为已复核事实"])
        if query["is_detail"] and requested_active:
            method_notes.append("问题偏细节，建议启用主动感知进行局部放大")
        else:
            method_notes.append("使用整体纹理、颜色和空间关系进行视觉解译")
    else:
        limits.extend(["影像来源信息不足，需在回答中说明不确定性"])

    if gsd_value:
        if gsd_value >= 8 and query["is_detail"]:
            limits.append(f"GSD 约 {gsd_value:g}m/像素，细小目标尺寸可能低于可判读尺度")
        elif gsd_value <= 2 and query["is_detail"]:
            strengths.append(f"GSD 约 {gsd_value:g}m/像素，具备较好的细节观察条件")

    strategy = {
        "source": source,
        "query": query,
        "active_perception": should_use_active,
        "strengths": strengths,
        "limits": limits,
        "method_notes": method_notes,
    }
    strategy["prompt"] = strategy_prompt(strategy)
    return strategy


def strategy_prompt(strategy):
    lines = ["## 智能分析策略"]
    if strategy["strengths"]:
        lines.append("可重点分析：" + "；".join(strategy["strengths"]))
    if strategy["limits"]:
        lines.append("判读边界：" + "；".join(strategy["limits"]))
    if strategy["method_notes"]:
        lines.append("方法提示：" + "；".join(strategy["method_notes"]))
    lines.append(
        "回答时请区分“影像可直接观察到的事实”和“基于纹理/形态的推断”，"
        "对超出影像分辨率或时效能力的问题明确说明不可可靠判断。"
    )
    return "\n".join(lines)
