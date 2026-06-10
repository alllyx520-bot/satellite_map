# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

SatelliteSense — a Django web app that downloads Mapbox satellite imagery for a user-drawn region and runs multi-model Qwen VLM analysis on it (大场景遥感图像智能问答系统).

## Commands

All Python MUST use the conda env absolute path (no system Python):

```bash
# Dev (hot reload)
E:\Anaconda\envs\satellite_env\python.exe manage.py runserver
# Run as user (auto-opens browser, --noreload for PyInstaller compatibility)
E:\Anaconda\envs\satellite_env\python.exe start.py
# Migrations
E:\Anaconda\envs\satellite_env\python.exe manage.py makemigrations
E:\Anaconda\envs\satellite_env\python.exe manage.py migrate
# Tests (pure-function unit tests for the core utils)
E:\Anaconda\envs\satellite_env\python.exe manage.py test map_api
# Core loop smoke check: health → recommendation → AI → history → report
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline
# Agent loop smoke check: adds mocked RemoteSensingAgent session → Sentinel-2 → NDWI → VL → DeepSeek review
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline --agent
# Optional live dependency checks. These consume external services/API quota.
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline --live-mapbox
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline --live-sentinel
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline --live-ai
E:\Anaconda\envs\satellite_env\python.exe manage.py smoke_pipeline --live-mapbox --live-sentinel --live-ai
# RemoteCLIP weights (one-time, ~350MB → models/RemoteCLIP-ViT-B-32.pt)
E:\Anaconda\envs\satellite_env\python.exe scripts\download_remoteclip.py
```

Deps installed via conda; `requirements.txt` is a `pip freeze` snapshot for reproduction (`pip install -r requirements.txt`). Key: Django 5.2, django-cors-headers, dashscope, Pillow, requests, python-docx, python-dotenv. **RemoteCLIP tile retrieval** needs `torch` (CUDA build for GPU — `pip install torch --index-url https://download.pytorch.org/whl/cu121`) + `open_clip_torch`; these are **optional** — without them the tile ranker falls back to a color/edge heuristic.

Secrets in `.env` (gitignored, loaded by both `manage.py` and `start.py` via `load_dotenv` with absolute path): `MAPBOX_TOKEN`, `DASHSCOPE_API_KEY`, `AMAP_KEY`, `DEEPSEEK_API_KEY`.

## Verification / Smoke Checks

`smoke_pipeline` is the project-level acceptance check for the stable demo loop. The default command does **not** call external AI or imagery services: it creates a local smoke image, mocks the Qwen response, then verifies system health, task-adaptive source recommendation, AI analysis, history save/load, Word report generation, report download, and cleanup.

Use the live flags only when you need to prove external dependencies:

| Command | What it proves | External dependency |
|---------|----------------|---------------------|
| `manage.py smoke_pipeline` | Backend core loop is working without paid/network dependencies | none |
| `manage.py smoke_pipeline --live-mapbox` | Default Mapbox basemap download, progress polling, image persistence, and cleanup work | `MAPBOX_TOKEN`, Mapbox network/quota |
| `manage.py smoke_pipeline --live-sentinel` | Sentinel-2 STAC search, candidate scoring, TiTiler rendering, scene metadata, and cleanup work | Element84 Earth Search, TiTiler |
| `manage.py smoke_pipeline --live-ai` | Real DashScope visual model call, output parsing/fallback, history, and report chain work | `DASHSCOPE_API_KEY`, model quota |
| `manage.py smoke_pipeline --agent` | Mocked RemoteSensingAgent loop, session state, Sentinel-2 selection, NDWI summary, VL analysis, DeepSeek review, and history persistence | none |
| `manage.py smoke_pipeline --live-mapbox --live-sentinel --live-ai` | Full live dependency loop is currently usable | all of the above |

The command prints JSON with per-step `ok` values. It removes smoke images, report files, database records, and progress entries unless `--keep-artifacts` is passed. If a live flag fails but the default smoke check passes, treat it first as an external key/network/quota/provider problem rather than a local code failure.

