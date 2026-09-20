"""Calibrated single-scene spectral products, using one AOI and QA grid."""
import numpy as np

from .imagery_sources.earth_search import get_collection_profile
from .remote_sensing_indices import INDEX_DEFINITIONS, compute_index, summarize
from .utils.agent_tools import _qa_mask_source, _qa_valid_mask, _radiometry_for, _sign_asset_for_collection, fetch_cog_bbox_array, polygon_mask_for_bbox


def compute_spectral_summary(candidate, bbox, *, index, titiler_endpoint=None, polygon=None, threshold=None):
    if index not in {"ndwi", "ndvi", "mndwi", "ndbi", "bsi", "ndsi", "nbr"}:
        return {"available": False, "reason": "未支持的光谱产品", "index": index}
    definition = INDEX_DEFINITIONS[index]
    # 无 QA 波段的 collection(如 sentinel-2-l1c 兜底)不允许默默无掩膜计算。
    collection = getattr(candidate, "collection", None) or "sentinel-2-l2a"
    profile = get_collection_profile(collection)
    qa_band = profile.get("qa_band")
    if qa_band is None:
        raise ValueError(f"{collection} 无 SCL 云掩膜，光谱指数不可用，请改用 L2A 影像")
    try:
        threshold = float(threshold if threshold is not None else (0.3 if index == "ndvi" else -0.1 if index == "nbr" else 0.1))
        if not np.isfinite(threshold) or not -1 <= threshold <= 1:
            raise ValueError("指数阈值必须在 -1 到 1 之间")
        assets = candidate.assets or {}
        band_aliases = profile.get("band_aliases") or {}
        selected = {}
        for name in definition["bands"]:
            fallback = {"swir": "swir16", "swir2": "swir22"}.get(name, name)
            key = band_aliases.get(name, fallback)
            selected[name] = assets.get(key)
        selected[qa_band] = assets.get(qa_band)
        missing = [name for name, asset in selected.items() if not asset or not asset.get("href")]
        if missing:
            raise ValueError("缺少必需波段或 QA 资产：" + ", ".join(missing))
        radiometry = _radiometry_for(profile)
        arrays = {
            name: fetch_cog_bbox_array(_sign_asset_for_collection(asset, profile), bbox, titiler_endpoint,
                                       kind="scl" if name == qa_band else "reflectance",
                                       radiometry=radiometry)
            for name, asset in selected.items()
        }
        qa_array = arrays.pop(qa_band)
        if any(array.shape != qa_array.shape for array in arrays.values()):
            raise ValueError("光谱与 QA 输出网格不一致")
        aoi = polygon_mask_for_bbox(polygon, bbox, qa_array.shape) if polygon else np.ones(qa_array.shape, dtype=bool)
        aoi_count = int(aoi.sum())
        qa = _qa_valid_mask(qa_array, profile)
        mask = qa & aoi
        for array in arrays.values():
            mask &= np.isfinite(array) & (array >= -1e-6) & (array <= 1 + 1e-6)
        values, valid = compute_index(index, arrays, valid_mask=mask)
        valid_count = int(valid.sum())
        if aoi_count == 0 or valid_count < 1024 or valid_count / aoi_count < 0.6:
            raise ValueError("AOI 有效像元少于 1024 或低于 60%，不能输出指标比例")
        stats = summarize(values, valid_mask=valid)
        selected_values = values[valid]
        ratio = float(np.mean(selected_values > threshold))
        mask_source = _qa_mask_source(candidate)
        if profile.get("qa_kind") == "bitmask":
            bad_bits = profile.get("qa_bad_bits") or []
            qa_int = np.nan_to_num(qa_array, nan=0).astype(np.uint16)
            masked_counts = {f"bit{bit}": int(np.sum(aoi & (np.bitwise_and(qa_int, 1 << int(bit)) != 0))) for bit in bad_bits}
        else:
            masked_counts = {str(code): int(np.sum(aoi & (qa_array == code))) for code in (0, 1, 2, 3, 8, 9, 10, 11)}
        return {
            "available": True, "index": index, "method": definition["formula"],
            "bands": list(definition["bands"]), "threshold": threshold, "threshold_method": "fixed",
            "thresholded_pixel_ratio": round(ratio, 4), "thresholded_percent": round(ratio * 100, 2),
            "alternative_thresholds": [{"threshold": round(threshold + delta, 4), "ratio": round(float(np.mean(selected_values > threshold + delta)), 4)} for delta in (-0.05, -0.02, 0.02, 0.05)],
            "sample_size_px": valid_count, "aoi_pixel_count": aoi_count,
            "valid_pixel_ratio": round(valid_count / aoi_count, 4), "mean": stats["mean"], "min": stats["min"], "max": stats["max"],
            "mask_source": f"{mask_source}+nodata+reflectance_range" + ("+aoi" if polygon else ""),
            "masked_class_counts": masked_counts,
            "aoi": polygon or bbox, "polygon_clipped": bool(polygon), "measurement_grade": "screening",
            "data_contract": {"source": collection, "assets": selected,
                              "grid": {"bbox": bbox, "shape": list(qa_array.shape)},
                              "radiometry": {name: (asset.get("raster:bands") or [{}])[0] for name, asset in selected.items() if name != qa_band},
                              "qa_resampling": "nearest", "spectral_resampling": "bilinear"},
            "limitations": ["仅统计共同有效像元", "阈值分类并非地物真值", "输出网格经过重采样，不能代替原生分辨率面积测量"],
            "change_detection_allowed": False,
        }
    except Exception as exc:
        return {"available": False, "index": index, "method": definition["formula"],
                "reason": str(exc)[:180] if isinstance(exc, ValueError) else "光谱资产读取失败，请重试",
                "diagnostics": {"error_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}}
