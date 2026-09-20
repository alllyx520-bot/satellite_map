"""Inspectable task methods and measured observation coverage."""
METHODS = {
    "overview": {"title": "全局理解与局部解释", "steps": ["确认来源、日期和是否具有地理信息", "查看全图，按问题选取局部窗口", "用可见特征支持结论，并引用具体观察"], "guard": "整图缩略图不能支撑细小目标的完整计数。"},
    "localization": {"title": "定位与空间关系", "steps": ["概览确定候选区域", "原始窗口核对目标，给窗口命名", "计算同图关系或先检查两图配准", "交付稳定观察引用供追问"], "guard": "观察窗口边界并不等于目标轮廓；没有地理变换只用图像方位。"},
    "counting": {"title": "全域计数", "steps": ["明确目标定义和最小可见尺度", "按 1024px 窗口与 128px 重叠检查全域", "核对跨窗口重复对象", "分别报告已检出数量、估计值、遗漏风险与覆盖"], "guard": "覆盖工具量度查看范围，不证明目标识别召回率或计数正确。"},
    "change": {"title": "多时相变化", "steps": ["检索同产品两期真实日期影像", "核验共同有效区域与季节、云、配准条件", "计算两期指数差异候选", "查看对应位置的两期原图，区分变化与替代解释"], "guard": "指数差异候选不是地物变化真值；底图未知日期不能支撑时相结论。"},
    "water": {"title": "水体与季节性追问", "steps": ["检查原始波段、QA 与 NDWI/MNDWI", "对比同季历史影像", "查询完整 AOI 的 GSW 背景和同期气象", "解释本次新增水面的证据强度"], "guard": "GSW occurrence 频率阈值不能直接证明季节性；降雨相关性不能单独证明成因。"},
    "terrain": {"title": "地形背景", "steps": ["检索覆盖 AOI 的 DEM", "用原始高程和适合的单位计算坡度、坡向、起伏", "将地形结果与影像可见地物关联"], "guard": "DEM 分辨率和获取时段限定了可解释的地形尺度。"},
    "sar": {"title": "SAR 散射观察", "steps": ["核验校准产品和 VV/VH 极化", "核验同轨方向、入射条件与几何校正", "在有效域计算散射变化并用光学或背景复核"], "guard": "未校准 DN 不能直接当作散射系数；阴影和叠掩不能当作水面真值。"},
}


def consult(args, context):
    topic = args.get("topic")
    if topic not in METHODS:
        return {"methods": [{"topic": key, "title": value["title"]} for key, value in METHODS.items()]}
    return {"topic": topic, **METHODS[topic]}


def coverage(args, context):
    from shapely.geometry import box
    from shapely.ops import unary_union
    from .spatial_tools import get_attachment
    attachment = get_attachment(context, args["attachment_id"])
    domain = box(0, 0, attachment.width, attachment.height)
    threshold = args.get("max_pixel_scale", 1.0)
    inspected, coarse, ids = [], [], []
    for row in attachment.observations.filter(conversation=context["conversation"]):
        if len(row.window) != 4:
            continue
        x, y, width, height = row.window
        shape = box(x, y, x + width, y + height).intersection(domain)
        out = row.metadata.get("output_size") or []
        # Only original window reads have a known sampling scale. An annotation
        # alone cannot add inspected area, and a coarse overview stays coarse.
        if row.kind != "window" or len(out) != 2 or min(out) <= 0:
            continue
        scale = max(width / out[0], height / out[1])
        coarse.append(shape)
        if scale <= threshold:
            inspected.append(shape)
            ids.append(str(row.id))
    total = domain.area
    seen = unary_union(inspected).area if inspected else 0
    any_scale = unary_union(coarse).area if coarse else 0
    return {"attachment_id": str(attachment.id), "max_original_pixels_per_display_pixel": threshold,
        "observed_original_pixels": seen, "total_original_pixels": total,
        "observed_fraction": seen / total if total else 0,
        "overview_fraction": any_scale / total if total else 0, "observation_ids": ids,
        "unobserved_fraction": 1 - seen / total if total else 1,
        "limitations": ["覆盖仅表示保存了对应尺度的观察窗口，不证明识别完整性。", "有效数据掩膜及算法处理覆盖需从具体数据产品读取。"]}
