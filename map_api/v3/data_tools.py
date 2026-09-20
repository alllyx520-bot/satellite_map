"""Offline, auditable V3 raster data products.

These tools deliberately accept already-read arrays.  Network/STAC access remains in
providers, so a tool invocation can be replayed and unit-tested without live data.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from ..remote_sensing_indices import INDEX_DEFINITIONS, compute_index, summarize


DATA_TOOL_SPECS = [
    {"name": "spectral_index", "description": "计算经 QA 掩膜的 NDVI/NDWI/MNDWI/NDBI/BSI/NDSI/NBR。", "inputs": ["index", "bands", "valid_mask"]},
    {"name": "sar_backscatter", "description": "校准 Sentinel-1 VV/VH 后向散射并输出水面暗散射候选；时序比较需要相同相对轨道、方向和近似入射角。", "inputs": ["vv", "vh", "metadata"]},
    {"name": "dem_terrain", "description": "从 DEM 计算高程、坡度、坡向和局部起伏；需要像元间距或 bbox。", "inputs": ["dem", "pixel_size_m|bbox"]},
    {"name": "landsat_surface_temperature", "description": "用 Landsat Collection 2 ST DN、比例因子和 QA 生成地表温度筛查结果。", "inputs": ["st", "qa_pixel", "scale", "offset"]},
]


def _array(value, name):
    array = np.asarray(value, dtype=np.float32)
    if array.ndim != 2 or not array.size:
        raise ValueError(f"{name} 必须是非空二维栅格")
    return array


def _mask(value, shape):
    if value is None:
        return np.ones(shape, dtype=bool)
    mask = np.asarray(value, dtype=bool)
    if mask.shape != shape:
        raise ValueError("valid_mask 与栅格尺寸不一致")
    return mask


def spectral_index(args: dict[str, Any]):
    index = str(args.get("index", "")).lower()
    if index not in INDEX_DEFINITIONS or not INDEX_DEFINITIONS[index].get("implemented"):
        raise ValueError(f"未实现的指数: {index}")
    bands = {name: _array(value, name) for name, value in (args.get("bands") or {}).items()}
    required = INDEX_DEFINITIONS[index]["bands"]
    if any(name not in bands for name in required):
        raise ValueError("缺少指数所需波段")
    shape = bands[required[0]].shape
    if any(value.shape != shape for value in bands.values()):
        raise ValueError("波段网格尺寸不一致")
    valid = _mask(args.get("valid_mask"), shape)
    for name in required:
        valid &= np.isfinite(bands[name]) & (bands[name] >= -1e-6) & (bands[name] <= 1 + 1e-6)
    values, valid = compute_index(index, bands, valid_mask=valid)
    if valid.sum() < int(args.get("min_valid_pixels", 16)):
        raise ValueError("共同有效像元不足")
    threshold = float(args.get("threshold", 0.3 if index == "ndvi" else -0.1 if index == "nbr" else 0.1))
    stats = summarize(values, valid_mask=valid, threshold=threshold)
    return {"available": True, "product": index, "method": INDEX_DEFINITIONS[index]["formula"], "values": values,
            "valid_mask": valid, "summary": stats, "threshold": threshold, "measurement_grade": "screening",
            "limitations": INDEX_DEFINITIONS[index].get("limitations", []) + ["阈值分类不是地物真值"]}


def _orbit_key(metadata):
    return (metadata.get("relative_orbit") or metadata.get("sat:relative_orbit"),
            metadata.get("orbit_direction") or metadata.get("sat:orbit_state"))


def sar_backscatter(args: dict[str, Any]):
    vv = _array(args.get("vv"), "vv")
    vh_value = args.get("vh")
    vh = _array(vh_value, "vh") if vh_value is not None else None
    if vh is not None and vh.shape != vv.shape:
        raise ValueError("VV/VH 网格尺寸不一致")
    meta = args.get("metadata") or {}
    if not meta.get("calibration"):
        raise ValueError("SAR 必须由数据源声明 calibration，不能默认假定 sigma0")
    calibration = str(meta["calibration"]).lower()
    if calibration not in {"linear_sigma0", "linear_gamma0", "db"}:
        raise ValueError("SAR 必须声明 calibration（linear_sigma0/linear_gamma0/db）")
    valid = _mask(args.get("valid_mask"), vv.shape) & np.isfinite(vv)
    vv_db = vv if calibration == "db" else np.where(vv > 0, 10 * np.log10(vv), np.nan)
    vh_db = None if vh is None else (vh if calibration == "db" else np.where(vh > 0, 10 * np.log10(vh), np.nan))
    valid &= np.isfinite(vv_db)
    water_threshold = float(args.get("water_vv_db_threshold", -18.0))
    water = valid & (vv_db <= water_threshold)
    result = {"available": True, "product": "sar_backscatter", "vv_db": vv_db, "valid_mask": valid,
              "water_candidate_mask": water, "summary": {"valid_pixel_count": int(valid.sum()), "water_candidate_ratio": round(float(water.sum() / max(1, valid.sum())), 4), "mean_vv_db": round(float(np.nanmean(vv_db[valid])), 3)},
              "orbit": {"relative_orbit": _orbit_key(meta)[0], "direction": _orbit_key(meta)[1], "incidence_angle_deg": meta.get("incidence_angle_deg")},
              "limitations": ["暗散射还可能是平滑地表或几何阴影，必须用光学或历史水体复核", "未做地形校正与斑点滤波时仅供筛查"]}
    if vh_db is not None:
        result["vh_db"] = vh_db
        result["vh_vv_ratio_db"] = vh_db - vv_db
    before_meta = args.get("before_metadata")
    if before_meta:
        before_key, after_key = _orbit_key(before_meta), _orbit_key(meta)
        if not all(before_key) or before_key != after_key:
            result["temporal_comparison_allowed"] = False
            result["temporal_block_reason"] = "SAR 两期必须有相同相对轨道和过境方向"
        else:
            before_angle, after_angle = before_meta.get("incidence_angle_deg"), meta.get("incidence_angle_deg")
            if before_angle is None or after_angle is None:
                result["temporal_comparison_allowed"] = False
                result["temporal_block_reason"] = "SAR 两期必须声明入射角"
            elif abs(float(before_angle) - float(after_angle)) > 1.0:
                result["temporal_comparison_allowed"] = False
                result["temporal_block_reason"] = "SAR 两期入射角差超过 1°"
            else:
                result["temporal_comparison_allowed"] = True
                before_vv = args.get("before_vv")
                if before_vv is not None:
                    before = _array(before_vv, "before_vv")
                    if before.shape != vv.shape:
                        raise ValueError("前后 SAR 网格尺寸不一致")
                    before_db = before if calibration == "db" else np.where(before > 0, 10 * np.log10(before), np.nan)
                    common = valid & np.isfinite(before_db)
                    if common.sum() < int(args.get("min_valid_pixels", 16)):
                        raise ValueError("SAR 两期共同有效像元不足")
                    delta = np.where(common, vv_db - before_db, np.nan)
                    result["vv_change_db"] = delta
                    result["summary"]["mean_vv_change_db"] = round(float(np.nanmean(delta[common])), 3)
    return result


def _pixel_size_m(args, shape):
    pixel = args.get("pixel_size_m")
    if pixel is not None:
        if isinstance(pixel, (int, float)):
            return float(pixel), float(pixel)
        return float(pixel[0]), float(pixel[1])
    bbox = args.get("bbox") or {}
    try:
        lat = (float(bbox["min_lat"]) + float(bbox["max_lat"])) / 2
        dx = abs(float(bbox["max_lng"]) - float(bbox["min_lng"])) * 111320 * math.cos(math.radians(lat)) / shape[1]
        dy = abs(float(bbox["max_lat"]) - float(bbox["min_lat"])) * 110574 / shape[0]
        return dx, dy
    except (KeyError, TypeError, ValueError):
        raise ValueError("DEM 需要 pixel_size_m 或 geographic bbox")


def dem_terrain(args: dict[str, Any]):
    dem = _array(args.get("dem"), "dem")
    dx, dy = _pixel_size_m(args, dem.shape)
    if dx <= 0 or dy <= 0:
        raise ValueError("DEM 像元间距必须为正")
    valid = _mask(args.get("valid_mask"), dem.shape) & np.isfinite(dem)
    # np.gradient gives dz/dy and dz/dx. Aspect is degrees clockwise from north.
    gy, gx = np.gradient(np.where(valid, dem, np.nan), dy, dx)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = (np.degrees(np.arctan2(gx, -gy)) + 360) % 360
    aspect[np.isclose(gx, 0, atol=1e-12) & np.isclose(gy, 0, atol=1e-12)] = np.nan
    padded = np.pad(np.where(valid, dem, np.nan), 1, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (3, 3))
    roughness = np.nanstd(windows, axis=(-2, -1))
    return {"available": True, "product": "dem_terrain", "elevation_m": dem, "slope_deg": slope, "aspect_deg": aspect,
            "roughness_m": roughness, "valid_mask": valid, "summary": {"valid_pixel_count": int(valid.sum()), "mean_elevation_m": round(float(np.nanmean(dem[valid])), 2), "mean_slope_deg": round(float(np.nanmean(slope[valid])), 2), "max_slope_deg": round(float(np.nanmax(slope[valid])), 2)},
            "pixel_size_m": [dx, dy], "measurement_grade": "screening", "limitations": ["坡度由 DEM 栅格导出，边缘和填洼区不稳定", "DEM 是静态地形背景，不能证明当前灾害或地表状态"]}


def landsat_surface_temperature(args: dict[str, Any]):
    metadata = args.get("metadata") or {}
    collection = metadata.get("collection")
    if collection != "landsat-c2-l2":
        raise ValueError("地表温度只接受 Landsat Collection 2 Level-2 ST 产品")
    asset_name = str(args.get("asset_name", "ST_B10"))
    if asset_name.upper() != "ST_B10":
        raise ValueError("地表温度必须使用 ST_B10，不能把单独热红外波段当作 LST")
    st = _array(args.get("st"), "st")
    qa = args.get("qa_pixel")
    if qa is None:
        raise ValueError("Landsat ST 必须提供同场景 QA_PIXEL，不能默认无云")
    valid = _mask(args.get("valid_mask"), st.shape) & np.isfinite(st) & (st > 0)
    qa = _array(qa, "qa_pixel").astype(np.uint16)
    if qa.shape != st.shape:
        raise ValueError("ST/QA 网格尺寸不一致")
    bad_bits = args.get("qa_bad_bits", [1, 2, 3, 4])
    bad = sum(1 << int(bit) for bit in bad_bits)
    valid &= (qa & bad) == 0
    scale, offset = float(args.get("scale", 0.00341802)), float(args.get("offset", 149.0))
    kelvin = np.where(valid, st * scale + offset, np.nan)
    celsius = kelvin - 273.15
    if valid.sum() < int(args.get("min_valid_pixels", 16)):
        raise ValueError("Landsat ST 共同有效像元不足")
    return {"available": True, "product": "landsat_surface_temperature", "temperature_kelvin": kelvin, "temperature_celsius": celsius, "valid_mask": valid,
            "summary": {"valid_pixel_count": int(valid.sum()), "mean_celsius": round(float(np.nanmean(celsius[valid])), 2), "min_celsius": round(float(np.nanmin(celsius[valid])), 2), "max_celsius": round(float(np.nanmax(celsius[valid])), 2)},
            "scale": scale, "offset": offset, "measurement_grade": "screening", "limitations": ["必须使用 Landsat Collection 2 ST 产品而非单独 L1 thermal band", "单次过境温度受时间、大气和地表发射率影响，不能单景判定热岛"]}


_TOOLS = {"spectral_index": spectral_index, "sar_backscatter": sar_backscatter, "dem_terrain": dem_terrain, "landsat_surface_temperature": landsat_surface_temperature}


def data_tool_specs():
    """Return copy-safe capability metadata for the V3 /capabilities endpoint."""
    return [dict(spec) for spec in DATA_TOOL_SPECS]


def execute_data_tool(name: str, args: dict[str, Any], run=None):
    """V3 harness adapter. It has no I/O; callers own array persistence and evidence."""
    tool = _TOOLS.get(str(name))
    if tool is None:
        return {"error": {"code": "unknown_data_tool", "message": f"未知数据工具: {name}"}}
    try:
        result = tool(args or {})
        if run is not None and hasattr(run, "record_data_tool"):
            run.record_data_tool(name, result)
        return {"result": result}
    except ValueError as exc:
        return {"error": {"code": "invalid_data_input", "message": str(exc)}}
