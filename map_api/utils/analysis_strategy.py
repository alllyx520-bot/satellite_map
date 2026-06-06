from .smart_query_analyzer import analyze_query


SENTINEL2_DETAIL_LIMITS = {
    "building",
    "road",
    "infrastructure",
}

TASK_RUBRICS = {
    "water": {
        "label": "水体与岸线解译",
        "entities": {"water"},
        "rubric": [
            "识别水体类型、岸线形态和连通关系",
            "依据色调、纹理和边界判断浑浊、富营养化或人工硬化迹象",
            "结合周边不透水面和地形关系提示潜在洪涝或面源污染风险",
        ],
    },
    "vegetation": {
        "label": "植被覆盖与生态格局解译",
        "entities": {"vegetation"},
        "rubric": [
            "估计植被覆盖度和高/中/低覆盖区域",
            "区分林地、草地、农田等主要植被类型及斑块形态",
            "分析连续性、破碎化和与建设用地/水体的空间关系",
        ],
    },
    "built_up": {
        "label": "建设用地与城市形态解译",
        "entities": {"building", "urban", "road", "infrastructure"},
        "rubric": [
            "识别建设用地边界、建筑密度和道路骨架",
            "判断紧凑型、蔓延型、带状或组团式空间形态",
            "关注新增硬化地表、零散斑块和功能区混合关系",
        ],
    },
    "terrain_hazard": {
        "label": "地形地貌与灾害线索解译",
        "entities": {"mountain"},
        "rubric": [
            "识别山脊、沟谷、陡坡、冲沟和坡脚堆积等地貌特征",
            "结合裸地、阴影和纹理突变提示滑坡、崩塌或水土流失线索",
            "说明仅能提供遥感线索，灾害判断需结合 DEM 和现场调查",
        ],
    },
    "land_use": {
        "label": "综合土地利用解译",
        "entities": set(),
        "rubric": [
            "按建设用地、耕地、林草地、水体、裸地等类别概括用地结构",
            "估计主要类型面积占比和空间分布",
            "分析人类活动强度、生态本底和潜在冲突区域",
        ],
    },
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


def build_task_profile(query):
    entities = set(query.get("entities", {}))
    best_key = None
    best_score = 0
    for key, cfg in TASK_RUBRICS.items():
        score = len(entities & cfg["entities"])
        if score > best_score:
            best_key = key
            best_score = score

    if not best_key:
        best_key = "land_use"

    cfg = TASK_RUBRICS[best_key]
    return {
        "task": best_key,
        "label": cfg["label"],
        "rubric": cfg["rubric"],
    }


def build_analysis_strategy(question, scene=None, gsd=None, requested_active=True):
    query = analyze_query(question or "")
    task_profile = build_task_profile(query)
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
        "task_profile": task_profile,
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
    task = strategy.get("task_profile") or {}
    if task:
        lines.append("任务画像：" + task.get("label", "综合遥感解译"))
        lines.append("专业分析维度：" + "；".join(task.get("rubric", [])))
    lines.append(
        "回答时请区分“影像可直接观察到的事实”和“基于纹理/形态的推断”，"
        "对超出影像分辨率或时效能力的问题明确说明不可可靠判断。"
    )
    return "\n".join(lines)
