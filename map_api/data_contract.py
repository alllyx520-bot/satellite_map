"""统一影像数据契约与源能力矩阵。"""
from dataclasses import dataclass, asdict

from .imagery_sources.earth_search import get_collection_profile

CONTRACT_VERSION = 1

SOURCE_CAPABILITIES = {
    "sentinel2": {
        "visual_interpretation": True,
        "spectral_index": True,
        "physical_measurement": True,
        "change_detection": True,
        "small_target_detection": False,
    },
    "mapbox": {
        "visual_interpretation": True,
        "spectral_index": False,
        "physical_measurement": False,
        "change_detection": False,
        "small_target_detection": False,
    },
    "tianditu": {
        "visual_interpretation": True,
        "spectral_index": False,
        "physical_measurement": False,
        "change_detection": False,
        "small_target_detection": False,
    },
    "esri": {
        "visual_interpretation": True,
        "spectral_index": False,
        "physical_measurement": False,
        "change_detection": False,
        "small_target_detection": False,
    },
    # SAR 后向散射只做水体/宏观地物目视线索,不算光谱/物理测量/变化检测能力。
    "sentinel1": {
        "visual_interpretation": True,
        "spectral_index": False,
        "physical_measurement": False,
        "change_detection": False,
        "small_target_detection": False,
    },
    # 静态 DEM(2011-2015 基线),仅地形目视参考。
    "copdem": {
        "visual_interpretation": True,
        "spectral_index": False,
        "physical_measurement": False,
        "change_detection": False,
        "small_target_detection": False,
    },
    # Landsat C2 L2(30m 筛查级):L2 反射率支持光谱指数;变化检测仍受两期一致性门控。
    "landsat": {
        "visual_interpretation": True,
        "spectral_index": True,
        "physical_measurement": False,
        "change_detection": True,
        "small_target_detection": False,
    },
}


@dataclass(frozen=True)
class SceneDataContract:
    source: str
    scene_identity: dict
    temporal: dict
    spatial: dict
    assets: dict
    quality: dict
    capabilities: dict
    limitations: list
    version: int = CONTRACT_VERSION

    def as_dict(self):
        value = asdict(self)
        value["data_contract_version"] = value.pop("version")
        return value


def capabilities_for(source, *, temporal_consistency="single_scene", has_common_mask=False):
    result = dict(SOURCE_CAPABILITIES.get(source, {}))
    if source in ("sentinel2", "landsat"):
        # 单景/同日拼接本身不是变化检测输入；只有显式完成两期共同掩膜后才开放。
        result["change_detection"] = temporal_consistency == "two_date_aligned" and has_common_mask
    else:
        result["change_detection"] = False
    return result


def build_scene_contract(scene, *, assets=None, polygon=None):
    source = (getattr(scene, "source", "") or "").lower()
    metadata = dict(getattr(scene, "metadata", None) or {})
    temporal_consistency = metadata.get("temporal_consistency") or ("single_scene" if source != "sentinel2" else "unknown")
    native = metadata.get("native_resolution_m")
    if native is None and source == "sentinel2":
        # sentinel2 历史波段表保持现状兼容;其他源从 collection profile 表读取。
        native = {"blue": 10, "green": 10, "red": 10, "nir": 10, "swir": 20, "scl": 20, "visual": 10}
    if native is None:
        native = get_collection_profile(metadata.get("collection")).get("native_resolution_m")
    limitations = list(metadata.get("limitations") or [])
    if source in ("mapbox", "tianditu", "esri"):
        limitations.extend(["拍摄时间未知", "原生传感器 GSD 未知", "不可用于光谱指数、正式变化检测或物理测量"])
    elif source in ("sentinel1", "copdem", "landsat"):
        profile_limitations = get_collection_profile(metadata.get("collection")).get("limitations")
        if profile_limitations:
            limitations.append(profile_limitations)
    return SceneDataContract(
        source=source,
        scene_identity={"product_id": scene.product_id, "item_id": metadata.get("item_id", ""), "collection": metadata.get("collection", ""), "platform": metadata.get("platform"), "processing_baseline": metadata.get("processing_baseline")},
        temporal={"acquired_at": scene.acquired_at.isoformat() if scene.acquired_at else None, "published_at": scene.published_at.isoformat() if scene.published_at else None, "acquisition_date": scene.acquired_at.date().isoformat() if scene.acquired_at else None, "temporal_consistency": temporal_consistency},
        spatial={"requested_aoi": polygon, "requested_bbox": metadata.get("requested_bbox") or metadata.get("bbox"), "effective_bbox": {"min_lng": scene.min_lng, "min_lat": scene.min_lat, "max_lng": scene.max_lng, "max_lat": scene.max_lat}, "polygon": polygon or metadata.get("district_polygon"), "projection": metadata.get("proj_epsg") or metadata.get("proj:epsg"), "native_resolution_m": native, "pixel_spacing_m": metadata.get("pixel_spacing_m") or metadata.get("rendered_gsd_m") or scene.gsd_m, "render_size": metadata.get("render_size_px") or metadata.get("render_size")},
        assets=assets or metadata.get("assets") or {},
        quality={"cloud_percent": scene.cloud_percent, "target_coverage_ratio": metadata.get("target_coverage_ratio"), "valid_image_ratio": metadata.get("valid_image_ratio"), "nodata_ratio": metadata.get("nodata_ratio"), "masked_pixel_ratio": metadata.get("masked_pixel_ratio"), "mask_source": metadata.get("mask_source")},
        capabilities=capabilities_for(source, temporal_consistency=temporal_consistency, has_common_mask=bool(metadata.get("mask_source"))),
        limitations=limitations,
    ).as_dict()


def assess_requirements(source, requirements):
    capabilities = SOURCE_CAPABILITIES.get(source, {})
    missing = [key for key, needed in (requirements or {}).items() if needed and not capabilities.get(key, False)]
    status = "not_supported" if missing else "matched"
    return {"status": status, "source": source, "missing_capabilities": missing, "capabilities": capabilities}


def build_data_requirements(*, task=None, visual_detail=False, spectral_bands=False,
                            recent_acquisition=False, two_dates=False,
                            physical_measurement=False, area_ratio=False,
                            small_target_detection=False, minimum_native_resolution_m=None,
                            required_indices=None, minimum_valid_pixel_ratio=0.0,
                            aoi_geometry_required=False):
    """构造可序列化的数据需求声明，供源路由和 AnalysisRun 复用。"""
    return {
        "task": task,
        "needs_visual_detail": bool(visual_detail),
        "needs_spectral_bands": bool(spectral_bands),
        "needs_recent_acquisition": bool(recent_acquisition),
        "needs_two_dates": bool(two_dates),
        "needs_physical_measurement": bool(physical_measurement),
        "needs_area_ratio": bool(area_ratio),
        "needs_small_target_detection": bool(small_target_detection),
        "minimum_native_resolution_m": minimum_native_resolution_m,
        "required_indices": list(required_indices or []),
        "minimum_valid_pixel_ratio": float(minimum_valid_pixel_ratio or 0.0),
        "aoi_geometry_required": bool(aoi_geometry_required),
    }