## API Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Main map page |
| GET | `/api/system/health/` | Health/config report, imagery strategy, smart pipeline, source roles, and analysis modes |
| POST | `/api/satellite/get-img/` | Start background download; returns `file_name`, `total_tiles`, GSD/area metadata |
| POST | `/api/satellite/get-sentinel-img/` | Search recent Sentinel-2 L2A candidates, select the best renderable scene, return image + traceable metadata |
| GET | `/api/satellite/show-img/?file=` | Serve a saved image (falls back to newest if no `file`) |
| GET | `/api/satellite/progress/?file=` | Poll download progress |
| GET / POST | `/api/imagery/recommend-source/` | Recommend Mapbox vs Sentinel-2 from the user question and current source |
| GET | `/api/imagery/scenes/` | List recent imagery scenes and metadata |
| GET | `/api/imagery/scenes/<id>/` | Read one imagery scene and metadata |
| POST / GET | `/api/agent/sessions/` | Create a one-sentence RemoteSensingAgent investigation / list recent sessions |
| GET | `/api/agent/sessions/<id>/` | Read one Agent session, including `slots`, `plan`, `timeline`, `observer`, `artifacts`, `scene_id`, and `history_id` |
| POST | `/api/agent/sessions/<id>/messages/` | Send user confirmation or report request. Waiting options include continuing high-cloud scenes, expanding the time range, switching to Mapbox, canceling, and generating the Word report after completion |
| POST | `/api/ai/query-region/` | AI analysis. Single `file_name` or `file_names[]` (≥2 → compare). Supported UI modes: `precise` → `qwen3-vl-plus` + active perception, `fast` → `qwen3-vl-flash` without active perception. Optional `gsd` (m/px) + `bbox` (geo extent) drive measurement/geo-grounding; `self_check` (default off) enables self-consistency. Returns `answer`, `active_stages`, `targets[]` (label/lat/lng/size), scene metadata, and `analysis_method` |
| GET / POST | `/api/ai/history/` | List / upsert chat history |
| GET / DELETE | `/api/ai/history/<id>/` | Load / delete one record |
| GET | `/api/geo/search/?q=` | Gaode POI search |
| POST | `/api/report/generate/` | Generate Word report → returns download URL |
| GET | `/api/report/download/?file=` | Download generated report |
| GET | `/admin/` | Django admin |

## Architecture

Single Django app `map_api`. Frontend is one server-rendered page (`templates/browser.html` + `static/browser.js` + `static/browser.css`) using Leaflet; no build step. All logic lives in `map_api/views.py` orchestrating the `utils/`, `imagery_sources/`, and management command modules. All dashscope calls go through `_call_qwen()` (centralizes the VL-only `vl_high_resolution_images` kwarg); all user-supplied filenames go through `safe_media_path()` (path-traversal guard).

### Imagery strategy

The current product strategy is **task-adaptive dual source**:
- Mapbox remains the default high-resolution reference basemap. It is best for buildings, roads, facilities, spatial pattern, and small-target visual interpretation, but it lacks traceable acquisition date/cloud/product metadata.
- Sentinel-2 L2A is an optional recent public source. It is selected through Element84 Earth Search, scored by recency/cloud/GSD/product traceability, rendered through TiTiler, and best suited for macro land-use, water, vegetation, agriculture, and change-screening tasks.
- `/api/imagery/recommend-source/` converts the user's question into a source recommendation. Detail/counting tasks prefer Mapbox; recent/macro/agriculture/water/vegetation/change tasks prefer Sentinel-2.
- Reports, history, and the scene panel preserve source quality, confidence, source recommendation, Sentinel candidate selection, and AI output quality metadata.

### RemoteSensingAgent

The Agent layer is intentionally controlled rather than fully autonomous. A user can type a goal such as `帮我调查南宁市在2026年四月的水体情况`; the backend creates an `AgentSession`, asks DeepSeek (`deepseek-v4-flash`) to parse slots and plan, resolves the place through Gaode administrative district search, picks the image source, retrieves imagery, runs lightweight NDWI for water tasks, calls the existing Qwen VL analysis chain, and then asks DeepSeek to review the conclusion.

- Default mode is `precise`: `deepseek-v4-flash` controller + `qwen3-vl-plus` vision model.
- Fast mode is `fast`: `deepseek-v4-flash` controller + `qwen3-vl-flash` vision model.
- Missing `DEEPSEEK_API_KEY` fails immediately. There is no rule-only fallback for Agent planning.
- Sentinel-2 is used for water, vegetation, agriculture, macro land-use, timeliness, and change-screening tasks. Mapbox is used for buildings, roads, facilities, small targets, and detail-heavy visual interpretation.
- First version uses administrative `bbox` screening, not exact polygon clipping, and selects one best Sentinel-2 candidate rather than doing monthly compositing.
- Frontend polls the session and renders `observer`: current stage, public reasoning summary, what the Agent is doing, the next step, and per-plan-step status. This is the Codex-like progress view; never expose private chain-of-thought.

### Two main request flows

**1. Image download** (`POST /api/satellite/get-img/` → `get_satellite_img_api`)
- Picks resolution from the region's haversine diagonal if not given (1280 / 2048 / 3072, clamped to 4096).
- Returns immediately with a `file_name` and `total_tiles`, then downloads in a **background thread**. Frontend polls `GET /api/satellite/progress/?file=` against the shared in-memory `_download_progress` dict (`prune_progress()` caps it at 50 entries).
- `fetch_satellite_image` splits large scenes into a ≤1280px grid, fetches cells **in parallel** (`ThreadPoolExecutor`, 4 workers; `_fetch_tile` retries/backoff; progress increment lock-protected), stitches with PIL. Spatial metadata (GSD m/px, area km²) computed in the view and returned.

