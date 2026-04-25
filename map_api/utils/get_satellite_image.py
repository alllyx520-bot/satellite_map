import requests
import os
import math
from PIL import Image
from io import BytesIO

MAPBOX_TOKEN = os.environ.get('MAPBOX_TOKEN', '')
CELL_MAX = 1280
MAX_TOTAL = 4096

def haversine_distance(lon1, lat1, lon2, lat2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def _fetch_tile(url, proxies):
    resp = requests.get(url, timeout=30, proxies=proxies)
    if resp.status_code == 200:
        img = Image.open(BytesIO(resp.content))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        return img
    raise Exception(f"tile fetch failed: {resp.status_code}")

def fetch_satellite_image(min_lon, min_lat, max_lon, max_lat, save_dir, file_name="satellite_result.jpg",
                          target_resolution=1024, ultra_hd=False):
    target_resolution = min(MAX_TOTAL, max(1, target_resolution))

    lon_diff = max_lon - min_lon
    lat_diff = max_lat - min_lat
    center_lat_rad = math.radians((min_lat + max_lat) / 2.0)
    aspect_ratio = (lon_diff * math.cos(center_lat_rad)) / lat_diff

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
        url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/[{min_lon},{min_lat},{max_lon},{max_lat}]/{actual_w}x{actual_h}{retina}?access_token={MAPBOX_TOKEN}"
        try:
            img = _fetch_tile(url, proxies)
            img.save(full_save_path, 'JPEG', quality=95)
            print(f"[Mapbox] ✅ {full_save_path}")
            return full_save_path
        except Exception as e:
            print(f"[Mapbox] ❌ {e}")
            return None

    # Large scene: split into grid and stitch
    cols = math.ceil(total_w / CELL_MAX)
    rows = math.ceil(total_h / CELL_MAX)
    cell_w = math.ceil(total_w / cols)
    cell_h = math.ceil(total_h / rows)

    canvas = Image.new('RGB', (total_w, total_h))

    for r in range(rows):
        for c in range(cols):
            c_min_lon = min_lon + c * (lon_diff / cols)
            c_max_lon = min_lon + (c + 1) * (lon_diff / cols)
            c_max_lat = max_lat - r * (lat_diff / rows)
            c_min_lat = max_lat - (r + 1) * (lat_diff / rows)

            cw = cell_w if c < cols - 1 else total_w - c * cell_w
            ch = cell_h if r < rows - 1 else total_h - r * cell_h
            cw = max(1, cw); ch = max(1, ch)

            url = f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/[{c_min_lon},{c_min_lat},{c_max_lon},{c_max_lat}]/{cw}x{ch}{retina}?access_token={MAPBOX_TOKEN}"
            try:
                tile = _fetch_tile(url, proxies)
                canvas.paste(tile, (c * cell_w, r * cell_h))
                print(f"[Mapbox] tile ({r+1}/{rows},{c+1}/{cols}) OK")
            except Exception as e:
                print(f"[Mapbox] tile ({r+1}/{rows},{c+1}/{cols}) ❌ {e}")
                # Fill failed tile with dark gray
                fill = Image.new('RGB', (cw, ch), (40, 40, 40))
                canvas.paste(fill, (c * cell_w, r * cell_h))

    canvas.save(full_save_path, 'JPEG', quality=92)
    print(f"[Mapbox] ✅ stitched {total_w}x{total_h} → {full_save_path}")
    return full_save_path
