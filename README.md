# SatelliteSense Remote Sensing Workbench

SatelliteSense is a Django + Leaflet remote-sensing analysis workbench. It lets users draw a region on a map, fetch imagery from Mapbox or Sentinel-2, run Qwen VL analysis, save history, generate Word reports, and start a controlled investigation Agent from a natural-language goal.

This README is written for the next coding agent or teammate taking over the project.

## Current Project Direction

Build a stable, usable intelligent remote-sensing interpretation tool for a student innovation project.

Core direction:

- Keep Mapbox high-resolution basemap as the default visual-detail workflow.
- Add Sentinel-2 L2A recent public imagery as an optional traceable source.
- Keep only two analysis modes:
  - `precise`: `deepseek-v4-flash` controller + `qwen3-vl-plus`
  - `fast`: `deepseek-v4-flash` controller + `qwen3-vl-flash`
- Default mode is `precise`.
- Focus on the product and analysis chain first. Do not prioritize slides, reports, or defense materials before the tool itself is stable.
- Important product principle: users should not be forced to manually verify image-source suitability. The system should choose, validate, and explain image sources as much as possible.

## Tech Stack

- Backend: Django 5.2
- Frontend: server-rendered HTML, plain JavaScript, Leaflet, local CSS
- AI/VL: DashScope Qwen VL
- Agent controller: DeepSeek API
- Map source:
  - Mapbox static satellite imagery
  - Sentinel-2 L2A through Element84 Earth Search + TiTiler
- Reports: `python-docx`
- Database: SQLite in local dev

There is no frontend build step.

## Project Layout

```text
manage.py                     Django entrypoint (loads .env first)
start.py                      User-facing launcher (auto-opens browser, --noreload)
requirements.txt              Curated dev dependencies (core + optional extras)
requirements-prod.txt         Minimal production dependencies
README.md                     This file: product direction, setup, verification
CLAUDE.md                     Agent-oriented implementation deep dive
DEPLOY.md                     Alibaba Cloud ECS deployment runbook

satellite_map/                Django project package
  settings.py                 Env-driven settings (DEBUG/hosts/CSRF/CORS)
  urls.py                     / (landing), /workbench/, /api/, /admin/
  env.py                      Shared .env loader

map_api/                      Single Django app with all backend logic
  views.py                    API orchestration (~3.4k lines)
  models.py                   ChatHistory, DownloadTask, ImageryScene, AgentSession
  urls.py                     /api/ routes
  tests.py                    Unit + API tests (manage.py test map_api)
  imagery_sources/            Provider abstraction: mapbox.py, earth_search.py
  utils/                      Download, preprocessing, query analysis, RemoteCLIP,
                              active perception, analysis strategy, Agent tools
  management/commands/
    smoke_pipeline.py         Project-level acceptance check

templates/
  home.html                   Landing page (route /)
  browser.html                Main workbench page (route /workbench/)

static/
  home.css / home.js          Landing page assets (Three.js globe via CDN)
  browser.css / browser.js    Workbench UI styling and logic
  remixicon.css / .woff2      Local icon assets

deploy/                       nginx config + systemd unit for the ECS
scripts/                      One-off utilities (RemoteCLIP weight download)

models/                       RemoteCLIP weights, ignored by git
media/                        Runtime images/reports/logs, ignored by git
output/                       Screenshots/logs, ignored by git
test-results/                 Playwright/test artifacts, ignored by git
```

Use the conda Python environment explicitly:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe
```

## Environment Variables

Secrets live in `.env`, which is ignored by git.

Required or useful keys:

```text
MAPBOX_TOKEN=...
DASHSCOPE_API_KEY=...
DEEPSEEK_API_KEY=...
AMAP_KEY=...
TITILER_ENDPOINT=https://titiler.xyz
```

Sentinel tuning knobs:

```text
SENTINEL_MIN_COVERAGE_RATIO=0.92
SENTINEL_MIN_VALID_IMAGE_RATIO=0.88
SENTINEL_MAX_MOSAIC_CANDIDATES=6
AGENT_SENTINEL_CANDIDATE_LIMIT=15
```

Defaults are defined in `map_api/views.py`. Do not commit real API keys.

## Run Locally

First-time setup (core dependencies only; RemoteCLIP extras are optional):

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe -m pip install -r requirements.txt
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py migrate
```

