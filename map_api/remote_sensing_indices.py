"""可复用的光谱指数内核，保持纯 numpy、无 Django 依赖。"""
import numpy as np

INDEX_DEFINITIONS = {
    "ndvi": {"label": "植被指数", "bands": ("red", "nir"), "range": (-1, 1), "formula": "(NIR-Red)/(NIR+Red)", "threshold_strategy": "分位数或 Otsu", "uses": ("植被健康", "农田筛查"), "status": "catalog_only", "implemented": False},
    "ndwi": {"label": "水体指数", "bands": ("green", "nir"), "range": (-1, 1), "formula": "(Green-NIR)/(Green+NIR)", "threshold_strategy": "固定阈值 + 质量说明", "uses": ("水体范围", "岸线变化"), "status": "implemented", "implemented": True},
    "mndwi": {"label": "改进水体指数", "bands": ("green", "swir"), "range": (-1, 1), "formula": "(Green-SWIR)/(Green+SWIR)", "threshold_strategy": "Otsu", "uses": ("城市水体", "建筑背景抑制"), "status": "catalog_only", "implemented": False},
    "ndbi": {"label": "建成区指数", "bands": ("swir", "nir"), "range": (-1, 1), "formula": "(SWIR-NIR)/(SWIR+NIR)", "threshold_strategy": "分位数或 Otsu", "uses": ("城市扩张", "建设活动"), "status": "catalog_only", "implemented": False},
    "bsi": {"label": "裸土指数", "bands": ("swir", "red", "nir", "blue"), "range": (-1, 1), "formula": "((SWIR+Red)-(NIR+Blue))/((SWIR+Red)+(NIR+Blue))", "threshold_strategy": "分位数", "uses": ("裸地筛查", "施工扰动"), "status": "catalog_only", "implemented": False},
    "ndsi": {"label": "雪指数", "bands": ("green", "swir"), "range": (-1, 1), "formula": "(Green-SWIR)/(Green+SWIR)", "threshold_strategy": "Otsu", "uses": ("积雪范围", "冰雪变化"), "status": "catalog_only", "implemented": False},
}

INDEX_FUNCTIONS = {}


def available_indices():
    return {name: {**meta, "bands": list(meta.get("bands", ())), "uses": list(meta.get("uses", ())) } for name, meta in INDEX_DEFINITIONS.items()}


def get_index_function(name):
    """按目录名称解析指数函数，统一处理用户或 Agent 的动态选择。"""
    key = str(name or "").strip().lower()
    fn = INDEX_FUNCTIONS.get(key)
    if fn is None:
        raise ValueError(f"不支持的遥感指数：{name}")
    return fn


def compute_index(name, bands, **kwargs):
    """使用波段名称字典执行目录中的指数，供任务编排层调用。"""
    if not isinstance(bands, dict):
        raise ValueError("指数波段必须是对象")
    fn = get_index_function(name)
    required = INDEX_DEFINITIONS[str(name).strip().lower()]["bands"]
    missing = [band for band in required if band not in bands]
    if missing:
        raise ValueError(f"指数 {name} 缺少波段：{', '.join(missing)}")
    return fn(*(bands[band] for band in required), **kwargs)


def spectral_index(numerator_a, numerator_b, *, valid_mask=None, fill_value=np.nan):
    a = np.asarray(numerator_a, dtype=np.float32)
    b = np.asarray(numerator_b, dtype=np.float32)
    if a.shape != b.shape:
        raise ValueError("光谱波段尺寸必须一致")
    denom = a + b
    valid = np.isfinite(a) & np.isfinite(b) & (np.abs(denom) > 1e-6)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != a.shape:
            raise ValueError("有效像素掩膜尺寸必须与波段一致")
        valid &= mask
    out = np.full(a.shape, fill_value, dtype=np.float32)
    out[valid] = (a[valid] - b[valid]) / denom[valid]
    return out, valid


def ndvi(red, nir, **kwargs):
    return spectral_index(nir, red, **kwargs)


def ndwi(green, nir, **kwargs):
    return spectral_index(green, nir, **kwargs)


def mndwi(green, swir, **kwargs):
    return spectral_index(green, swir, **kwargs)


def ndbi(swir, nir, **kwargs):
    return spectral_index(swir, nir, **kwargs)


