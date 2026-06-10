import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import math
import time
import threading
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from io import BytesIO

CELL_MAX = 1280
MAX_TOTAL = 4096
BLANK_PIXEL_THRESHOLD = 10
MIN_VALID_PIXEL_RATIO = 0.02
logger = logging.getLogger(__name__)

_download_progress = {}
MAX_PROGRESS_ENTRIES = 50

def get_download_progress(file_name):
    return _download_progress.get(file_name)

def prune_progress():
    """限制进度字典大小,丢弃最早的已完成/出错条目,避免长期运行内存无限增长。"""
    if len(_download_progress) <= MAX_PROGRESS_ENTRIES:
        return
    for k in list(_download_progress.keys()):
        if len(_download_progress) <= MAX_PROGRESS_ENTRIES:
            break
        if _download_progress[k].get("status") in ("done", "error"):
            _download_progress.pop(k, None)

def haversine_distance(lon1, lat1, lon2, lat2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _visual_valid_ratio(img, threshold=BLANK_PIXEL_THRESHOLD):
    sample = img.convert("RGB")
    sample.thumbnail((96, 96))
    total = sample.width * sample.height
    if total <= 0:
        return 0
    valid = sum(1 for r, g, b in sample.getdata() if max(r, g, b) > threshold)
    return valid / total


def _ensure_not_blank(img, label="image"):
    ratio = _visual_valid_ratio(img)
    if ratio < MIN_VALID_PIXEL_RATIO:
        raise Exception(f"{label} appears blank: valid ratio {ratio:.3f}")


def _save_jpeg_atomic(img, full_save_path, quality):
    tmp_path = f"{full_save_path}.tmp_{uuid.uuid4().hex}.jpg"
    try:
        img.save(tmp_path, "JPEG", quality=quality)
        with Image.open(tmp_path) as check:
            check.verify()
        os.replace(tmp_path, full_save_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _fetch_tile(url, proxies, retries=5):
    last_err = None
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(2.0 * attempt)
        try:
            session = requests.Session()
            retry_strategy = Retry(total=1, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("https://", adapter)
            resp = session.get(url, timeout=60, proxies=proxies)
            session.close()
            if resp.status_code == 200:
                img = Image.open(BytesIO(resp.content))
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                _ensure_not_blank(img, "Mapbox tile")
                return img
            raise Exception(f"tile fetch failed: {resp.status_code}")
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            last_err = str(e)[:120]
            continue
        except Exception as e:
            last_err = str(e)[:120]
            continue
    raise Exception(f"tile fetch failed after {retries} attempts: {last_err}")

def _notify(progress_callback, file_name, info):
    if progress_callback:
        progress_callback(file_name, info.copy())


def _set_progress(file_name, info, progress_callback=None):
    _download_progress[file_name] = info
    _notify(progress_callback, file_name, info)


def _update_progress(file_name, updates, progress_callback=None):
    info = _download_progress.setdefault(file_name, {"total": 1, "done": 0, "status": "downloading"})
    info.update(updates)
    _notify(progress_callback, file_name, info)
    return info


def _mapbox_token():
    return os.environ.get('MAPBOX_TOKEN', '')


def fetch_satellite_image(min_lon, min_lat, max_lon, max_lat, save_dir, file_name="satellite_result.jpg",
                          target_resolution=1024, ultra_hd=False, progress_callback=None):
    global _download_progress
    token = _mapbox_token()
    target_resolution = min(MAX_TOTAL, max(1, target_resolution))

    lon_diff = max_lon - min_lon
    lat_diff = max_lat - min_lat
    center_lat_rad = math.radians((min_lat + max_lat) / 2.0)
    aspect_ratio = (lon_diff * math.cos(center_lat_rad)) / lat_diff if lat_diff else 1.0

    if aspect_ratio >= 1:
        total_w = target_resolution
        total_h = max(1, int(target_resolution / aspect_ratio))
    else:
        total_h = target_resolution
        total_w = max(1, int(target_resolution * aspect_ratio))

    os.makedirs(save_dir, exist_ok=True)
    full_save_path = os.path.join(save_dir, file_name)

    proxies = {"http": None, "https": None}
    retina = "@2x" if ultra_hd else ""

    # Single tile if small enough
    if total_w <= CELL_MAX and total_h <= CELL_MAX:
        actual_w = total_w * 2 if ultra_hd else total_w
        actual_h = total_h * 2 if ultra_hd else total_h
        _set_progress(file_name, {"total": 1, "done": 0, "failed": 0, "status": "downloading"}, progress_callback)
        url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/[{min_lon},{min_lat},{max_lon},{max_lat}]/{actual_w}x{actual_h}{retina}?access_token={token}"
        try:
            img = _fetch_tile(url, proxies)
            _ensure_not_blank(img, "Mapbox image")
            _save_jpeg_atomic(img, full_save_path, quality=95)
            _set_progress(file_name, {"total": 1, "done": 1, "failed": 0, "status": "done"}, progress_callback)
            logger.info("Mapbox image saved: %s", full_save_path)
            return full_save_path
        except Exception as e:
            _update_progress(file_name, {"status": "error", "error": str(e)[:200]}, progress_callback)
            logger.warning("Mapbox image fetch failed: %s", e)
            return None

    # Large scene: split into grid and stitch
    cols = math.ceil(total_w / CELL_MAX)
    rows = math.ceil(total_h / CELL_MAX)
    cell_w = math.ceil(total_w / cols)
    cell_h = math.ceil(total_h / rows)

    total_tiles = cols * rows
    _set_progress(file_name, {"total": total_tiles, "done": 0, "failed": 0, "status": "downloading"}, progress_callback)
    progress_lock = threading.Lock()

    canvas = Image.new('RGB', (total_w, total_h))
    failed_tiles = 0

    def _fetch_one(r, c):
        """抓单个格子,返回 (粘贴 x, 粘贴 y, tile 图)。失败用深灰占位,不让整图崩。"""
        nonlocal failed_tiles
        c_min_lon = min_lon + c * (lon_diff / cols)
        c_max_lon = min_lon + (c + 1) * (lon_diff / cols)
        c_max_lat = max_lat - r * (lat_diff / rows)
        c_min_lat = max_lat - (r + 1) * (lat_diff / rows)

        cw = cell_w if c < cols - 1 else total_w - c * cell_w
        ch = cell_h if r < rows - 1 else total_h - r * cell_h
        cw = max(1, cw); ch = max(1, ch)

        url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/[{c_min_lon},{c_min_lat},{c_max_lon},{c_max_lat}]/{cw}x{ch}{retina}?access_token={token}"
        failed = False
        try:
            tile = _fetch_tile(url, proxies)
            logger.info("Mapbox tile (%s/%s,%s/%s) OK", r + 1, rows, c + 1, cols)
        except Exception as e:
            logger.warning("Mapbox tile (%s/%s,%s/%s) failed: %s", r + 1, rows, c + 1, cols, e)
            failed = True
            tile = Image.new('RGB', (cw, ch), (40, 40, 40))
        with progress_lock:
            if failed:
                failed_tiles += 1
            info = _download_progress[file_name]
            _update_progress(
                file_name,
                {"done": info.get("done", 0) + 1, "failed": failed_tiles},
                progress_callback,
            )
        return (c * cell_w, r * cell_h, tile)

    # 4 并发抓格子(_fetch_tile 自带重试/退避),粘贴在主线程顺序进行,避免 PIL 画布竞态
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch_one, r, c) for r in range(rows) for c in range(cols)]
        for fut in as_completed(futures):
            px, py, tile = fut.result()
            canvas.paste(tile, (px, py))

    if failed_tiles == total_tiles:
        _update_progress(file_name, {"status": "error", "failed": failed_tiles}, progress_callback)
        logger.warning("Mapbox all tiles failed for %s", file_name)
        return None

    try:
        _ensure_not_blank(canvas, "Mapbox stitched image")
        _save_jpeg_atomic(canvas, full_save_path, quality=92)
    except Exception as e:
        _update_progress(file_name, {"status": "error", "failed": failed_tiles, "error": str(e)[:200]}, progress_callback)
        logger.warning("Mapbox stitched image validation failed for %s: %s", file_name, e)
        return None

    _update_progress(file_name, {"status": "partial" if failed_tiles else "done", "failed": failed_tiles}, progress_callback)
    logger.info("Mapbox stitched image saved: %sx%s -> %s", total_w, total_h, full_save_path)
    return full_save_path
