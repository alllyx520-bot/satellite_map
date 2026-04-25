# AGENTS.md — SatelliteSense

## Quick Start

```bash
E:\Anaconda\envs\satellite_env\python.exe manage.py runserver
# or (auto-opens browser, with --noreload for exe compatibility):
E:\Anaconda\envs\satellite_env\python.exe start.py
```

Conda env: `satellite_env` at `E:\Anaconda\envs\satellite_env`

## Architecture

```
satellite_map/                  # Django project config
map_api/
  views.py                      # 3 API views: get-img, show-img, ai-query
  utils/get_satellite_image.py  # Mapbox satellite fetch (auto-tiling up to 4096px)
templates/browser.html          # Leaflet map + chat modal
static/
  browser.js                    # Map interaction, selection, chat, compare, zoom
  browser.css                   # Apple-inspired dark glass UI
media/satellite_imgs/           # Image cache (tiled/stitched large scenes)
```

## API Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Main map page |
| POST | `/api/satellite/get-img/` | Download satellite image (auto-resolution, returns spatial metadata) |
| GET | `/api/satellite/show-img/` | Serve saved image by `?file=` |
| POST | `/api/ai/query-region/` | AI analysis (single img or multi `file_names[]` for comparison) |
| GET | `/admin/` | Django admin |

## Key Features

- **大场景拼接**: `get_satellite_image.py` auto-splits large areas into 1280px grid cells, stitches with PIL. Resolution auto-calculated from bbox size (1280/2048/3072), cap 4096.
- **空间上下文**: Backend computes GSD (m/px) and area (km²), injected into AI prompt.
- **多区域对比**: Sidebar "对比" mode with checkboxes, sends multiple images to AI in one query.
- **局部放大**: Chat modal "🔍 放大" button + drag to select sub-region → re-fetches at higher res within same chat.
- **AI model**: `qwen3.5-plus` via `dashscope.MultiModalConversation.call()`. Supports multi-image input for comparison.

## Key Constraints

- **Mapbox proxy trick**: `proxies={"http": None, "https": None}` bypasses VPN — do not remove.
- **CORS middleware** must stay between SessionMiddleware and CommonMiddleware (`settings.py:48`).
- **`start.py`** uses `--noreload` (required for PyInstaller). Dev via `manage.py` for hot reload.
- **DashScope API key** hardcoded in `views.py:116` — replace with env var before production.
- **No requirements.txt** — deps via conda. Key: Django 5.2, django-cors-headers, dashscope, Pillow, requests.
- **`get_satellite_image.py`**: resolution clamped to [1, 4096], RGBA/P → RGB before JPEG save.

## Testing

```bash
E:\Anaconda\envs\satellite_env\python.exe manage.py test map_api
```

`map_api/tests.py` is currently empty.
