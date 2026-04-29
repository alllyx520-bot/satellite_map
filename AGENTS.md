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
  models.py                     # ChatHistory model
  views.py                      # API views: get-img, show-img, ai-query, history, report
  urls.py                       # API routes
  utils/
    get_satellite_image.py      # Mapbox satellite fetch with retry + progress tracking
    image_preprocessor.py       # Adaptive tiling/scaling for AI input
static/
  browser.js                    # Map interaction, selection, chat, history, compare, zoom, layers, shortcuts
  browser.css                   # Apple-inspired dark glass UI
templates/
  browser.html                  # Leaflet map + chat modal + history panel + model selector
media/
  satellite_imgs/               # Image cache (tiled/stitched large scenes)
  reports/                      # Generated Word reports
.env                            # MAPBOX_TOKEN + DASHSCOPE_API_KEY (gitignored)
```

## API Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Main map page |
| POST | `/api/satellite/get-img/` | Download satellite image (auto-resolution, returns spatial metadata + progress id) |
| GET | `/api/satellite/show-img/` | Serve saved image by `?file=` |
| POST | `/api/ai/query-region/` | AI analysis (single img or multi `file_names[]` for comparison). `model` param: `qwen3-vl-plus` (default), `qwen3.6-plus`, `qwen3.5-plus`, `qwen3-vl-flash` |
| POST | `/api/ai/history/` | Create chat history record |
| GET | `/api/ai/history/` | List chat history (ordered by newest) |
| DELETE | `/api/ai/history/<id>/` | Delete a chat history record |
| POST | `/api/report/generate/` | Generate Word report from chat messages |
| GET | `/api/report/download/` | Download generated report by `?filename=` |
| GET | `/admin/` | Django admin |

## Key Features

- **大场景拼接**: `get_satellite_image.py` auto-splits large areas into grid cells (1280/2048/3072), stitches with PIL. Resolution cap 4096px.
- **异步瓦片下载**: ThreadPoolExecutor downloads tiles concurrently with `_download_progress` dict + frontend polling progress bar.
- **图像预处理器**: `image_preprocessor.py` — adaptive tiling/scaling based on model max dimension (qwen3-vl-plus/qwen-vl-max: 3584px, others: 2560px).
- **多模型 AI**: Qwen3-VL-Plus (default, strongest VLM in Qwen series), Qwen3.6-Plus (latest unified multimodal), Qwen3.5-Plus (unified vision-language, 1M context), Qwen3-VL-Flash (lightweight). VL models enable `vl_high_resolution_images=True`.
- **空间上下文**: Backend computes GSD (m/px) and area (km²), injected into AI prompt.
- **多区域对比**: Sidebar "对比" mode with checkboxes, sends multiple images to AI in one query.
- **局部放大**: Chat modal "🔍 放大" button + drag to select sub-region → re-fetches at higher res within same chat.
- **聊天历史**: Auto-saves Q&A to `ChatHistory` model; sidebar panel with CRUD (load/delete).
- **地图图层**: Toggle administrative boundary overlay + CartoDB road labels overlay.
- **全局快捷键**: `Esc` cancels download, `Ctrl+Enter` sends chat.
- **报告导出**: `python-docx` generates Word reports with font optimization (`/api/report/generate/` + `/api/report/download/`).

## Key Constraints

- **Mapbox proxy trick**: `proxies={"http": None, "https": None}` bypasses VPN — do not remove.
- **Mapbox SSL retry**: 5 retries with exponential backoff (0.6s interval) for SSL timeouts.
- **CORS middleware** must stay between SessionMiddleware and CommonMiddleware (`settings.py:48`).
- **`start.py`** uses `--noreload` (required for PyInstaller). Dev via `manage.py` for hot reload.
- **DashScope API key** loaded from `.env` (`DASHSCOPE_API_KEY`). Never hardcode in production.
- **`.env` auto-loading**: `manage.py` and `start.py` both call `load_dotenv()` with absolute path.
- **No requirements.txt** — deps via conda. Key: Django 5.2, django-cors-headers, dashscope, Pillow, requests, python-docx.
- **`get_satellite_image.py`**: resolution clamped to [1, 4096], RGBA/P → RGB before JPEG save.
- **Image cache**: `media/satellite_imgs/` is gitignored; clean up old files periodically.

## Testing

```bash
E:\Anaconda\envs\satellite_env\python.exe manage.py test map_api
```

`map_api/tests.py` is currently empty.