**1b. Sentinel-2 optional source** (`POST /api/satellite/get-sentinel-img/` → `get_sentinel_img_api`)
- Normalizes bbox and validates `max_cloud`, `target_resolution`, and `candidate_limit`.
- Searches recent Sentinel-2 L2A candidates through Element84 Earth Search.
- Sorts candidates by `suitability_score` and acquisition time, then attempts rendering in rank order. If the best candidate cannot render, it falls back to the next candidate and records `render_fallback_errors`.
- Creates an `ImageryScene` with product id, acquisition date, cloud percent, GSD, license, score reasons, candidate pool size, selected rank, and TiTiler render metadata.
- Reuses local cached Sentinel scenes when bbox/product/render parameters match.

**2. AI analysis** (`POST /api/ai/query-region/` → `ai_query_region`) — the core pipeline:
```
analyze_query(question)              # smart_query_analyzer.py — intent, entities (中文), spatial hints
  → smart_prepare_image_v2(...)      # image_preprocessor.py — adaptive: {"single": path} (small/scaled)
                                     #   or {"tiles":[overview,...], "grid"} (very large), with tile ranking
       rank_tiles → clip_retriever.score_tiles  # RemoteCLIP: 中文实体→英文语义短语→文本/图块余弦相似度
                                                 #   (falls back to color/edge heuristic if torch/weights absent)
  → if active_perception: Active Perception 迭代放大 (ZoomEye-style, ≤MAX_ZOOM_LEVELS=2)  # active_perception.py
       Stage 1: 1024px image + build_stage1_prompt → <think> + optional bbox_2d + <answer>
       loop: extract_bbox_from_response(scale_factor=ap_scale → 原图坐标)
             → cut_image_geom (crop full-res ≤3584px, returns crop geometry)
             → re-query; model may emit a new bbox (in CROP coords) → map_bbox_to_original → next level
       _measure_and_locate: bbox + gsd → 真实米/公顷 (measure_bbox); bbox + geo extent → 经纬度 (pixel_bbox_to_geo)
     else: single-shot prompt with overview/tiles
  → _call_qwen(model, messages)  # centralizes VL-only vl_high_resolution_images kwarg
  → normalize_model_answer(...)  # extracts <answer>, falls back to raw text if the model ignores the requested format
  → returns answer (+ GSD measurement footer), active_stages, targets[] (for frontend map markers), analysis_method metadata
```
- `MAX_DIM_MAP` (`image_preprocessor.py`) sets per-model max input dim (VL 3584, others 2560); `adaptive_resolution(question, base_dim)` bumps for detail / lowers for macro.
- **Active perception gate** is just `use_active_perception` (default true) — the model itself decides whether to emit a zoom bbox (no regex veto).
- **Compare** mode (`file_names[]` ≥2): images labeled A/B/C… in one query; active perception skipped.
- **GSD measurement + geo-grounding** (`gsd`/`bbox` in request): the zoomed target's pixel bbox → real size (m, hectares) and center lon/lat → returned in `targets[]` and appended to the answer; frontend drops a Leaflet marker.
- **Tile ranking**: `clip_retriever.py` loads RemoteCLIP (ViT-B-32, GPU if available) as a lazy singleton; `rank_tiles` builds an English RS phrase from the Chinese entities and scores tiles by cosine similarity. Any failure (no torch / no weights) → falls back to the `compute_tile_features`/`score_tile_relevance` color heuristic.
- **Output stability**: `normalize_model_answer` records whether the model returned a structured `<answer>`, whether fallback parsing was used, and whether self-check was applied. This data is preserved in chat history and Word reports as `analysis_method.output_quality`.

### Other endpoints
- `geo_search` (`/api/geo/search/`) — Gaode (高德) POI search; key from `AMAP_KEY` env var (works natively from mainland China, no proxy needed).
- `generate_report` / `download_report` — `python-docx` Word export of a chat, embedding the image.
- `chat_history_list` / `chat_history_detail` — CRUD over the single `ChatHistory` model (`update_or_create` keyed on `image_file`).

### Storage
- `media/satellite_imgs/` — downloaded + preprocessed images (`_hd`/`_overview`/`_tile_*`/`_crop_*`/`_stage1_*` derivatives accumulate here). **Gitignored; grows unbounded — clean periodically.**
- `media/reports/` — generated `.docx`. `db.sqlite3` holds chat history.

## Constraints (do not break)

- **Mapbox proxy bypass**: every outbound `requests` call passes `proxies={"http": None, "https": None}` to bypass a system VPN. Removing it breaks downloads.
- **CORS middleware order**: `corsheaders.middleware.CorsMiddleware` must sit between `SessionMiddleware` and `CommonMiddleware` (`settings.py:48`).
- **`start.py` uses `--noreload`** (required for PyInstaller exe builds). Use `manage.py runserver` for hot reload during dev.
- **VL-only kwarg**: `vl_high_resolution_images=True` is added only for `qwen3-vl-*` / `qwen-vl-*` models — do not send it to unified models.
- Images converted RGBA/P → RGB before JPEG save throughout; resolution clamped to `[1, 4096]`.
- `DEBUG=True` and `SECRET_KEY` are dev defaults in `settings.py` — not production-ready.
