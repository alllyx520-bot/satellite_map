"""Calibrated single-scene spectral products, using one AOI and QA grid."""
import numpy as np

from .imagery_sources.earth_search import get_collection_profile
from .remote_sensing_indices import INDEX_DEFINITIONS, compute_index, summarize
from .utils.agent_tools import fetch_cog_bbox_array, polygon_mask_for_bbox


def compute_spectral_summary(candidate, bbox, *, index, titiler_endpoint=None, polygon=None, threshold=None):
    if index not in {"ndwi", "ndvi", "mndwi"}:
        return {"available": False, "reason": "未支持的光谱产品", "index": index}
    definition = INDEX_DEFINITIONS[index]
    # 无 SCL 的 collection(如 sentinel-2-l1c 兜底)不允许默默无掩膜计算。
    collection = getattr(candidate, "collection", None) or "sentinel-2-l2a"
    if get_collection_profile(collection).get("qa_band") is None:
        raise ValueError(f"{collection} 无 SCL 云掩膜，光谱指数不可用，请改用 L2A 影像")
    try:
        threshold = float(threshold if threshold is not None else (0.3 if index == "ndvi" else 0.1))
        if not np.isfinite(threshold) or not -1 <= threshold <= 1:
            raise ValueError("指数阈值必须在 -1 到 1 之间")
        assets = candidate.assets or {}
        selected = {name: assets.get("swir16") if name == "swir" else assets.get(name) for name in definition["bands"]}
        selected["scl"] = assets.get("scl")
        missing = [name for name, asset in selected.items() if not asset or not asset.get("href")]
        if missing:
            raise ValueError("缺少必需波段或 QA 资产：" + ", ".join(missing))
        arrays = {name: fetch_cog_bbox_array(asset, bbox, titiler_endpoint, kind="scl" if name == "scl" else "reflectance") for name, asset in selected.items()}
        scl = arrays.pop("scl")
        if any(array.shape != scl.shape for array in arrays.values()):
            raise ValueError("光谱与 SCL 输出网格不一致")
        aoi = polygon_mask_for_bbox(polygon, bbox, scl.shape) if polygon else np.ones(scl.shape, dtype=bool)
        aoi_count = int(aoi.sum())
        qa = np.isfinite(scl) & np.isin(scl, [4, 5, 6, 7])
        mask = qa & aoi
        for array in arrays.values():
            mask &= np.isfinite(array) & (array >= 0) & (array <= 1)
        values, valid = compute_index(index, arrays, valid_mask=mask)
        valid_count = int(valid.sum())
        if aoi_count == 0 or valid_count < 1024 or valid_count / aoi_count < 0.6:
            raise ValueError("AOI 有效像元少于 1024 或低于 60%，不能输出指标比例")
        stats = summarize(values, valid_mask=valid)
        selected_values = values[valid]
        ratio = float(np.mean(selected_values > threshold))
        return {
            "available": True, "index": index, "method": definition["formula"],
            "bands": list(definition["bands"]), "threshold": threshold, "threshold_method": "fixed",
            "thresholded_pixel_ratio": round(ratio, 4), "thresholded_percent": round(ratio * 100, 2),
            "alternative_thresholds": [{"threshold": round(threshold + delta, 4), "ratio": round(float(np.mean(selected_values > threshold + delta)), 4)} for delta in (-0.05, -0.02, 0.02, 0.05)],
            "sample_size_px": valid_count, "aoi_pixel_count": aoi_count,
            "valid_pixel_ratio": round(valid_count / aoi_count, 4), "mean": stats["mean"], "min": stats["min"], "max": stats["max"],
            "mask_source": "sentinel-2-scl+nodata+reflectance_range" + ("+aoi" if polygon else ""),
            "masked_class_counts": {str(code): int(np.sum(aoi & (scl == code))) for code in (0, 1, 2, 3, 8, 9, 10, 11)},
            "aoi": polygon or bbox, "polygon_clipped": bool(polygon), "measurement_grade": "screening",
            "data_contract": {"source": "sentinel-2-l2a", "assets": selected,
                              "grid": {"bbox": bbox, "shape": list(scl.shape)},
                              "radiometry": {name: (asset.get("raster:bands") or [{}])[0] for name, asset in selected.items() if name != "scl"},
                              "qa_resampling": "nearest", "spectral_resampling": "bilinear"},
            "limitations": ["仅统计共同有效像元", "阈值分类并非地物真值", "输出网格经过重采样，不能代替原生分辨率面积测量"],
            "change_detection_allowed": False,
        }
    except Exception as exc:
        return {"available": False, "index": index, "method": definition["formula"],
                "reason": str(exc)[:180] if isinstance(exc, ValueError) else "光谱资产读取失败，请重试",
                "diagnostics": {"error_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}}
