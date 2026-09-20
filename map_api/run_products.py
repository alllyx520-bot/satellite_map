"""Run-owned raster products. Files are immutable per attempt and owner-protected."""
import hashlib
from io import BytesIO
from pathlib import Path

import numpy as np
import tifffile
from django.conf import settings
from PIL import Image

from .remote_sensing_indices import INDEX_DEFINITIONS, compute_index, summarize, change_summary
from .imagery_sources.earth_search import get_collection_profile
from .utils.agent_tools import (
    _qa_valid_mask, _radiometry_for, _sign_asset_for_collection,
    fetch_cog_bbox_array, polygon_mask_for_bbox,
)


def product_path(run_id, name):
    root = (Path(settings.MEDIA_ROOT) / "run_artifacts" / str(run_id)).resolve()
    path = (root / name).resolve()
    if path.parent != root or not name or Path(name).name != name:
        raise ValueError("运行产物路径无效")
    return path


def write_product(claim, suffix, content):
    name = f"v{claim.lease_plan_version}-{claim.step_id}-{claim.lease_token}.{suffix}"
    path = product_path(claim.run_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A stale worker writes only its unique attempt path, never a newer result.
    with path.open("xb") as handle:
        handle.write(content)
    return {"file_name": name, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}


def read_product(run_id, product):
    content = product_path(run_id, product["file_name"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != product["sha256"]:
        raise ValueError("运行产物校验失败，请重新生成")
    return content


def save_arrays(claim, arrays):
    buf = BytesIO()
    np.savez_compressed(buf, **arrays)
    return write_product(claim, "npz", buf.getvalue())


def load_arrays(run_id, product):
    with np.load(BytesIO(read_product(run_id, product)), allow_pickle=False) as arrays:
        return {key: arrays[key] for key in arrays.files}


def read_scene_assets(periods, bbox, indices, *, before_request, size=256, collection="sentinel-2-l2a"):
    # profile 驱动(2026-09-11):QA 波段、波段别名、辐射校准覆盖、PC 资产签名全部按 collection 解析,
    # 与 legacy agent 路径(spectral_products/agent_tools)保持同一语义;landsat 的 nir→nir08、
    # qa_pixel 位掩膜、SAS 签名都在这里生效。
    profile = get_collection_profile(collection)
    qa_band = profile.get("qa_band") or "scl"
    aliases = profile.get("band_aliases") or {}
    radiometry = _radiometry_for(profile)

    def resolve(band):
        return aliases.get(band) or {"swir": "swir16", "swir2": "swir22"}.get(band, band)

    required = {"red", "green", "blue", qa_band}
    for index in indices:
        required.update(INDEX_DEFINITIONS[index]["bands"])
    result = {}
    contracts = []
    for period, candidates in enumerate(periods):
        dates = {candidate["acquired_at"][:10] for candidate in candidates}
        if len(dates) != 1:
            raise ValueError("同一时相不能包含跨日期拼接场景")
        for position, candidate in enumerate(candidates):
            selected = {band: candidate["assets"].get(resolve(band)) for band in required}
            missing = [band for band, asset in selected.items() if not asset or not asset.get("href")]
            if missing:
                raise ValueError("场景缺少必需资产：" + ", ".join(sorted(missing)))
            for band, asset in sorted(selected.items()):
                before_request()
                array = fetch_cog_bbox_array(_sign_asset_for_collection(asset, profile), bbox, None,
                                             size=size, kind="scl" if band == qa_band else "reflectance",
                                             verify_grid=True, radiometry=radiometry)
                if array.shape != (size, size):
                    raise ValueError("场景输出网格尺寸不一致")
                result[f"p{period}_s{position}_{band}"] = array
            contracts.append({"period": period, "position": position, "scene_id": candidate["product_id"],
                              "acquired_at": candidate["acquired_at"], "assets": selected})
    return result, {"projection": "EPSG:4326", "shape": [size, size], "bbox": bbox,
                    "collection": collection, "qa_band": qa_band,
                    "qa_resampling": "nearest", "spectral_resampling": "bilinear", "scenes": contracts}


def apply_masks(arrays, contract, polygon):
    shape = tuple(contract["shape"])
    aoi = polygon_mask_for_bbox(polygon, contract["bbox"], shape) if polygon else np.ones(shape, dtype=bool)
    if not aoi.any():
        raise ValueError("AOI 内没有可统计像元")
    output = {"aoi": aoi}
    stats = []
    periods = sorted({scene["period"] for scene in contract["scenes"]})
    qa_band = contract.get("qa_band") or "scl"
    qa_profile = get_collection_profile(contract.get("collection") or "")
    for period in periods:
        valid = np.zeros(shape, dtype=bool)
        scenes = [scene for scene in contract["scenes"] if scene["period"] == period]
        bands = set(scenes[0]["assets"]) - {qa_band}
        combined = {band: np.full(shape, np.nan, dtype=np.float32) for band in bands}
        for scene in scenes:
            prefix = f"p{period}_s{scene['position']}_"
            scl = arrays[prefix + qa_band]
            mask = aoi & _qa_valid_mask(scl, qa_profile)
            for band in bands:
                values = arrays[prefix + band]
                # float32 边界容差:校准后 0/1 边界可能落在 -1e-9 量级,不应剔除
                mask &= np.isfinite(values) & (values >= -1e-6) & (values <= 1 + 1e-6)
            take = mask & ~valid
            for band in bands:
                combined[band][take] = arrays[prefix + band][take]
            valid |= take
        count = int(valid.sum())
        ratio = count / int(aoi.sum())
        if count < 1024 or ratio < 0.6:
            raise ValueError("AOI 有效像元少于 1024 或低于 60%，不能输出指标比例")
        output.update({f"p{period}_{band}": values for band, values in combined.items()})
        output[f"p{period}_valid"] = valid
        stats.append({"period": period, "valid_pixel_count": count, "aoi_pixel_count": int(aoi.sum()),
                      "valid_pixel_ratio": round(ratio, 4), "mask_source": "SCL(4,5,6,7)+nodata+reflectance_range+AOI",
                      "composite_policy": "first_jointly_valid_pixel_same_date", "overlap_counted_once": True})
    return output, stats


def calculate_products(arrays, indices, periods):
    output, summaries = {}, []
    for period in range(periods):
        for index in indices:
            bands = {band: arrays[f"p{period}_{band}"] for band in INDEX_DEFINITIONS[index]["bands"]}
            values, valid = compute_index(index, bands, valid_mask=arrays[f"p{period}_valid"])
            aoi_count = int(arrays["aoi"].sum())
            count = int(valid.sum())
            if count < 1024 or count / aoi_count < 0.6:
                raise ValueError("指数计算后的有效像元不足")
            threshold = 0.3 if index == "ndvi" else -0.1 if index == "nbr" else 0.1
            stats = summarize(values, valid_mask=valid, threshold=threshold)
            sensitivity = [{"threshold": round(threshold + delta, 4), "ratio": round(float(np.mean(values[valid] > threshold + delta)), 4)}
                           for delta in (-0.05, -0.02, 0.02, 0.05)]
            stats.update({"available": True, "period": period, "index": index, "method": INDEX_DEFINITIONS[index]["formula"],
                          "bands": list(INDEX_DEFINITIONS[index]["bands"]), "threshold": threshold,
                          "thresholded_percent": round(stats["above_threshold_ratio"] * 100, 2),
                          "valid_pixel_ratio": round(count / aoi_count, 4), "aoi_pixel_count": aoi_count,
                          "threshold_sensitivity": sensitivity, "measurement_grade": "screening",
                          "limitations": ["比例仅相对于 AOI 内有效像元", "阈值分类并非地物真值", "网格重采样不等于原生分辨率面积测量"]})
            output[f"p{period}_{index}"] = values
            output[f"p{period}_{index}_valid"] = valid
            summaries.append(stats)
    return output, summaries


def compare_products(arrays, indices, aoi_count):
    summaries, output = [], {}
    for index in indices:
        before, after = arrays[f"p0_{index}"], arrays[f"p1_{index}"]
        common = arrays[f"p0_{index}_valid"] & arrays[f"p1_{index}_valid"]
        if int(common.sum()) < 1024 or common.sum() / aoi_count < 0.6:
            raise ValueError("两期共同有效像元少于 1024 或低于 AOI 的 60%")
        summary = change_summary(before, after, valid_mask=common, min_delta=0.1)
        summary.update({"index": index, "method": "after-before", "common_mask": True,
                        "aoi_valid_pixel_ratio": round(float(common.sum() / aoi_count), 4),
                        "limitations": ["仅比较两期共同有效像元", "季节与观测条件可能影响指数差值"]})
        summaries.append(summary)
        output[index] = np.where(common, after - before, np.nan)
    return output, summaries


def geotiff_bytes(values, bbox):
    height, width = values.shape
    sx = (bbox["max_lng"] - bbox["min_lng"]) / width
    sy = (bbox["max_lat"] - bbox["min_lat"]) / height
    keys = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
    buf = BytesIO()
    tifffile.imwrite(buf, values.astype(np.float32), photometric="minisblack", metadata=None,
                     extratags=[(33550, "d", 3, (sx, sy, 0), False),
                                (33922, "d", 6, (0, 0, 0, bbox["min_lng"], bbox["max_lat"], 0), False),
                                (34735, "H", len(keys), keys, False), (42113, "s", 0, "nan", False)])
    return buf.getvalue()


def rgb_preview(arrays, period):
    rgb = np.stack([arrays[f"p{period}_{band}"] for band in ("red", "green", "blue")], axis=-1)
    rgb = (np.clip(np.nan_to_num(rgb) / 0.35, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
    buf = BytesIO()
    Image.fromarray(rgb).save(buf, "PNG")
    return buf.getvalue()