Start the dev server:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py runserver 127.0.0.1:8000
```

Open:

```text
http://127.0.0.1:8000/             Landing page
http://127.0.0.1:8000/workbench/   Analysis workbench
```

Alternative user-facing launcher:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe start.py
```

## Deployment Notes

Full deployment steps are in `DEPLOY.md`.

The current Alibaba Cloud ECS deployment reuses the verified ERP server access
notes from `D:\Projects\AAAprojects\ERP\README.md`:

```text
Server: root@101.200.128.20
SSH key path: D:\Projects\AAAprojects\ERP\.codex-ssh\huixianglian_deploy_ed25519
SatelliteSense URL: http://101.200.128.20:8083/
Gunicorn internal bind: 127.0.0.1:8010
Server project directory: /opt/satellitesense
```

Important coexistence rule:

- `http://101.200.128.20/` is the existing ImageFlow site.
- `http://101.200.128.20/showcase/` is the ERP showcase.
- Do not overwrite the server root nginx route or the `/showcase/` route.
- Deploy SatelliteSense on port `8083` or another user-confirmed port greater
  than or equal to `8083`.
- Never commit, upload, paste, or print the private SSH key content.

## Verification Commands

Use these before handing off meaningful changes:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py check
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py test map_api
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline
node --check static\browser.js
```

Optional live dependency checks:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-mapbox
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-sentinel
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-ai
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --agent
```

Default `smoke_pipeline` is mocked and does not call paid/network AI services. Live flags depend on external APIs and quota.

## Main User Flows

### 1. Manual Region Analysis

1. User draws a region on the map.
2. Frontend chooses image source from the top controls:
   - `高清底图 / Mapbox`
   - `近期公开影像 / Sentinel-2`
3. Backend downloads or renders an image.
4. Region card appears in the right workspace.
5. User enters the analysis cabin.
6. User asks questions.
7. Backend calls Qwen VL through `/api/ai/query-region/`.
8. History and report generation reuse the same scene metadata.

### 2. Agent Investigation

User can type something like:

```text
帮我调查南宁市在2026年四月的水体情况
```

The controlled Agent flow:

1. Create `AgentSession`.
2. DeepSeek parses goal into structured slots.
3. Gaode resolves place to administrative bbox.
4. Agent picks image source.
5. Sentinel-2 is used for water, vegetation, agriculture, land-use, timeliness, and change-screening tasks.
6. Mapbox is used for small targets, buildings, roads, and detail-heavy questions.
7. Sentinel imagery is retrieved and quality-checked.
8. Water tasks compute lightweight NDWI.
9. Qwen VL interprets image.
10. DeepSeek reviews the final conclusion.
11. User can generate a Word report after completion.

The frontend shows a Codex-like public progress view: current stage, what the Agent is doing, and next step. Do not expose private chain-of-thought.

## Key Routes

Pages:

```text
GET  /                             Landing page (home.html)
GET  /workbench/                   Analysis workbench (browser.html)
GET  /admin/                       Django admin
```

APIs:

```text
GET  /api/system/health/
POST /api/satellite/get-img/
POST /api/satellite/get-sentinel-img/
GET  /api/satellite/show-img/?file=...
GET  /api/satellite/progress/?file=...
POST /api/satellite/cleanup/
GET  /api/imagery/search/
GET/POST /api/imagery/recommend-source/
GET  /api/imagery/scenes/
GET  /api/imagery/scenes/<id>/
GET/POST /api/agent/sessions/
GET  /api/agent/sessions/<id>/
POST /api/agent/sessions/<id>/messages/
POST /api/ai/query-region/
GET/POST /api/ai/history/
GET/DELETE /api/ai/history/<id>/
GET  /api/geo/search/?q=...
POST /api/report/generate/
GET  /api/report/download/?file=...
```

## Sentinel-2 Handling Notes

This part is critical. A recent bug showed large black areas in Sentinel previews. The root cause was not frontend CSS. Sentinel-2 scenes are split into tiles/granules; if a user bbox crosses scene coverage, TiTiler may render uncovered parts as black no-data pixels.

Current hardening:

