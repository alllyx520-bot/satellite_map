import os
import math
from PIL import Image

MAX_DIM = 2560
TILE_THRESHOLD = 4096
TILE_SIZE = 1536
TILE_OVERLAP = 192
OVERVIEW_DIM = 1536
MAX_TILES = 9

MAX_DIM_MAP = {
    'qwen3.6-plus': 2560,
    'qwen3.5-plus': 2560,
    'qwen3.5-flash': 2560,
    'qwen3-vl-plus': 3584,
    'qwen3-vl-flash': 2560,
    'qwen-vl-max': 3584,
}


def smart_prepare_image(fp, max_dim=None):
    if not os.path.exists(fp):
        return None

    effective_max = max_dim or MAX_DIM

    img = Image.open(fp)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    w, h = img.size
    orig_w, orig_h = w, h
    base, ext = os.path.splitext(fp)

    if max(w, h) <= effective_max:
        return {"single": fp, "orig_w": w, "orig_h": h, "eff_w": w, "eff_h": h}

    if max(w, h) <= TILE_THRESHOLD:
        ratio = effective_max / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        scaled_path = f"{base}_hd.jpg"
        resized.save(scaled_path, 'JPEG', quality=92)
        return {"single": scaled_path, "orig_w": w, "orig_h": h, "eff_w": new_w, "eff_h": new_h}

    # Tiling for very large images
    stride = TILE_SIZE - TILE_OVERLAP
    cols = max(1, math.ceil((w - TILE_OVERLAP) / stride))
    rows = max(1, math.ceil((h - TILE_OVERLAP) / stride))

    if cols * rows > MAX_TILES:
        ratio = TILE_THRESHOLD / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        w_n, h_n = img.size
        w, h = w_n, h_n
        cols = max(1, math.ceil((w - TILE_OVERLAP) / stride))
        rows = max(1, math.ceil((h - TILE_OVERLAP) / stride))
    if cols < 1: cols = 1
    if rows < 1: rows = 1

    results = []

    overview = img.copy()
    if max(w, h) > OVERVIEW_DIM:
        ratio = OVERVIEW_DIM / max(w, h)
        overview = overview.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    overview_path = f"{base}_overview.jpg"
    overview.save(overview_path, 'JPEG', quality=85)
    results.append(overview_path)

    for r in range(rows):
        for c in range(cols):
            x1 = c * stride
            y1 = r * stride
            x2 = min(x1 + TILE_SIZE, w)
            y2 = min(y1 + TILE_SIZE, h)
            x1 = max(0, x2 - TILE_SIZE)
            y1 = max(0, y2 - TILE_SIZE)

            tile = img.crop((x1, y1, x2, y2))
            tile_path = f"{base}_tile_{r}_{c}.jpg"
            tile.save(tile_path, 'JPEG', quality=88)
            results.append(tile_path)

    return {"tiles": results, "grid": (cols, rows), "orig_w": orig_w, "orig_h": orig_h, "eff_w": TILE_SIZE, "eff_h": TILE_SIZE}
