"""Streaming, QA-aware two-date spectral change analysis for V3 attachments.

The output is deliberately a *change candidate* layer.  A spectral difference is
not a labelled real-world change, so callers get the provenance and limitations
needed to request a visual or external-evidence review.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import uuid

import numpy as np

from ..models import SpatialAttachment, SpatialObservation
from ..remote_sensing_indices import INDEX_DEFINITIONS, compute_index
from .raster_products import _calibrate, _qa_valid


OPTICAL_COLLECTIONS = {"sentinel-2-l2a", "sentinel-2-c1-l2a", "landsat-c2-l2"}
CELL_SIZE = 128
MAX_CANDIDATE_GRID_CELLS = 4096


def _acquired_at(attachment):
    value = (attachment.metadata or {}).get("acquired_at")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("两期影像都必须有来源记录的 acquired_at，不能猜测日期")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("acquired_at 必须是可解析的 ISO-8601 日期时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("acquired_at 必须包含时区，避免把本地日期当作过境时间")
    return parsed.astimezone(timezone.utc)


def _scoped_attachment(ctx, attachment_id):
    try:
        attachment = SpatialAttachment.objects.get(pk=attachment_id)
    except (SpatialAttachment.DoesNotExist, ValueError) as exc:
        raise ValueError("影像附件不存在") from exc
    conversation = ctx["conversation"]
    sent = {str(value) for value in ctx.get("attachment_ids", [])}
    if (attachment.conversation_id != conversation.id or
            attachment.owner_session_key != conversation.owner_session_key or
            str(attachment.id) not in sent):
        raise ValueError("影像不在当前任务已发送的附件范围内")
    if attachment.status != "ready" or not attachment.file_path:
        raise ValueError("影像尚未就绪")
    return attachment


def _validate_pair(before, after, product):
    if before.kind != "geotiff" or after.kind != "geotiff":
        raise ValueError("普通 RGB 图片不能进行数值变化；只有明确配准后才能做视觉比较")
    if before.coordinate_space != "geographic" or after.coordinate_space != "geographic":
        raise ValueError("两期影像必须有可靠地理参考；普通 RGB 仅能在明确配准后做视觉比较")
    first_date, second_date = _acquired_at(before), _acquired_at(after)
    if first_date >= second_date:
        raise ValueError("reference_attachment_id 必须早于 comparison_attachment_id")
    first_meta, second_meta = before.metadata or {}, after.metadata or {}
    collection = first_meta.get("collection")
    if collection not in OPTICAL_COLLECTIONS or second_meta.get("collection") != collection:
        raise ValueError("两期必须是同一受信光学 collection，不能混用数据源或普通 RGB")
    # A collection fixes the sensor processing product; when providers expose a
    # more detailed product family it must also agree instead of being inferred.
    for key in ("processing_level", "product_family", "product_type"):
        one, two = first_meta.get(key), second_meta.get(key)
        if one is not None or two is not None:
            if not one or not two or str(one) != str(two):
                raise ValueError(f"两期来源的 {key} 必须明确且一致")
    if product not in INDEX_DEFINITIONS or not INDEX_DEFINITIONS[product].get("supports_change_detection"):
        raise ValueError("只支持具有变更检测约束的光谱指数")
    for attachment in (before, after):
        declared = (attachment.metadata or {}).get("products") or []
        if declared and product not in declared:
            raise ValueError("两期附件源元数据都必须声明支持该指数")
    return first_date, second_date, collection


def _band_values(dataset, metadata, indexes, window, *, destination=None):
    """Read one source block, optionally reprojecting it to reference window."""
    if destination is None:
        return {name: _calibrate(name, dataset.read(index, window=window), metadata)
                for name, index in indexes.items()}
    import rasterio
    from rasterio.warp import reproject
    values = {}
    for name, index in indexes.items():
        raw = np.full(destination["shape"], np.nan, dtype=np.float32)
        # rasterio.band keeps the source lazy: GDAL reads only the source area
        # contributing to this reference block instead of materialising a scene.
        reproject(source=rasterio.band(dataset, index), destination=raw, src_transform=dataset.transform,
                  src_crs=dataset.crs, src_nodata=dataset.nodata,
                  dst_transform=destination["transform"], dst_crs=destination["crs"],
                  dst_nodata=np.nan,
                  # QA classes cannot be interpolated, reflectance can.
                  resampling=destination["qa_resampling"] if name in {"scl", "qa_pixel"} else destination["value_resampling"])
        values[name] = _calibrate(name, raw, metadata)
    return values


def _index_and_qa(values, metadata, product):
    required = INDEX_DEFINITIONS[product]["bands"]
    missing = [name for name in required if name not in values]
    if missing:
        raise ValueError("附件缺少指数所需波段: " + ", ".join(missing))
    valid, source = _qa_valid(values, metadata, values[required[0]].shape)
    valid &= np.logical_and.reduce([np.isfinite(values[name]) & (values[name] >= -1e-6) & (values[name] <= 1 + 1e-6)
                                    for name in required])
    index, valid = compute_index(product, values, valid_mask=valid)
    return index, valid, source


def _candidate_windows(cells, width, height):
    """Merge adjacent candidate cells, including cells from neighbouring blocks."""
    remaining = set(cells)
    groups = []
    while remaining:
        start = remaining.pop(); queue = deque([start]); group = [start]
        while queue:
            x, y = queue.popleft()
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbour in remaining:
                    remaining.remove(neighbour); queue.append(neighbour); group.append(neighbour)
        groups.append(group)
    records = []
    for group in groups:
        xs, ys = zip(*group)
        x, y = min(xs) * CELL_SIZE, min(ys) * CELL_SIZE
        right, bottom = min(width, (max(xs) + 1) * CELL_SIZE), min(height, (max(ys) + 1) * CELL_SIZE)
        changed = sum(cells[cell][0] for cell in group); valid = sum(cells[cell][1] for cell in group)
        records.append(([x, y, right - x, bottom - y], changed, valid))
    return sorted(records, key=lambda item: item[1], reverse=True)


def _accumulate_cells(cells, changed, valid, row0, col0, *, dropped):
    """Accumulate exact intersections with the global candidate grid.

    Blocks may begin at arbitrary offsets, so a block-local ``range(0, 128)``
    is wrong.  The retained grid has a hard cap: huge scenes still have bounded
    Python memory, and the response explicitly reports discarded grid cells.
    """
    height, width = valid.shape
    for grid_y in range(row0 // CELL_SIZE, (row0 + height - 1) // CELL_SIZE + 1):
        y0, y1 = max(0, grid_y * CELL_SIZE - row0), min(height, (grid_y + 1) * CELL_SIZE - row0)
        for grid_x in range(col0 // CELL_SIZE, (col0 + width - 1) // CELL_SIZE + 1):
            x0, x1 = max(0, grid_x * CELL_SIZE - col0), min(width, (grid_x + 1) * CELL_SIZE - col0)
            key = (grid_x, grid_y)
            piece_changed = int(changed[y0:y1, x0:x1].sum())
            if key not in cells and not piece_changed:
                continue
            if key not in cells:
                if len(cells) >= MAX_CANDIDATE_GRID_CELLS:
                    dropped[0] += 1
                    continue
                cells[key] = [0, 0]
            cells[key][0] += piece_changed
            cells[key][1] += int(valid[y0:y1, x0:x1].sum())


def _candidate_preview(before, after, window, destination):
    """Render an actual before/after preview in reference pixel coordinates."""
    import rasterio
    from PIL import Image, ImageDraw
    from rasterio.enums import Resampling
    from rasterio.windows import Window, transform as window_transform
    from . import assets

    x, y, width, height = window
    scale = min(1, 768 / max(width, height))
    out_height, out_width = max(1, round(height * scale)), max(1, round(width * scale))
    with rasterio.open(before.file_path) as first, rasterio.open(after.file_path) as second:
        indexes = list(range(1, min(3, first.count) + 1))
        first_data = first.read(indexes, window=Window(x, y, width, height), out_shape=(len(indexes), out_height, out_width), resampling=Resampling.bilinear, masked=True)
        aligned = np.full((len(indexes), out_height, out_width), np.nan, dtype=np.float32)
        target_transform = window_transform(Window(x, y, width, height), first.transform) * first.transform.scale(width / out_width, height / out_height)
        for output_band, source_band in enumerate(range(1, min(3, second.count) + 1)):
            from rasterio.warp import reproject
            reproject(rasterio.band(second, source_band), aligned[output_band], src_transform=second.transform, src_crs=second.crs,
                      src_nodata=second.nodata, dst_transform=target_transform, dst_crs=first.crs, dst_nodata=np.nan, resampling=Resampling.bilinear)
    left = Image.fromarray(np.moveaxis(assets._rgb(first_data), 0, -1))
    right = Image.fromarray(np.moveaxis(assets._rgb(aligned), 0, -1))
    combined = Image.new("RGB", (left.width + right.width, left.height + 22), "black")
    combined.paste(left, (0, 22)); combined.paste(right, (left.width, 22))
    draw = ImageDraw.Draw(combined); draw.text((5, 4), "Before (reference)", fill="white")
    draw.text((left.width + 5, 4), "After (aligned)", fill="white")
    destination.parent.mkdir(parents=True, exist_ok=True)
    combined.save(destination, "JPEG", quality=88)


def compare(args, ctx, *, artifact_writer, evidence_writer, artifact_file_writer):
    """Create an after-minus-before index GeoTIFF and locatable candidate windows."""
    before = _scoped_attachment(ctx, args["reference_attachment_id"])
    after = _scoped_attachment(ctx, args["comparison_attachment_id"])
    if before.id == after.id:
        raise ValueError("两期变化需要两个不同附件")
    product = args["product"]
    first_date, second_date, collection = _validate_pair(before, after, product)
    threshold = float(args.get("min_delta", 0.15))
    if not 0 < threshold <= 2:
        raise ValueError("min_delta 必须在 0 到 2 之间")
    max_candidates = int(args.get("max_candidates", 24))
    if not 1 <= max_candidates <= 64:
        raise ValueError("max_candidates 必须在 1 到 64 之间")

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import transform as window_transform
    from . import assets

    before_map, after_map = before.metadata.get("band_map") or {}, after.metadata.get("band_map") or {}
    required = set(INDEX_DEFINITIONS[product]["bands"])
    qa_name = "scl" if collection.startswith("sentinel-2") else "qa_pixel"
    wanted = required | {qa_name}
    if not wanted <= set(before_map) or not wanted <= set(after_map):
        raise ValueError("两期影像都必须含同场景 QA 与指数所需波段")
    stats = {"valid": 0, "changed": 0, "sum": 0.0, "min": np.inf, "max": -np.inf, "total": 0}
    cells, dropped_cells = {}, [0]
    scratch = assets._inside(assets._root() / "derived" / f"change-{ctx['run'].id}-{uuid.uuid4().hex}.tif")
    scratch.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(before.file_path) as first, rasterio.open(after.file_path) as second:
        if not first.crs or not second.crs:
            raise ValueError("两期 GeoTIFF 都必须包含 CRS")
        profile = first.profile.copy()
        profile.update(driver="GTiff", count=1, dtype="float32", nodata=np.nan, compress="deflate", BIGTIFF="IF_SAFER")
        if first.width < 16 or first.height < 16:
            profile.pop("tiled", None); profile.pop("blockxsize", None); profile.pop("blockysize", None)
        grid = {"crs": str(first.crs), "transform": list(first.transform)[:6], "width": first.width, "height": first.height}
        with rasterio.open(scratch, "w", **profile) as output:
            for _, window in first.block_windows(1):
                from .runtime import checkpoint
                checkpoint()
                shape = (int(window.height), int(window.width))
                destination = {"shape": shape, "transform": window_transform(window, first.transform),
                               "crs": first.crs, "qa_resampling": Resampling.nearest,
                               "value_resampling": Resampling.bilinear}
                first_values = _band_values(first, before.metadata, {name: before_map[name] for name in wanted}, window)
                second_values = _band_values(second, after.metadata, {name: after_map[name] for name in wanted}, window, destination=destination)
                first_index, first_valid, first_qa = _index_and_qa(first_values, before.metadata, product)
                second_index, second_valid, second_qa = _index_and_qa(second_values, after.metadata, product)
                valid = first_valid & second_valid
                delta = np.where(valid, second_index - first_index, np.nan).astype(np.float32)
                output.write(delta, 1, window=window)
                sample = delta[valid]; stats["total"] += delta.size
                if sample.size:
                    stats["valid"] += int(sample.size); stats["changed"] += int((np.abs(sample) >= threshold).sum())
                    stats["sum"] += float(sample.sum()); stats["min"] = min(stats["min"], float(sample.min())); stats["max"] = max(stats["max"], float(sample.max()))
                _accumulate_cells(cells, valid & (np.abs(delta) >= threshold), valid,
                                  int(window.row_off), int(window.col_off), dropped=dropped_cells)
    if stats["valid"] < 16:
        raise ValueError("两期 QA 后共同有效像元不足")
    qualifying = {cell: value for cell, value in cells.items() if value[0] >= 16}
    all_windows = _candidate_windows(qualifying, grid["width"], grid["height"])
    windows = all_windows[:max_candidates]
    contract = {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id),
                "collection": collection, "product": product, "reference_acquired_at": first_date.isoformat(),
                "comparison_acquired_at": second_date.isoformat(), "grid": grid,
                "qa_masks": [first_qa, second_qa], "alignment": "comparison reprojected block-wise to reference native grid"}
    limitations = ["差异阈值只产生变化候选，不是地物变化真值；需用局部视觉与独立证据复核", "重投影、季节物候、观测角度和残余云阴影会造成伪变化"]
    summary = {"valid_pixel_count": stats["valid"], "valid_pixel_ratio": round(stats["valid"] / stats["total"], 4),
               "mean_delta": round(stats["sum"] / stats["valid"], 4), "min_delta": round(stats["min"], 4), "max_delta": round(stats["max"], 4),
               "changed_pixel_count": stats["changed"], "changed_pixel_ratio": round(stats["changed"] / stats["valid"], 4), "threshold": threshold,
               "reference_grid_pixels_processed": stats["total"], "common_valid_pixels": stats["valid"],
               "common_valid_coverage_ratio": round(stats["valid"] / stats["total"], 4),
               "candidate_window_count": len(windows), "candidate_windows_available": len(all_windows),
               "candidate_windows_truncated": max(0, len(all_windows) - len(windows)),
               "candidate_grid_cell_capacity": MAX_CANDIDATE_GRID_CELLS,
               "candidate_grid_overflow_events": dropped_cells[0]}
    evidence = evidence_writer(ctx["run"], "two_date_index_change", summary, scene_id=str(before.scene_id or ""),
                               aoi=before.bbox, contract=contract, limitations=limitations)
    raster = artifact_file_writer(ctx["run"], "change_raster", f"{product} 两期差异", scratch, "image/tiff",
                                  {"product": product, "contract": contract, "summary": summary}, [evidence.evidence_id])
    report = {"product": product, "summary": summary, "contract": contract, "limitations": limitations,
              "candidate_windows": [{"window": item[0], "changed_pixels": item[1], "valid_pixels": item[2]} for item in windows]}
    report_artifact = artifact_writer(ctx["run"], "change_report", f"{product} 两期变化报告",
                                      json.dumps(report, ensure_ascii=False).encode("utf-8"), "application/json",
                                      {"product": product}, [evidence.evidence_id])
    observations = []
    for number, (window, changed, valid) in enumerate(windows, 1):
        x, y, width, height = window
        preview = assets._inside(assets._root() / "observations" / f"change-{uuid.uuid4().hex}.jpg")
        _candidate_preview(before, after, window, preview)
        observation = SpatialObservation.objects.create(conversation=ctx["conversation"], attachment=before, run=ctx["run"],
            label=f"{product} 变化候选 {number}", kind="change_candidate", window=window,
            geometry={"type": "Polygon", "coordinates": [[[x, y], [x + width, y], [x + width, y + height], [x, y + height], [x, y]]]},
            summary=f"{product} 差异超过 ±{threshold:g} 的候选窗口（{changed}/{valid} 个共同有效像元）；不是地物变化真值。",
            evidence_refs=[evidence.evidence_id], preview_path=str(preview), context_version=ctx.get("version", 1),
            metadata={"call_key": ctx.get("call_key", ""), "product": product, "threshold": threshold,
                      "changed_pixels": changed, "valid_pixels": valid, "reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id),
                      "comparison_alignment": "rendered on reference native grid"})
        observations.append(observation)
    return {"evidence_id": evidence.evidence_id, "artifact_ids": [raster.id, report_artifact.id], "summary": summary,
            "measurement_grade": "screening",
            "observations": [str(item.id) for item in observations], "image_refs": [str(item.id) for item in observations],
            "limitations": limitations}