def bsi(swir, red, nir, blue, *, valid_mask=None, fill_value=np.nan):
    """Bare Soil Index: ((SWIR+RED)-(NIR+BLUE))/((SWIR+RED)+(NIR+BLUE))."""
    swir, red, nir, blue = (np.asarray(v, dtype=np.float32) for v in (swir, red, nir, blue))
    if len({swir.shape, red.shape, nir.shape, blue.shape}) != 1:
        raise ValueError("BSI 波段尺寸必须一致")
    return spectral_index(swir + red, nir + blue, valid_mask=valid_mask, fill_value=fill_value)


def ndsi(green, swir, **kwargs):
    return spectral_index(green, swir, **kwargs)


INDEX_FUNCTIONS.update({
    "ndvi": ndvi, "ndwi": ndwi, "mndwi": mndwi,
    "ndbi": ndbi, "bsi": bsi, "ndsi": ndsi,
})


def summarize(index_array, valid_mask=None, threshold=None, pixel_area_m2=None):
    values = np.asarray(index_array, dtype=np.float32)
    valid = np.isfinite(values)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != values.shape:
            raise ValueError("有效像素掩膜尺寸必须与指数一致")
        valid &= mask
    sample = values[valid]
    result = {"valid_pixel_count": int(sample.size), "valid_pixel_ratio": round(float(sample.size / values.size), 4) if values.size else 0.0}
    if sample.size:
        result.update(mean=round(float(sample.mean()), 4), min=round(float(sample.min()), 4), max=round(float(sample.max()), 4))
        if threshold is not None:
            result["above_threshold_ratio"] = round(float(np.mean(sample > threshold)), 4)
            if pixel_area_m2 is not None:
                try:
                    area = float(pixel_area_m2)
                except (TypeError, ValueError):
                    raise ValueError("像元面积必须是数字")
                if area < 0:
                    raise ValueError("像元面积不能为负数")
                result["above_threshold_area_m2"] = round(float(np.sum(sample > threshold) * area), 3)
    else:
        result.update(mean=None, min=None, max=None)
    return result


def otsu_threshold(index_array, valid_mask=None, bins=256):
    """在有效指数像元上计算可解释的 Otsu 分割阈值。"""
    values = np.asarray(index_array, dtype=np.float32)
    valid = np.isfinite(values)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != values.shape:
            raise ValueError("有效像素掩膜尺寸必须与指数一致")
        valid &= mask
    sample = values[valid]
    if sample.size == 0:
        raise ValueError("指数有效像元为空")
    lo, hi = float(sample.min()), float(sample.max())
    if lo == hi:
        return lo
    hist, edges = np.histogram(sample, bins=max(2, int(bins)), range=(lo, hi))
    weights = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) / 2
    cumulative = np.cumsum(weights)
    means = np.cumsum(weights * centers)
    total = cumulative[-1]
    between = (means[-1] * cumulative - means) ** 2 / (cumulative * (total - cumulative) + 1e-12)
    return float(centers[int(np.nanargmax(between))])


def quantile_threshold(index_array, quantile=0.75, valid_mask=None):
    """按有效像元分布返回分位数阈值，适合类别比例未知的快速筛查。"""
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        raise ValueError("分位数必须是数字")
    if not 0 <= q <= 1:
        raise ValueError("分位数必须在 0 到 1 之间")
    values = np.asarray(index_array, dtype=np.float32)
    valid = np.isfinite(values)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != values.shape:
            raise ValueError("有效像素掩膜尺寸必须与指数一致")
        valid &= mask
    sample = values[valid]
    if sample.size == 0:
        raise ValueError("指数有效像元为空")
    return float(np.quantile(sample, q))


def change_summary(before, after, valid_mask=None, min_delta=0.0):
    """计算两期指数差值摘要，统一输出方向、比例和有效像元质量。"""
    first = np.asarray(before, dtype=np.float32)
    second = np.asarray(after, dtype=np.float32)
    if first.shape != second.shape:
        raise ValueError("前后时相指数尺寸必须一致")
    valid = np.isfinite(first) & np.isfinite(second)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != first.shape:
            raise ValueError("有效像素掩膜尺寸必须与指数一致")
        valid &= mask
    delta = second - first
    sample = delta[valid]
    if sample.size == 0:
        raise ValueError("前后时相没有共同有效像元")
    threshold = abs(float(min_delta))
    magnitude = np.abs(sample) >= threshold
    return {
        "valid_pixel_count": int(sample.size),
        "valid_pixel_ratio": round(float(sample.size / first.size), 4) if first.size else 0.0,
        "mean_delta": round(float(sample.mean()), 4),
        "min_delta": round(float(sample.min()), 4),
        "max_delta": round(float(sample.max()), 4),
        "changed_pixel_ratio": round(float(magnitude.mean()), 4),
        "threshold": float(min_delta),
    }