- Default target coverage is high: `0.92`.
- Default valid rendered pixel ratio is high: `0.88`.
- Default candidate pool is `10` for the public endpoint.
- The Agent uses a larger candidate pool through `AGENT_SENTINEL_CANDIDATE_LIMIT`.
- Single-scene renders with large edge no-data are rejected.
- Multi-scene mosaics are attempted when one scene does not cover the bbox.
- If a mosaic still has large no-data edges, the request fails and tells the user to shrink range, expand dates, or switch to Mapbox.
- Tiny edge crop is allowed only for small rendering artifacts. Large crop is treated as coverage failure.

Relevant functions in `map_api/views.py`:

```text
select_sentinel_scene_candidates
greedy_cover_sentinel_candidates
sentinel_retrieval_result
compose_sentinel_mosaic
crop_sentinel_nodata_border
sentinel_nodata_crop_too_large
postprocess_cached_sentinel_scene
```

When debugging Sentinel results, inspect:

```text
scene.metadata.target_coverage_ratio
scene.metadata.valid_image_ratio
scene.metadata.no_data_crop
scene.metadata.mosaic_candidates
scene.metadata.render_fallback_errors
```

## Model Modes

Frontend model selection must stay simple:

```text
precise -> qwen3-vl-plus
fast    -> qwen3-vl-flash
```

Agent controller:

```text
deepseek-v4-flash
```

If `DEEPSEEK_API_KEY` is missing, Agent creation should fail clearly. Do not silently fall back to rule-only planning for Agent mode.

## Frontend Design State

Two server-rendered pages, no build step:

- `/` is a lightweight landing page (`home.html` + `static/home.css` / `home.js`) with a Three.js globe loaded from CDN; it links into the workbench.
- `/workbench/` is the actual tool (`browser.html` + `static/browser.css` / `browser.js`).

The workbench UI is a dense GIS workbench:

- Left sidebar: Agent console
- Center: map workspace
- Right sidebar: selected regions/history/workspace
- Analysis cabin/modal: image preview + chat + compact metadata

Recent design fixes:

- Removed most emoji controls.
- Uses local Remix Icon assets.
- Custom dropdowns replace ugly native selects.
- Analysis cabin text was reduced.
- Prompt presets are compact chips.
- Modal scrollbars are dark and subtle.
- Sidebar collapse buttons have stable positions.
- Image previews use `object-fit: contain` where aspect ratio matters.

Before changing UI heavily, verify with browser screenshots or at least reload `http://127.0.0.1:8000/` and check desktop/mobile/modal states.

## Known Constraints

- First Agent version uses administrative bbox screening, not exact polygon clipping.
- Sentinel-2 is 10 m class imagery. It is not suitable for counting vehicles, small buildings, roof materials, or narrow road details.
- Mapbox has better visual detail, but lacks traceable acquisition date/cloud/product metadata.
- Sentinel-2 is traceable and recent, but clouds, no-data edges, spatial resolution, and revisit cycle limit confidence.
- NDWI is a lightweight screening metric only. It is not a formal water-body mapping product.
- Generated Word reports should not contain Markdown-style formulas if future formulas are added.

## Git / Artifact Hygiene

Ignored runtime artifacts:

```text
media/
output/
test-results/
db.sqlite3
.env
design-qa.md
```

Do not commit:

- API keys
- downloaded/generated satellite images
- Word reports
- Playwright screenshots
- local QA image comparisons
- SQLite dev database

Commit code, migrations, local static assets, tests, and docs.

## Recent Verified State

Most recent handoff checks:

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py check
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py test map_api
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline
node --check static\browser.js
```

All passed after the Sentinel mosaic hardening and UI cleanup.

## Handoff Advice For The Next Agent

1. Read `README.md` first, then `CLAUDE.md` for extra historical implementation detail.
2. Check `git status --short` before editing.
3. Use the conda Python absolute path.
4. If a user reports a visual problem, first decide whether it is frontend display or backend image content. For Sentinel black regions, suspect coverage/mosaic/no-data before CSS.
5. If touching Sentinel selection, add tests around coverage, valid image ratio, and no-data crop behavior.
6. If touching Agent, preserve the public observer timeline but do not expose hidden chain-of-thought.
7. If touching UI, keep it compact and tool-like; this is a remote-sensing workbench, not a landing page.
