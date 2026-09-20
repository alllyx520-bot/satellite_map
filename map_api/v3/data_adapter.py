"""I/O boundary for V3 data products.

The model supplies only durable references (attachment/scene/product/date/AOI).  This
module reads bytes itself, persists every result, and never accepts model-provided
pixel arrays.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, timezone
from pathlib import Path

import numpy as np
import requests
from django.conf import settings
from django.db import transaction
from jsonschema import Draft202012Validator, ValidationError

from ..imagery_sources import get_provider_for_collection
from ..models import ImageryScene, RunArtifact, RunEvidence, SpatialAttachment
from ..orchestrator import _agent_fetch_mapbox, _agent_fetch_sentinel
from ..utils import external_data
from ..utils.agent_tools import _sign_asset_for_collection
from ..imagery_sources.earth_search import get_collection_profile
from .raster_products import compute_attachment, ingest_cogs, ingest_cog_mosaic
from .runtime import ToolInterrupted
from . import assets


def _root():
    root = (Path(settings.MEDIA_ROOT) / "v3").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _json(value):
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, dict): return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json(v) for v in value]
    return value


def _safe_write(relative, content):
    path = (_root() / relative).resolve()
    if _root() not in path.parents: raise ValueError("产物路径无效")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _artifact(run, kind, title, content, mime, metadata=None, evidence_refs=None):
    key = uuid.uuid4().hex
    ext = ".json" if mime == "application/json" else ".tif"
    relative = f"{run.id}/{key}{ext}"
    _safe_write(relative, content)
    return RunArtifact.objects.create(run=run, artifact_id="v3-data-" + key, kind=kind, title=title,
        uri=f"/api/v3/artifacts/{key}/download", mime_type=mime,
        metadata={**(metadata or {}), "relative_path": relative, "sha256": hashlib.sha256(content).hexdigest()},
        evidence_refs=evidence_refs or [])


def _artifact_file(run, kind, title, source, mime, metadata=None, evidence_refs=None):
    """Persist an already-streamed artifact without materialising it in RAM."""
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("产物临时文件不可用")
    key = uuid.uuid4().hex
    ext = ".json" if mime == "application/json" else ".tif"
    relative = f"{run.id}/{key}{ext}"
    target = (_root() / relative).resolve()
    if _root() not in target.parents:
        raise ValueError("产物路径无效")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                digest.update(chunk); outgoing.write(chunk)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
    return RunArtifact.objects.create(run=run, artifact_id="v3-data-" + key, kind=kind, title=title,
        uri=f"/api/v3/artifacts/{key}/download", mime_type=mime,
        metadata={**(metadata or {}), "relative_path": relative, "sha256": digest.hexdigest()},
        evidence_refs=evidence_refs or [])


def _evidence(run, name, summary, *, scene_id="", aoi=None, contract=None, limitations=None, asset_id=""):
    return RunEvidence.objects.create(run=run, evidence_id="v3-data-" + uuid.uuid4().hex, kind="computed_metric",
        scene_id=str(scene_id or ""), asset_id=asset_id, metric=name, value=_json(summary), method=name,
        aoi=aoi, data_contract=contract or {}, limitations=limitations or [])


INDEX_PRODUCTS = ["ndvi", "ndwi", "mndwi", "ndbi", "bsi", "ndsi", "nbr"]
RASTER_COLLECTIONS = ["sentinel-2-l2a", "sentinel-2-c1-l2a", "sentinel-1-grd", "cop-dem-glo-30", "landsat-c2-l2"]
BASEMAP_COLLECTIONS = ["mapbox", "tianditu", "esri"]
EXTERNAL_PRODUCTS = ["water_baseline", "landcover", "osm", "weather", "fire_detections"]
BBOX_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "WGS84 经纬度范围，使用 min_lng/min_lat/max_lng/max_lat；经度在前。不是图像像素坐标。",
    "required": ["min_lng", "min_lat", "max_lng", "max_lat"],
    "properties": {
        "min_lng": {"type": "number", "minimum": -180, "maximum": 180, "description": "西边界经度"},
        "min_lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "南边界纬度"},
        "max_lng": {"type": "number", "minimum": -180, "maximum": 180, "description": "东边界经度，须大于 min_lng"},
        "max_lat": {"type": "number", "minimum": -90, "maximum": 90, "description": "北边界纬度，须大于 min_lat"},
    },
    "examples": [{"min_lng": 126.5, "min_lat": 45.6, "max_lng": 126.8, "max_lat": 45.9}],
}
DATE_SCHEMA = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "公历日期 YYYY-MM-DD"}


def data_specs():
    def schema(properties, required):
        return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}

    scene_properties = {
        "collection": {"type": "string", "enum": RASTER_COLLECTIONS}, "bbox": BBOX_SCHEMA,
        "date_start": {**DATE_SCHEMA, "description": "起始日期，包含当日；DEM 为静态地形，不按日期筛选"},
        "date_end": {**DATE_SCHEMA, "description": "结束日期，包含当日，须不早于 date_start"},
        "max_cloud": {"type": "number", "minimum": 0, "maximum": 100, "description": "整景云量百分比上限，默认 30；局部 QA 仍需另行检查，SAR/DEM 不使用此筛选"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "最多返回/检查的候选数，默认 30；达到此数不代表完整时序"},
    }
    return [
        {"name": "search_scenes", "description": "按 collection、日期和 WGS84 bbox 检索候选，返回范围覆盖和资产名称；完整 STAC 保存为产物。结果按最新优先，有数量上限；统计整个季节时按月份检索并去重。", "schema": schema(scene_properties, ["collection", "bbox"])},
        {"name": "retrieve_imagery", "description": "取得原始波段及 QA 并注册为当前会话的可查看附件。可指定检索得到的 item_id，同时传其拍摄日期以缩小检索；或 exclude_item_ids 排除不合格候选。底图 collection 可用 mapbox/tianditu/esri，但日期未知、不能算光谱指数。", "schema": schema({**scene_properties,
            "collection": {"type": "string", "enum": RASTER_COLLECTIONS + BASEMAP_COLLECTIONS},
            "source": {"type": "string", "enum": BASEMAP_COLLECTIONS, "description": "仅底图适用，必须与 collection 相同；通常省略"},
            "item_id": {"type": "string", "minLength": 1, "maxLength": 240},
            "exclude_item_ids": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 240}, "maxItems": 100},
        }, ["collection", "bbox"])},
        {"name": "compute_product", "description": "从已注册 GeoTIFF 附件的真实波段、校准与 QA 逐块计算产品；先 retrieve_imagery，产品必须属于附件声明的 products。", "schema": schema({"attachment_id": {"type": "string", "minLength": 1}, "product": {"type": "string", "enum": INDEX_PRODUCTS + ["sar_backscatter", "dem_terrain", "landsat_surface_temperature"]}}, ["attachment_id", "product"])},
        {"name": "compare_two_date_change", "description": "在同源、同产品、带可靠日期和 QA 的两期 GeoTIFF 上逐块计算指数差异；输出变化候选，不能当作地物真值。", "schema": {"type": "object", "additionalProperties": False, "required": ["reference_attachment_id", "comparison_attachment_id", "product"], "properties": {"reference_attachment_id": {"type": "string"}, "comparison_attachment_id": {"type": "string"}, "product": {"enum": ["ndvi", "ndwi", "mndwi", "ndbi", "bsi", "ndsi", "nbr"]}, "min_delta": {"type": "number", "exclusiveMinimum": 0, "maximum": 2}, "max_candidates": {"type": "integer", "minimum": 1, "maximum": 64}}}},
        {"name": "external_evidence", "description": "查询 WGS84 bbox 的外部背景。product: water_baseline=JRC GSW 1984–2021 水体出现频率；landcover=WorldCover 2021；osm=现有地图要素；weather=气象，date 单日或 date_start/date_end 区间；fire_detections=FIRMS 最近 1–5 日火点。历史背景不能当作当前观测。", "schema": schema({
            "product": {"type": "string", "enum": EXTERNAL_PRODUCTS}, "bbox": BBOX_SCHEMA,
            "date": {**DATE_SCHEMA, "description": "仅 weather：单日；与 date_start/date_end 二选一"},
            "date_start": {**DATE_SCHEMA, "description": "仅 weather：历史区间起点，必须与 date_end 同时给出"},
            "date_end": {**DATE_SCHEMA, "description": "仅 weather：历史区间终点，区间不超过 366 日"},
            "days": {"type": "integer", "minimum": 1, "maximum": 5, "description": "仅 fire_detections：最近天数，默认 3"},
            "source": {"type": "string", "enum": ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"], "description": "仅 fire_detections：FIRMS 传感器来源"},
        }, ["product", "bbox"])},
    ]


def _validate_arguments(name, args):
    specification = next((item for item in data_specs() if item["name"] == name), None)
    if specification is None:
        raise ValueError("未知数据工具")
    try:
        Draft202012Validator(specification["schema"]).validate(args)
    except ValidationError as exc:
        field = ".".join(str(part) for part in exc.absolute_path) or "arguments"
        correction = "；bbox 必须使用 min_lng/min_lat/max_lng/max_lat" if field == "bbox" or field.startswith("bbox.") else ""
        raise ValueError(f"{field}: {exc.message}{correction}") from exc
    bbox = args.get("bbox")
    if bbox:
        if not all(np.isfinite(value) for value in bbox.values()):
            raise ValueError("bbox 经纬度必须是有限数值")
        if bbox["min_lng"] >= bbox["max_lng"] or bbox["min_lat"] >= bbox["max_lat"]:
            raise ValueError("bbox 必须 min_lng < max_lng 且 min_lat < max_lat；跨日期变更线请拆成两个范围")
    dates = {}
    for key in ("date", "date_start", "date_end"):
        if key in args:
            try:
                dates[key] = date.fromisoformat(args[key])
            except ValueError as exc:
                raise ValueError(f"{key} 必须是有效公历日期 YYYY-MM-DD") from exc
    if dates.get("date_start") and dates.get("date_end") and dates["date_start"] > dates["date_end"]:
        raise ValueError("date_end 不能早于 date_start")
    if name == "retrieve_imagery":
        collection = args["collection"]
        if "source" in args and args["source"] != collection:
            raise ValueError("source 仅用于底图且必须与 collection 相同；原始卫星影像请省略 source")
        if collection in BASEMAP_COLLECTIONS and any(key in args for key in ("date_start", "date_end", "item_id", "exclude_item_ids")):
            raise ValueError("底图拍摄日期未知且不支持场景筛选；时间问题请使用 Sentinel/Landsat collection")
    if name == "external_evidence":
        product = args["product"]
        if dates and product != "weather":
            raise ValueError("date/date_start/date_end 仅适用于 weather；water_baseline 与 landcover 是固定历史基线，FIRMS 仅支持最近 days 天")
        if product != "fire_detections" and any(key in args for key in ("source", "days")):
            raise ValueError("source/days 仅适用于 fire_detections；外部数据源由 product 自动选择")
        if "date" in dates and ("date_start" in dates or "date_end" in dates):
            raise ValueError("weather 使用 date 单日或 date_start/date_end 区间，不能同时提供")
        if ("date_start" in dates) != ("date_end" in dates):
            raise ValueError("weather 的 date_start 与 date_end 必须同时提供")
        if dates.get("date_start") and (dates["date_end"] - dates["date_start"]).days >= 366:
            raise ValueError("weather 一次最多查询 366 日；较长时段请分年查询")


def source_capabilities():
    """Static capabilities only; `configured` is intentionally not inferred from secrets."""
    return [
        {"source": "sentinel-2-l2a", "products": ["ndvi", "ndwi", "mndwi", "ndbi", "bsi", "ndsi", "nbr"], "temporal": True, "requires_credential": False},
        {"source": "sentinel-1-grd", "products": ["sar_backscatter"], "temporal": "same_orbit_direction_and_incidence_required", "requires_credential": False},
        {"source": "cop-dem-glo-30", "products": ["dem_terrain"], "temporal": False, "requires_credential": False},
        {"source": "landsat-c2-l2", "products": ["landsat_surface_temperature", "ndvi", "ndwi", "mndwi", "ndbi", "bsi", "ndsi", "nbr"], "temporal": True, "requires_credential": False},
        {"source": "firms", "products": ["fire_detections"], "temporal": "1-5 days", "requires_credential": True},
        {"source": "open-meteo", "products": ["weather"], "temporal": True, "requires_credential": False},
        {"source": "jrc-gsw", "products": ["water_baseline"], "temporal": "1984-2021 baseline", "requires_credential": False},
        {"source": "esa-worldcover", "products": ["landcover"], "temporal": "2021 baseline", "requires_credential": False},
        {"source": "openstreetmap", "products": ["osm"], "temporal": "community-maintained", "requires_credential": False},
    ]


def _attachment(context, value):
    if "attachment_ids" in context and str(value) not in context["attachment_ids"]:
        raise ValueError("attachment_id 尚未发送到当前问题")
    try: attachment = SpatialAttachment.objects.get(pk=value, conversation=context["conversation"])
    except SpatialAttachment.DoesNotExist: raise ValueError("attachment_id 不属于当前会话")
    if attachment.status != "ready" or not attachment.file_path: raise ValueError("附件尚未就绪")
    return attachment


def _persist_product(run, product, result, contract):
    summary = {k: v for k, v in result.items() if not isinstance(v, np.ndarray)}
    evidence = _evidence(run, product, summary.get("summary", summary), scene_id=contract.get("attachment_id", ""),
                         aoi=contract.get("bbox"), contract=contract, limitations=result.get("limitations", []))
    artifact = _artifact(run, "data_product", product, json.dumps(_json(summary), ensure_ascii=False).encode(), "application/json",
                         {"product": product}, [evidence.evidence_id])
    return {"evidence_id": evidence.evidence_id, "artifact_id": artifact.id, "summary": _json(summary)}


def _compute(args, context):
    a = _attachment(context, args["attachment_id"])
    product = args["product"]
    declared = a.metadata.get("products") or []
    if declared and product not in declared:
        raise ValueError("该附件源元数据未声明支持此数据产品")
    contract = {"bbox": a.bbox, "crs": a.crs, "transform": a.transform, "attachment_id": str(a.id), "metadata": a.metadata}
    result = compute_attachment(a.file_path, a.metadata, product, bbox=a.bbox)
    return _persist_product(context["run"], product, result, contract)


def _search(args, context):
    collection, bbox = args["collection"], args["bbox"]
    limit = args.get("limit", 30)
    candidates = get_provider_for_collection(collection).search(bbox, start_date=args.get("date_start"), end_date=args.get("date_end"), max_cloud=args.get("max_cloud", 30), limit=limit, collection=collection)
    raw = [candidate.as_dict() for candidate in candidates]
    artifact = _artifact(context["run"], "scene_search", "STAC scenes", json.dumps(_json(raw), ensure_ascii=False).encode(), "application/json", {"collection": collection, "bbox": bbox})
    items = []
    for candidate in raw:
        item = {key: candidate[key] for key in ("item_id", "collection", "source", "acquired_at", "bbox", "gsd_m", "cloud_percent") if key in candidate}
        item["asset_keys"] = list((candidate.get("assets") or {}).keys())
        scene_bbox = candidate.get("bbox") or {}
        if all(key in scene_bbox for key in ("min_lng", "min_lat", "max_lng", "max_lat")):
            overlap_x = max(0, min(bbox["max_lng"], scene_bbox["max_lng"]) - max(bbox["min_lng"], scene_bbox["min_lng"]))
            overlap_y = max(0, min(bbox["max_lat"], scene_bbox["max_lat"]) - max(bbox["min_lat"], scene_bbox["min_lat"]))
            item["bbox_overlap_ratio"] = round(overlap_x * overlap_y / ((bbox["max_lng"] - bbox["min_lng"]) * (bbox["max_lat"] - bbox["min_lat"])), 4)
        items.append(item)
    return {"items": _json(items), "artifact_id": artifact.id, "collection": collection, "bbox": bbox,
            "returned_count": len(items), "limit": limit, "possibly_truncated": len(items) >= limit,
            "coverage_note": "bbox_overlap_ratio 仅表示场景外接矩形交集比例，实际有效像元和局部云量需下载后检查",
            "next_step": "使用 retrieve_imagery，传 collection、bbox、item_id 及该景拍摄日期的 date_start/date_end；结果为可定位并计算的附件" if items else "无候选；检查时段、扩大云量上限或选择其他 collection"}


def _acquisition_day(candidate):
    """A day is the only permitted temporal key for an optical mosaic."""
    acquired = getattr(candidate, "acquired_at", None)
    if not acquired or not hasattr(acquired, "date"):
        return None
    if getattr(acquired, "tzinfo", None) is None:
        return None
    return acquired.astimezone(timezone.utc).date().isoformat()


def _selected_assets(candidate, names, profile):
    selected = []
    for name in names:
        raw_asset = (getattr(candidate, "assets", {}) or {}).get(name) or {}
        if not raw_asset.get("href"):
            # ST is absent on valid optical-only Landsat L2SR scenes. Product
            # capabilities are derived from the bands actually present below.
            continue
        signed_asset = _sign_asset_for_collection(raw_asset, profile)
        # Signed URLs are credentials. Provenance remains the original STAC href.
        signed_asset["source_href"] = raw_asset["href"]
        selected.append((name, signed_asset))
    if not selected:
        raise ValueError(f"场景 {candidate.item_id} 没有原始 COG 资产")
    return selected


def _same_day_scene_groups(candidates, collection):
    """Keep temporal mosaics same-source and same calendar day, never implicitly mix dates."""
    if collection in {"cop-dem-glo-30", "sentinel-1-grd"}:
        return [[candidate] for candidate in candidates]
    groups = {}
    for candidate in candidates:
        day = _acquisition_day(candidate)
        if day:
            groups.setdefault(day, []).append(candidate)
    # A legacy/mock candidate without an acquisition time can still be ingested as
    # one scene, but it is explicitly not eligible for a multi-scene mosaic.
    if not groups and len(candidates) == 1:
        return [[candidates[0]]]
    return [groups[day] for day in sorted(groups, reverse=True)]


def _retrieve(args, context):
    bbox, collection = args["bbox"], args["collection"]
    if collection in {"sentinel-2-l2a", "sentinel-2-c1-l2a", "sentinel-1-grd", "cop-dem-glo-30", "landsat-c2-l2"}:
        provider = get_provider_for_collection(collection)
        candidates = provider.search(bbox, start_date=args.get("date_start"), end_date=args.get("date_end"), max_cloud=args.get("max_cloud", 30), limit=args.get("limit", 30), collection=collection)
        excluded = set(args.get("exclude_item_ids") or [])
        candidates = [c for c in candidates if c.item_id not in excluded and (not args.get("item_id") or c.item_id == args["item_id"])
                      and getattr(c, "collection", collection) == collection]
        if not candidates: raise ValueError("没有满足条件的公开原始场景")
        profile = get_collection_profile(collection)
        names = {"sentinel-2-l2a": ["red","green","blue","nir","swir16","swir22","scl"], "sentinel-2-c1-l2a": ["red","green","blue","nir","swir16","swir22","scl"], "sentinel-1-grd": ["vv","vh"], "cop-dem-glo-30": ["data"], "landsat-c2-l2": ["red","green","blue","nir08","swir16","swir22","qa_pixel","st_b10"]}[collection]
        groups = _same_day_scene_groups(candidates, collection)
        if not groups:
            raise ValueError("候选影像缺少可靠拍摄日期，不能将多景暗混为同日覆盖")
        metadata = None; chosen = None; last_coverage_error = None
        for group in groups:
            # item_id is an explicit request for one item; it must never expand to
            # nearby acquisitions merely to fill an AOI.
            if args.get("item_id") and len(group) != 1:
                continue
            if len(group) == 1:
                scene_box = getattr(group[0], "bbox", None) or {}
                if all(key in scene_box for key in ("min_lng", "min_lat", "max_lng", "max_lat")) and not (
                        scene_box["min_lng"] <= bbox["min_lng"] and scene_box["min_lat"] <= bbox["min_lat"]
                        and scene_box["max_lng"] >= bbox["max_lng"] and scene_box["max_lat"] >= bbox["max_lat"]):
                    last_coverage_error = ValueError(
                        f"场景 {group[0].item_id} 外接矩形 {scene_box} 未完整覆盖请求 AOI；"
                        "请把 bbox 缩到该范围内，或省略 item_id 允许同日多景拼接")
                    continue
            target = assets._inside(assets._root() / "files" / f"{uuid.uuid4().hex}.tif")
            try:
                scene_assets = [{"item_id": candidate.item_id, "assets": _selected_assets(candidate, names, profile)} for candidate in group]
                target.parent.mkdir(parents=True, exist_ok=True)
                # Deterministic resume key: unsigned item ids/hrefs only, so
                # re-signed URLs (45 min expiry) do not break continuation.
                progress_key = hashlib.sha256(json.dumps({
                    "collection": collection,
                    "item_ids": sorted(candidate.item_id for candidate in group),
                    "bbox": [bbox["min_lng"], bbox["min_lat"], bbox["max_lng"], bbox["max_lat"]],
                    "bands": names,
                    "qa_bad_bits": profile.get("qa_bad_bits", [1, 2, 3, 4]),
                }, sort_keys=True).encode("utf-8")).hexdigest()[:16]
                products = []
                source_meta = {"collection": collection, "item_id": group[0].item_id, "product_id": group[0].product_id,
                               "acquired_at": group[0].acquired_at.isoformat() if group[0].acquired_at else None,
                               "source_item_ids": [candidate.item_id for candidate in group],
                               "source_acquisition_dates": sorted({_acquisition_day(candidate) for candidate in group if _acquisition_day(candidate)}),
                               "mosaic": len(group) > 1, "products": products,
                               "qa_bad_bits": profile.get("qa_bad_bits", [1, 2, 3, 4]),
                               "asset_name": "ST_B10" if collection == "landsat-c2-l2" else None}
                if collection == "cop-dem-glo-30":
                    source_meta["reference_datetime"] = source_meta["acquired_at"]; source_meta["acquired_at"] = None
                    source_meta["temporal_note"] = "DEM 集合参考日期不能当作单景拍摄日期；该产品用于地形背景，不用于两期影像变化。"
                if collection == "sentinel-1-grd" and (group[0].metadata or {}).get("calibration"):
                    source_meta["calibration"] = group[0].metadata["calibration"]
                metadata = ingest_cog_mosaic(scene_assets, bbox, target, profile=profile, source_metadata=source_meta, progress_key=progress_key)
                chosen = group
                break
            except ToolInterrupted as exc:
                try:
                    state = json.loads((target.parent / ".partial" / f"{progress_key}.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = None
                if state:
                    exc.partial_progress = {"done": len(state.get("done_windows") or []), "total": state.get("total_windows")}
                raise
            except ValueError as exc:
                target.unlink(missing_ok=True)
                last_coverage_error = exc
                if "完整覆盖 AOI" not in str(exc):
                    raise
        if metadata is None or chosen is None:
            detail = f"（{last_coverage_error}）" if last_coverage_error else ""
            raise ValueError(f"没有同日同源场景能完整覆盖 AOI；未创建部分覆盖影像{detail}") from last_coverage_error
        candidate = chosen[0]
        mapped = set(metadata["band_map"])
        index_requirements = {"ndvi": {"red", "nir"}, "ndwi": {"green", "nir"}, "mndwi": {"green", "swir"},
                              "ndbi": {"swir", "nir"}, "bsi": {"swir", "red", "nir", "blue"}, "ndsi": {"green", "swir"}, "nbr": {"nir", "swir2"}}
        if collection in {"sentinel-2-l2a", "sentinel-2-c1-l2a", "landsat-c2-l2"} and ({"scl"} <= mapped or {"qa_pixel"} <= mapped):
            metadata["products"] = [name for name, required in index_requirements.items() if required <= mapped]
        elif collection == "cop-dem-glo-30" and ({"dem"} <= mapped or {"elevation"} <= mapped):
            metadata["products"] = ["dem_terrain"]
        elif collection == "sentinel-1-grd" and "vv" in mapped and metadata.get("calibration"):
            metadata["products"] = ["sar_backscatter"]
        if collection == "landsat-c2-l2" and {"st", "qa_pixel"} <= mapped:
            metadata["products"].append("landsat_surface_temperature")
        attachment=SpatialAttachment.objects.create(owner_session_key=context["conversation"].owner_session_key,conversation=context["conversation"],name=f"{candidate.item_id}.tif",kind="geotiff",status="pending",file_path=str(target),size_bytes=target.stat().st_size,bbox=bbox,metadata=metadata)
        attachment=assets.process_attachment(attachment.id)
        if attachment.status!="ready": raise ValueError("原始资产规范化失败: "+attachment.error)
        return {"attachment": assets.attachment_payload(attachment)}
    else:
        scene = _agent_fetch_mapbox(bbox, collection)[0]
    source = Path(settings.MEDIA_ROOT) / "satellite_imgs" / scene.file_name
    if not source.is_file(): raise ValueError("影像下载未产生可用文件")
    target = assets._inside(assets._root() / "files" / f"{uuid.uuid4().hex}{source.suffix.lower()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    attachment = SpatialAttachment.objects.create(owner_session_key=context["conversation"].owner_session_key, conversation=context["conversation"], scene=scene,
        name=source.name, kind="geotiff" if source.suffix.lower() in {".tif", ".tiff"} else "image", status="pending", file_path=str(target), size_bytes=target.stat().st_size,
        bbox=bbox, metadata={**(scene.metadata or {}), "collection": collection, "scene_id": scene.id})
    attachment = assets.process_attachment(attachment.id)
    if attachment.status != "ready":
        raise ValueError("影像附件规范化失败: " + attachment.error)
    return {"attachment": assets.attachment_payload(attachment)}


def _external(args, context):
    bbox, product = args["bbox"], args["product"]
    funcs = {"water_baseline": external_data.query_water_baseline, "landcover": external_data.query_landcover_context,
             "osm": external_data.query_osm_context, "fire_detections": lambda area: external_data.summarize_firms_fires(external_data.query_firms_fires(area, days=args.get("days", 3), source=args.get("source") or external_data.FIRMS_DEFAULT_SOURCE))}
    if product == "weather":
        weather_options = {key: args[key] for key in ("date_start", "date_end") if key in args}
        data = external_data.query_weather_context((bbox["min_lat"] + bbox["max_lat"]) / 2, (bbox["min_lng"] + bbox["max_lng"]) / 2, args.get("date"), **weather_options)
    elif product in funcs: data = funcs[product](bbox)
    else: raise ValueError("外部证据 product 必须是 water_baseline/landcover/osm/weather/fire_detections")
    if data.get("available") is False:
        return {"error": {"code": "data_unavailable", "message": f"{product} 未返回有效数据；不能据此声称该区域没有水体或地物", "retryable": True}, "result": _json(data)}
    period = {key: args[key] for key in ("date", "date_start", "date_end") if key in args}
    source = {"weather": "open-meteo", "water_baseline": "jrc-gsw", "landcover": "esa-worldcover", "osm": "openstreetmap", "fire_detections": "firms"}[product]
    evidence = _evidence(context["run"], product, data, aoi=bbox,
        contract={"source": source, "product": product, "bbox": bbox, **period}, limitations=data.get("limitations", []))
    return {"evidence_id": evidence.evidence_id, "result": _json(data), "bbox": bbox, **period}


def execute(name, args, context):
    if not isinstance(args, dict) or any(k in args for k in ("arrays", "bands", "vv", "vh", "dem", "st", "qa_pixel", "band_map", "metadata")):
        return {"error": {"code": "reference_required", "message": "数据工具只接受 attachment_id 或 scene 引用；波段、校准和 QA 只能来自附件或源元数据"}}
    if not context or not context.get("run") or not context.get("conversation"):
        return {"error": {"code": "context_required", "message": "缺少 run 或 conversation 上下文"}}
    try:
        _validate_arguments(name, args)
        if name == "search_scenes": return {"result": _search(args, context)}
        if name == "retrieve_imagery": return {"result": _retrieve(args, context)}
        if name == "compute_product": return {"result": _compute(args, context)}
        if name == "compare_two_date_change":
            from .change_analysis import compare
            return {"result": compare(args, context, artifact_writer=_artifact, artifact_file_writer=_artifact_file, evidence_writer=_evidence)}
        if name == "external_evidence": return {"result": _external(args, context)}
        raise ValueError("未知数据工具")
    except ToolInterrupted as exc:
        progress = getattr(exc, "partial_progress", None) or {}
        done, total = progress.get("done"), progress.get("total")
        hint = f"（已完成 {done}/{total} 块）" if done is not None and total else ""
        return {"error": {"code": "ingest_interrupted",
                          "message": f"下载已在时限边界暂停并保留进度{hint}。用相同参数再次调用本工具将继续下载；也可缩小 bbox 或时段。",
                          "retryable": True}}
    except ValueError as exc: return {"error": {"code": "invalid_data_input", "message": str(exc), "retryable": False}}
    except requests.RequestException as exc:
        # Request URLs may contain signed assets or provider credentials. Return
        # only the response code and exception type, never requests' raw string.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        return {"error": {"code": "data_service_error", "message": f"数据服务请求失败（{detail}）；这是网络或远端响应问题。可缩小范围/时段或改用其他数据源", "retryable": status is None or status >= 500 or status in (408, 425, 429)}}


def registered_tools(Tool, schema):
    def handler(name):
        def call(args, context):
            result = execute(name, args, context)
            return result.get("result", result)
        return call
    return [Tool(item["name"], item["description"], item["schema"], handler(item["name"]),
                 timeout=900 if item["name"] in {"retrieve_imagery", "compare_two_date_change"} else 180,
                 recovery="uncertain_external" if item["name"] in {"search_scenes", "external_evidence"} else "replay_safe")
            for item in data_specs()]
