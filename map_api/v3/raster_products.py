"""Native-grid COG ingestion and streaming V3 raster products.

This module deliberately keeps source values in the attachment.  Calibration and
QA are recorded from the STAC asset, then applied while each output block is read;
it never turns a scientific product into a display-sized preview array.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from ..remote_sensing_indices import INDEX_DEFINITIONS
from .runtime import checkpoint


OPTICAL = {"red", "green", "blue", "nir", "swir", "swir2"}
ALIASES = {"nir08": "nir", "swir16": "swir", "swir22": "swir2", "data": "dem", "st_b10": "st"}


def logical_band(name):
    return ALIASES.get(name, name)


def _bounds(bbox):
    try:
        values = tuple(float(bbox[key]) for key in ("min_lng", "min_lat", "max_lng", "max_lat"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("AOI bbox 必须含 min_lng/min_lat/max_lng/max_lat") from exc
    if not values[0] < values[2] or not values[1] < values[3]:
        raise ValueError("AOI bbox 范围无效")
    return values


def _asset_band(asset, logical):
    bands = (asset or {}).get("raster:bands") or []
    band = bands[0] if bands and isinstance(bands[0], dict) else {}
    record = {"asset_key": logical, "nodata": band.get("nodata")}
    if logical in OPTICAL or logical == "st":
        if "scale" not in band or "offset" not in band:
            raise ValueError(f"{logical} 资产缺少 STAC raster:bands scale/offset，不能推断反射率校准")
        scale, offset = float(band["scale"]), float(band["offset"])
        if not np.isfinite(scale) or not np.isfinite(offset) or scale <= 0:
            raise ValueError(f"{logical} 资产的 scale/offset 无效")
        record.update(scale=scale, offset=offset, calibrated_unit="kelvin" if logical == "st" else "surface_reflectance")
    return record


def _pixel_size(transform, crs):
    x, y = abs(float(transform.a)), abs(float(transform.e))
    if crs and getattr(crs, "is_geographic", False):
        # Only used for DEM derivatives; calculate at the crop centre in caller.
        return None
    return [x, y]


def _http_href(href):
    # Public STAC DEM assets use s3:// identifiers. Access their anonymous HTTPS
    # representation instead of asking the host AWS credential chain for keys.
    if isinstance(href, str) and href.startswith("s3://"):
        parts = urlsplit(href)
        return f"https://{parts.netloc}.s3.amazonaws.com{parts.path}"
    return href


def _retry_href(href, attempt):
    """Give a retried remote COG request a distinct cache key.

    Some corporate proxies have returned a cached, truncated byte range for an
    otherwise healthy public S3 COG.  The token is deliberately request-only:
    ``source_href`` continues to record the unmodified STAC asset.
    """
    href = _http_href(href)
    if not isinstance(href, str) or not href.lower().startswith(("http://", "https://")):
        return href
    parts = urlsplit(href)
    query = "&".join(value for value in (parts.query, f"cog_retry={attempt}-{uuid.uuid4().hex}") if value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _recoverable_cog_error(exc):
    message = (type(exc).__name__ + " " + str(exc)).lower()
    return any(marker in message for marker in (
        "readencodedtile", "ireadblock", "warpoperationerror", "cpl_vsil_curl",
        "curl error", "http response code", "failed to read", "tiffread",
    ))


def _proxy_env():
    proxy = os.environ.get("V3_COG_PROXY")
    if not proxy:
        return {}
    env = {"GDAL_HTTP_PROXY": proxy}
    userpwd = os.environ.get("V3_COG_PROXYUSERPWD")
    if userpwd:
        env["GDAL_HTTP_PROXYUSERPWD"] = userpwd
    return env


def _read_progress(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_progress(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def ingest_cogs(selected, bbox, target, *, profile, source_metadata=None):
    """Ingest native COGs, retrying corrupt remote range responses safely.

    GDAL's own retry covers transport setup, but a proxy can reply ``206`` with
    a shortened cached body.  Retrying the whole small AOI ingest with a fresh
    URL cache key makes every block be decoded again; it never substitutes a
    thumbnail or synthetic pixel data.
    """
    import rasterio

    target = Path(target).resolve()
    attempts = 0 if os.environ.get("COG_READ_MODE") == "small_ranges" else max(1, min(2, int(os.environ.get("COG_INGEST_ATTEMPTS", "2"))))
    last_error = None
    for attempt in range(attempts):
        request_selected = []
        for name, asset in selected:
            request_asset = dict(asset)
            request_asset["href"] = _retry_href(asset.get("href"), attempt)
            request_selected.append((name, request_asset))
        temporary = target.with_name(f"{target.stem}.ingest-{uuid.uuid4().hex}{target.suffix}")
        try:
            with rasterio.Env(
                GDAL_HTTP_TIMEOUT="30",
                GDAL_HTTP_MAX_RETRY="1",
                GDAL_HTTP_RETRY_DELAY="1",
                GDAL_HTTP_MULTIRANGE="NO",
                GDAL_HTTP_USE_HEAD="NO",
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                VSI_CACHE="FALSE", **_proxy_env(),
            ):
                metadata = _ingest_cogs_once(request_selected, bbox, temporary, profile=profile, source_metadata=source_metadata)
            os.replace(temporary, target)
            return metadata
        except Exception as exc:
            last_error = exc
            if temporary.exists():
                temporary.unlink()
            if not _recoverable_cog_error(exc):
                raise
    # The proxy may truncate large range bodies consistently. Read verified
    # 64 KiB ranges through GDAL's Python VSI without downloading whole scenes.
    temporary = target.with_name(f"{target.stem}.ranges-{uuid.uuid4().hex}{target.suffix}")
    try:
        metadata = _ingest_cogs_once(selected, bbox, temporary, profile=profile,
            source_metadata=source_metadata, small_ranges=True)
        os.replace(temporary, target)
        return metadata
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError("原始 COG 分块读取失败，无法安全计算产品") from exc


def _mosaic_qa_valid(qa, profile):
    """Return the source QA validity mask without treating missing QA as clear."""
    if profile.get("qa_kind") == "bitmask":
        bad = sum(1 << int(bit) for bit in profile.get("qa_bad_bits", [1, 2, 3, 4]))
        return np.isfinite(qa) & ((qa.astype(np.uint16) & bad) == 0)
    return np.isfinite(qa) & np.isin(qa, [4, 5, 6, 7])


def ingest_cog_mosaic(scenes, bbox, target, *, profile, source_metadata=None, progress_key=None):
    """Use the same verified remote transport for both single and multi-scene IO.

    With ``progress_key`` the working file and a JSON sidecar use deterministic
    paths under ``<target dir>/.partial/`` so a ToolInterrupted call keeps every
    completed output window; the next call with the same key resumes there.
    """
    import rasterio
    from .runtime import ToolInterrupted
    target = Path(target).resolve()
    modes = [True] if os.environ.get("COG_READ_MODE") == "small_ranges" else [False, True]
    for small_ranges in modes:
        if progress_key:
            partial = target.parent / ".partial"
            partial.mkdir(parents=True, exist_ok=True)
            temporary = partial / f"{progress_key}.tif"
        else:
            temporary = target.with_name(f"{target.stem}.mosaic-{uuid.uuid4().hex}{target.suffix}")
        sidecar = temporary.with_suffix(".json")
        try:
            with rasterio.Env(GDAL_HTTP_TIMEOUT="30", GDAL_HTTP_MAX_RETRY="1",
                    GDAL_HTTP_MULTIRANGE="NO", GDAL_HTTP_USE_HEAD="NO",
                    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="FALSE", **_proxy_env()):
                metadata = _ingest_cog_mosaic_once(scenes, bbox, temporary, profile=profile,
                    source_metadata=source_metadata, small_ranges=small_ranges, resumable=bool(progress_key))
            os.replace(temporary, target)
            sidecar.unlink(missing_ok=True)
            return metadata
        except ToolInterrupted:
            if not progress_key:
                temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            if small_ranges or not _recoverable_cog_error(exc):
                if isinstance(exc, ValueError):
                    raise
                raise ValueError("原始 COG 分块读取失败，无法安全拼接") from exc


def _ingest_cog_mosaic_once(scenes, bbox, target, *, profile, source_metadata=None, small_ranges=False, resumable=False):
    """Build a same-day, native-grid mosaic from complete source scenes.

    Every scene must expose exactly the same logical bands and calibration.  Source
    choice is made per output block: clear QA pixels win in overlaps, then cloudy
    pixels fill only otherwise unobserved cells so coverage is measured honestly.
    The function rejects an incomplete requested AOI instead of emitting a raster
    that callers could accidentally describe as complete coverage.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from rasterio.transform import array_bounds
    from rasterio.warp import reproject, transform_bounds, transform_geom
    from rasterio.windows import Window, from_bounds, transform as window_transform

    if not scenes:
        raise ValueError("没有可用于拼接的同日场景")
    _bounds(bbox)
    expected_names = [name for name, _ in scenes[0]["assets"]]
    if not expected_names:
        raise ValueError("拼接场景没有可读取的 COG 资产")
    if any([name for name, _ in scene["assets"]] != expected_names for scene in scenes):
        raise ValueError("同日拼接场景的波段集合不一致")
    logical_names = [logical_band(name) for name in expected_names]
    if len(set(logical_names)) != len(logical_names):
        raise ValueError("拼接场景的波段语义重复")
    qa_name = "qa_pixel" if "qa_pixel" in logical_names else "scl" if "scl" in logical_names else None
    if profile.get("qa_band") and qa_name != profile.get("qa_band"):
        raise ValueError("同日拼接缺少来源声明的 QA 波段")

    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        opened = []
        calibration = None
        for scene in scenes:
            checkpoint()
            item_sources = {}
            item_calibration = {}
            for name, asset in scene["assets"]:
                logical = logical_band(name)
                href = _http_href(asset["href"])
                if small_ranges and href.startswith(("http://", "https://")):
                    from .range_reader import HTTPRangeFile
                    filename = "cog-" + uuid.uuid4().hex + ".tif"
                    def opener(path, mode="rb", url=href, expected=filename):
                        if Path(path).name != expected:
                            raise FileNotFoundError(path)
                        return HTTPRangeFile(url)
                    source = stack.enter_context(rasterio.open(filename, opener=opener))
                else:
                    source = stack.enter_context(rasterio.open(href))
                item_sources[logical] = source
                item_calibration[logical] = _asset_band(asset, logical)
            if calibration is None:
                calibration = item_calibration
            elif calibration != item_calibration:
                raise ValueError("同日拼接场景的波段校准或 nodata 声明不一致")
            opened.append(item_sources)
        ref = opened[0][logical_names[0]]
        if not ref.crs:
            raise ValueError("参考 COG 缺少 CRS，无法进行原生网格裁剪")
        west, south, east, north = transform_bounds("EPSG:4326", ref.crs, *_bounds(bbox), densify_pts=21)
        # Unlike a single-scene crop, the mosaic grid deliberately may extend past
        # the first tile. Other same-day tiles are expected to fill those cells.
        crop = from_bounds(west, south, east, north, ref.transform).round_offsets().round_lengths()
        if crop.width < 1 or crop.height < 1:
            raise ValueError("AOI 与参考 COG 没有有效重叠")
        width, height = int(crop.width), int(crop.height)
        crop_transform = window_transform(crop, ref.transform)
        tiled = width >= 16 and height >= 16
        output_profile = {"driver": "GTiff", "width": width, "height": height, "count": len(logical_names),
                          "dtype": "float32", "crs": ref.crs, "transform": crop_transform, "nodata": np.nan,
                          "compress": "deflate", "BIGTIFF": "IF_SAFER"}
        if tiled:
            output_profile.update(tiled=True, blockxsize=min(512, (width // 16) * 16), blockysize=min(512, (height // 16) * 16))
        aoi_geometry = transform_geom("EPSG:4326", ref.crs, {"type": "Polygon", "coordinates": [[
            [bbox["min_lng"], bbox["min_lat"]], [bbox["max_lng"], bbox["min_lat"]],
            [bbox["max_lng"], bbox["max_lat"]], [bbox["min_lng"], bbox["max_lat"]], [bbox["min_lng"], bbox["min_lat"]],
        ]]}, precision=15)
        signature = {"small_ranges": bool(small_ranges), "scenes": [
            [[name, asset.get("source_href", asset.get("href"))] for name, asset in scene["assets"]] for scene in scenes]}
        sidecar = target.with_suffix(".json") if resumable else None
        resume = None
        if resumable and sidecar.exists() and target.exists():
            state = _read_progress(sidecar)
            if state and state.get("signature") == signature:
                try:
                    with rasterio.open(target) as existing:
                        if (existing.width == width and existing.height == height
                                and existing.count == len(logical_names) and existing.crs == ref.crs):
                            resume = state
                except Exception:
                    resume = None
        if resumable and resume is None:
            sidecar.unlink(missing_ok=True)
        done_windows = {tuple(item) for item in (resume or {}).get("done_windows", [])}
        observed_total = int((resume or {}).get("observed_total", 0))
        valid_total = int((resume or {}).get("valid_total", 0))
        requested_total = int((resume or {}).get("requested_total", 0))
        mapping = {name: index for index, name in enumerate(logical_names, 1)}
        opened_target = rasterio.open(target, "r+") if resume else rasterio.open(target, "w", **output_profile)
        with opened_target as dst:
            windows = list(dst.block_windows(1))
            total_windows = len(windows)
            for _, win in windows:
                checkpoint()
                window_key = (int(win.col_off), int(win.row_off))
                if window_key in done_windows:
                    continue
                win_transform = window_transform(win, crop_transform)
                requested = geometry_mask([aoi_geometry], out_shape=(int(win.height), int(win.width)), transform=win_transform, invert=True)
                result = {name: np.full(requested.shape, np.nan, dtype=np.float32) for name in logical_names}
                chosen = np.zeros(requested.shape, dtype=bool)
                # Two passes keep valid observations preferred at overlaps without
                # turning cloud/shadow into an unobserved hole for coverage accounting.
                for require_clear in (True, False):
                    for item_sources in opened:
                        values = {}
                        for name in logical_names:
                            src = item_sources[name]
                            value = np.full(requested.shape, np.nan, dtype=np.float32)
                            method = Resampling.nearest if name in {"scl", "qa_pixel"} else Resampling.bilinear
                            reproject(rasterio.band(src, 1), value, src_transform=src.transform, src_crs=src.crs,
                                      src_nodata=src.nodata, dst_transform=win_transform, dst_crs=ref.crs,
                                      dst_nodata=np.nan, resampling=method)
                            values[name] = value
                        observed = np.logical_and.reduce([np.isfinite(values[name]) for name in logical_names])
                        clear = _mosaic_qa_valid(values[qa_name], profile) if qa_name else observed
                        eligible = (clear & observed) if require_clear else observed
                        take = requested & ~chosen & eligible
                        if take.any():
                            for name in logical_names:
                                result[name][take] = values[name][take]
                            chosen |= take
                observed = chosen & requested
                observed_total += int(observed.sum()); requested_total += int(requested.sum())
                if qa_name:
                    valid_total += int((observed & _mosaic_qa_valid(result[qa_name], profile)).sum())
                else:
                    valid_total += int(observed.sum())
                for name, index in mapping.items():
                    dst.write(result[name], index, window=win)
                if sidecar is not None:
                    done_windows.add(window_key)
                    _write_progress(sidecar, {"signature": signature, "done_windows": sorted(done_windows),
                        "total_windows": total_windows, "observed_total": observed_total,
                        "requested_total": requested_total, "valid_total": valid_total})
        coverage_ratio = observed_total / requested_total if requested_total else 0.0
        if not requested_total or coverage_ratio < 0.999:
            target.unlink(missing_ok=True)
            raise ValueError(f"同日场景未完整覆盖 AOI（真实源覆盖 {coverage_ratio:.2%}）")
        if resume:
            # A resumed GTiff has a different physical block layout even though
            # every pixel matches. Rewrite locally in the one-shot write order so
            # the delivered file is byte-identical to an uninterrupted ingest.
            rewritten = target.with_name(f"{target.stem}.rewrite-{uuid.uuid4().hex}{target.suffix}")
            try:
                with rasterio.open(target) as src, rasterio.open(rewritten, "w", **output_profile) as out:
                    for _, win in out.block_windows(1):
                        checkpoint()
                        for index in range(1, len(logical_names) + 1):
                            out.write(src.read(index, window=win), index, window=win)
                os.replace(rewritten, target)
            except Exception:
                rewritten.unlink(missing_ok=True)
                raise
        actual = array_bounds(height, width, crop_transform)
        metadata = {"band_map": mapping, "band_calibration": calibration,
                    "native_grid": {"crs": str(ref.crs), "transform": list(crop_transform)[:6], "width": width, "height": height,
                                    "reference_asset": logical_names[0], "pixel_size_native": _pixel_size(crop_transform, ref.crs),
                                    "aoi_bounds_in_grid_crs": [float(v) for v in actual]},
                    "source_assets": {name: [{"href": scene["assets"][index][1].get("source_href", scene["assets"][index][1].get("href"))}
                                             for scene in scenes] for index, name in enumerate(logical_names)},
                    "mosaic_coverage": {"coverage_complete": observed_total == requested_total,
                                        "requested_pixel_count": requested_total,
                                        "observed_pixel_count": observed_total,
                                        "source_coverage_ratio": round(coverage_ratio, 6),
                                        "coverage_note": None if observed_total == requested_total else
                                            "AOI 边界少量像元超出场景足迹（缺口 <0.1%），未成片缺失；统计按真实覆盖像元计算",
                                        "qa_valid_pixel_count": valid_total, "qa_valid_coverage_ratio": round(valid_total / requested_total, 4)}}
        if source_metadata:
            metadata.update(source_metadata)
        return metadata


def _ingest_cogs_once(selected, bbox, target, *, profile, source_metadata=None, small_ranges=False):
    """Crop STAC COG assets to the first required asset's native grid.

    ``selected`` is ``[(asset_key, signed_asset)]``.  Non-reference assets are
    reprojected one destination block at a time.  The target CRS and resolution are
    therefore the source reference COG's, never EPSG:4326/512-preview defaults.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds, reproject
    from rasterio.windows import Window, from_bounds, transform as window_transform

    if not selected:
        raise ValueError("场景没有可读取的 COG 资产")
    _bounds(bbox)
    target.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        sources = []
        for name, asset in selected:
            href = _http_href(asset["href"])
            if small_ranges and href.startswith(("https://", "http://")):
                from .range_reader import HTTPRangeFile
                filename = "cog-" + uuid.uuid4().hex + ".tif"
                def opener(path, mode="rb", url=href, expected=filename):
                    if Path(path).name != expected:
                        raise FileNotFoundError(path)
                    return HTTPRangeFile(url)
                source = stack.enter_context(rasterio.open(filename, opener=opener))
            else:
                source = stack.enter_context(rasterio.open(href))
            sources.append((name, asset, source))
        ref_name, _, ref = sources[0]
        if not ref.crs:
            raise ValueError(f"{ref_name} COG 缺少 CRS，无法进行原生网格裁剪")
        west, south, east, north = transform_bounds("EPSG:4326", ref.crs, *_bounds(bbox), densify_pts=21)
        requested = from_bounds(west, south, east, north, ref.transform)
        full = Window(0, 0, ref.width, ref.height)
        crop = requested.round_offsets().round_lengths().intersection(full)
        if crop.width < 1 or crop.height < 1:
            raise ValueError("AOI 与参考 COG 没有有效重叠")
        crop_transform = window_transform(crop, ref.transform)
        block = 512
        width, height = int(crop.width), int(crop.height)
        # GTiff block dimensions must be multiples of 16. Small AOIs cannot tile.
        tiled = width >= 16 and height >= 16
        out_profile = {"driver": "GTiff", "width": width, "height": height, "count": len(sources),
                       "dtype": "float32", "crs": ref.crs, "transform": crop_transform,
                       "nodata": np.nan, "compress": "deflate", "BIGTIFF": "IF_SAFER"}
        if tiled:
            out_profile.update(tiled=True, blockxsize=min(block, (width // 16) * 16), blockysize=min(block, (height // 16) * 16))
        mapping, calibration = {}, {}
        with rasterio.open(target, "w", **out_profile) as dst:
            for index, (name, asset, src) in enumerate(sources, 1):
                logical = logical_band(name)
                if logical in mapping:
                    raise ValueError(f"COG 波段语义重复: {logical}")
                mapping[logical] = index
                calibration[logical] = _asset_band(asset, logical)
                # QA and categorical values must retain labels; continuous products use bilinear alignment.
                method = Resampling.nearest if logical in {"scl", "qa_pixel"} else Resampling.bilinear
                for _, win in dst.block_windows(index):
                    checkpoint()
                    destination = np.full((int(win.height), int(win.width)), np.nan, dtype=np.float32)
                    reproject(rasterio.band(src, 1), destination, src_transform=src.transform, src_crs=src.crs,
                              src_nodata=src.nodata, dst_transform=window_transform(win, crop_transform), dst_crs=ref.crs,
                              dst_nodata=np.nan, resampling=method)
                    dst.write(destination, index, window=win)
        actual = array_bounds(height, width, crop_transform)
        metadata = {
            "band_map": mapping, "band_calibration": calibration, "native_grid": {
                "crs": str(ref.crs), "transform": list(crop_transform)[:6], "width": width, "height": height,
                "reference_asset": ref_name, "pixel_size_native": _pixel_size(crop_transform, ref.crs),
                "aoi_bounds_in_grid_crs": [float(v) for v in actual],
            },
            "source_assets": {logical_band(name): {"href": asset.get("source_href", asset.get("href")), "raster_bands": asset.get("raster:bands") or []}
                              for name, asset, _ in sources},
        }
        if source_metadata:
            metadata.update(source_metadata)
        return metadata


def _qa_valid(values, metadata, shape):
    collection = metadata.get("collection")
    if collection in {"sentinel-2-l2a", "sentinel-2-c1-l2a"}:
        qa = values.get("scl")
        if qa is None:
            raise ValueError("光谱指数必须有 Sentinel-2 SCL QA 波段")
        return np.isfinite(qa) & np.isin(qa, [4, 5, 6, 7]), "sentinel-2-scl"
    if collection == "landsat-c2-l2":
        qa = values.get("qa_pixel")
        if qa is None:
            raise ValueError("光谱指数必须有同场景 Landsat QA_PIXEL")
        bad = sum(1 << int(bit) for bit in metadata.get("qa_bad_bits", [1, 2, 3, 4]))
        return np.isfinite(qa) & ((qa.astype(np.uint16) & bad) == 0), "landsat-qa_pixel"
    raise ValueError("光谱指数仅接受带受信 QA 元数据的 Sentinel-2 L2A 或 Landsat C2 L2 附件")


def _calibrate(name, data, metadata):
    calibration = (metadata.get("band_calibration") or {}).get(name) or {}
    nodata = calibration.get("nodata")
    value = data.astype(np.float32, copy=False)
    if nodata is not None:
        value = np.where(value == float(nodata), np.nan, value)
    if name in OPTICAL:
        if "scale" not in calibration or "offset" not in calibration:
            raise ValueError(f"附件未记录 {name} 的 source calibration")
        value = value * float(calibration["scale"]) + float(calibration["offset"])
    return value


def _online(values, valid, state):
    sample = values[valid]
    if not sample.size:
        return
    state["count"] += int(sample.size)
    state["sum"] += float(sample.sum())
    state["min"] = min(state["min"], float(sample.min()))
    state["max"] = max(state["max"], float(sample.max()))
    state["above"] += int((sample > state["threshold"]).sum())


def _windows(ds):
    for item in ds.block_windows(1):
        checkpoint()
        yield item


def compute_attachment(path, metadata, product, *, bbox=None):
    """Compute summaries over every GeoTIFF block without full-scene allocation."""
    import rasterio
    from rasterio.transform import xy

    mapped = metadata.get("band_map") or {}
    with rasterio.open(path) as ds:
        if any(not isinstance(index, int) or not 1 <= index <= ds.count for index in mapped.values()):
            raise ValueError("附件的 band_map 与实际栅格波段不匹配")
        if product in INDEX_DEFINITIONS:
            required = INDEX_DEFINITIONS[product]["bands"]
            missing = [name for name in required if name not in mapped]
            if missing:
                raise ValueError("附件缺少指数所需波段: " + ", ".join(missing))
            threshold = 0.3 if product == "ndvi" else -0.1 if product == "nbr" else 0.1
            state = {"count": 0, "sum": 0., "min": math.inf, "max": -math.inf, "above": 0, "threshold": threshold, "total": ds.width * ds.height}
            mask_source = None
            for _, win in _windows(ds):
                values = {name: _calibrate(name, ds.read(mapped[name], window=win), metadata) for name in set(required) | ({"scl"} if "scl" in mapped else {"qa_pixel"} if "qa_pixel" in mapped else set())}
                valid, mask_source = _qa_valid(values, metadata, (int(win.height), int(win.width)))
                valid &= np.logical_and.reduce([np.isfinite(values[name]) & (values[name] >= -1e-6) & (values[name] <= 1 + 1e-6) for name in required])
                a = values[required[0]]; b = values[required[1]]
                if product == "ndvi":
                    a, b = values["nir"], values["red"]
                if product == "bsi":
                    a, b = values["swir"] + values["red"], values["nir"] + values["blue"]
                denominator = a + b
                valid &= np.abs(denominator) > 1e-6
                index = np.where(valid, (a - b) / denominator, np.nan)
                _online(index, valid, state)
            if state["count"] < 16:
                raise ValueError("QA 后共同有效像元不足")
            return {"available": True, "product": product, "method": INDEX_DEFINITIONS[product]["formula"], "threshold": threshold,
                    "summary": {"valid_pixel_count": state["count"], "valid_pixel_ratio": round(state["count"] / state["total"], 4), "mean": round(state["sum"] / state["count"], 4), "min": round(state["min"], 4), "max": round(state["max"], 4), "above_threshold_ratio": round(state["above"] / state["count"], 4)},
                    "mask_source": mask_source, "measurement_grade": "screening", "limitations": INDEX_DEFINITIONS[product].get("limitations", []) + ["阈值分类不是地物真值"]}
        if product == "landsat_surface_temperature":
            if metadata.get("collection") != "landsat-c2-l2" or metadata.get("asset_name", "ST_B10").upper() != "ST_B10":
                raise ValueError("地表温度只接受 Landsat Collection 2 ST_B10 产品")
            if "st" not in mapped or "qa_pixel" not in mapped:
                raise ValueError("Landsat ST 必须有同场景 ST_B10 和 QA_PIXEL")
            calibration = (metadata.get("band_calibration") or {}).get("st") or {}
            if "scale" not in calibration or "offset" not in calibration:
                raise ValueError("Landsat ST 资产缺少明确 scale/offset，不能推断温度")
            state = {"count": 0, "sum": 0., "min": math.inf, "max": -math.inf, "total": ds.width * ds.height}
            bad = sum(1 << int(bit) for bit in metadata.get("qa_bad_bits", [1, 2, 3, 4]))
            for _, win in _windows(ds):
                st = ds.read(mapped["st"], window=win).astype(np.float32); qa = ds.read(mapped["qa_pixel"], window=win)
                valid = np.isfinite(st) & (st > 0) & np.isfinite(qa) & ((qa.astype(np.uint16) & bad) == 0)
                kelvin = st * float(calibration["scale"]) + float(calibration["offset"])
                sample = (kelvin - 273.15)[valid]
                if sample.size:
                    state["count"] += int(sample.size); state["sum"] += float(sample.sum()); state["min"] = min(state["min"], float(sample.min())); state["max"] = max(state["max"], float(sample.max()))
            if state["count"] < 16: raise ValueError("Landsat ST QA 后共同有效像元不足")
            return {"available": True, "product": product, "scale": calibration["scale"], "offset": calibration["offset"], "summary": {"valid_pixel_count": state["count"], "mean_celsius": round(state["sum"] / state["count"], 2), "min_celsius": round(state["min"], 2), "max_celsius": round(state["max"], 2)}, "measurement_grade": "screening", "limitations": ["单次过境温度受时间、大气和地表发射率影响，不能单景判定热岛"]}
        if product == "sar_backscatter":
            if "vv" not in mapped: raise ValueError("SAR 产品至少需要 VV")
            calibration = metadata.get("calibration")
            if calibration not in {"linear_sigma0", "linear_gamma0", "db"}: raise ValueError("SAR 必须由来源元数据声明 calibration")
            state = {"count": 0, "sum": 0., "min": math.inf, "max": -math.inf, "water": 0, "total": ds.width * ds.height}
            for _, win in _windows(ds):
                vv = ds.read(mapped["vv"], window=win).astype(np.float32)
                valid = np.isfinite(vv) & ((vv > 0) if calibration != "db" else np.ones(vv.shape, bool))
                db = vv if calibration == "db" else np.where(valid, 10 * np.log10(vv), np.nan)
                sample = db[valid]
                if sample.size:
                    state["count"] += int(sample.size); state["sum"] += float(sample.sum())
                    state["min"] = min(state["min"], float(sample.min())); state["max"] = max(state["max"], float(sample.max()))
                    state["water"] += int((sample <= -18.0).sum())
            if state["count"] < 16: raise ValueError("SAR 有效像元不足")
            return {"available": True, "product": product, "calibration": calibration, "summary": {"valid_pixel_count": state["count"], "mean_vv_db": round(state["sum"] / state["count"], 3), "water_candidate_ratio": round(state["water"] / state["count"], 4)}, "measurement_grade": "screening", "limitations": ["暗散射还可能是平滑地表或几何阴影，必须用光学或历史水体复核", "未做地形校正与斑点滤波时仅供筛查"]}
        if product == "dem_terrain":
            if "dem" not in mapped and "elevation" not in mapped: raise ValueError("DEM 产品缺少高程波段")
            name = "dem" if "dem" in mapped else "elevation"; state = {"count": 0, "sum": 0., "min": math.inf, "max": -math.inf, "total": ds.width * ds.height}
            # Derivative requires neighbouring pixels.  Windows are expanded by one pixel, while only the centre is counted.
            dx, dy = abs(ds.transform.a), abs(ds.transform.e)
            if ds.crs and ds.crs.is_geographic:
                _, lat = xy(ds.transform, ds.height / 2, ds.width / 2); dx *= 111320 * math.cos(math.radians(lat)); dy *= 110574
            if dx <= 0 or dy <= 0: raise ValueError("DEM 网格缺少可用像元间距")
            slope_sum = 0.; slope_count = 0; max_slope = -math.inf
            for _, win in _windows(ds):
                halo = win.round_offsets().round_lengths(); row0=max(0, int(halo.row_off)-1); col0=max(0, int(halo.col_off)-1); row1=min(ds.height, int(halo.row_off+halo.height)+1); col1=min(ds.width, int(halo.col_off+halo.width)+1)
                data = ds.read(mapped[name], window=((row0,row1),(col0,col1))).astype(np.float32); valid=np.isfinite(data)
                centre = data[int(halo.row_off)-row0:int(halo.row_off+halo.height)-row0, int(halo.col_off)-col0:int(halo.col_off+halo.width)-col0]; cv=np.isfinite(centre)
                sample=centre[cv]
                if sample.size: state["count"]+=int(sample.size); state["sum"]+=float(sample.sum()); state["min"]=min(state["min"],float(sample.min())); state["max"]=max(state["max"],float(sample.max()))
                if data.shape[0] >= 3 and data.shape[1] >= 3:
                    gy,gx=np.gradient(np.where(valid,data,np.nan),dy,dx); slope=np.degrees(np.arctan(np.hypot(gx,gy)))[int(halo.row_off)-row0:int(halo.row_off+halo.height)-row0,int(halo.col_off)-col0:int(halo.col_off+halo.width)-col0]; ss=slope[np.isfinite(slope)]
                    if ss.size: slope_sum+=float(ss.sum()); slope_count+=int(ss.size); max_slope=max(max_slope,float(ss.max()))
            if state["count"] < 16: raise ValueError("DEM 有效像元不足")
            return {"available": True, "product": product, "pixel_size_m": [dx,dy], "summary": {"valid_pixel_count":state["count"],"mean_elevation_m":round(state["sum"]/state["count"],2),"mean_slope_deg":round(slope_sum/max(1,slope_count),2),"max_slope_deg":round(max_slope,2)}, "measurement_grade":"screening", "limitations":["坡度由 DEM 栅格导出，边缘和填洼区不稳定", "DEM 是静态地形背景，不能证明当前灾害或地表状态"]}
    raise ValueError("未知栅格产品")
