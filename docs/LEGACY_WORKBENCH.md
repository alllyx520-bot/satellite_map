# Legacy 工作台维护资料

以下内容保留自 V3 重构之前的 README，**仅作为当时的时点快照**，用于旧入口与回切维护；
新入口、启动方式、目录结构与命令一律以根 [README.md](../README.md) 与 [CLAUDE.md](../CLAUDE.md) 为准。

## 快照之后已经变化的关键事实（2026-09-14 核对，正文未逐处改写）

| 正文里的说法 | 当前事实 |
|---|---|
| `start.py` 是"User-facing launcher (auto-opens browser, `--noreload`)" | 已改为**同时拉起 Django + `run_v3_worker`**，不再用 `--noreload`，且要求 `static/v3/` 已构建 |
| `GET /` = landing page（`home.html`） | 现在是 **V3 React 工作台**；旧落地页在 `GET /legacy/` |
| `GET /workbench/` = `browser.html` | 现在 / 与 /workbench/ 相同，都是 V3；旧工作台在 `GET /legacy/workbench/` |
| `There is no frontend build step` | 对 legacy 页面成立；**V3 是 Vite 构建产物 `static/v3/`，改前端必须 `npm run build`** |
| Python 用 conda `general` | 改为项目隔离环境 `.codex-runtime/venv/`；conda `general` 缺 `rasterio`（跑不了 V3） |
| `models.py`: ChatHistory/DownloadTask/ImageryScene/AgentSession | 现在还有 V2 DAG 与 V3 的模型（`AgentRun`/`Run*`、`Conversation`/`SpatialAttachment`/…，迁移至 0027） |
| 正文各处的行号与测试数 | 已过时（测试见 CLAUDE.md 的实测基线） |

正文以下部分**原样保留**，不再维护。

---

# SatelliteSense Remote Sensing Workbench

SatelliteSense is a Django + Leaflet remote-sensing analysis workbench. It lets users draw a region on a map, fetch imagery from Mapbox or Sentinel-2, run Qwen VL analysis, save history, generate Word reports, and start a controlled investigation Agent from a natural-language goal.

This README is written for the next coding agent or teammate taking over the project.

## Current Project Direction

Build a stable, usable intelligent remote-sensing interpretation tool for a student innovation project.

Core direction:

- Keep Mapbox high-resolution basemap as the default visual-detail workflow.
- Add Sentinel-2 L2A recent public imagery as an optional traceable source.
- Keep only two analysis modes:
  - `precise`: `deepseek-flash` controller + `qwen3-vl-plus`
  - `fast`: `deepseek-flash` controller + `qwen3-vl-flash`
- Default mode is `precise`.
- Focus on the product and analysis chain first. Do not prioritize slides, reports, or defense materials before the tool itself is stable.
- Important product principle: users should not be forced to manually verify image-source suitability. The system should choose, validate, and explain image sources as much as possible.

## Tech Stack

- Backend: Django 5.2
- Frontend: server-rendered HTML, plain JavaScript, Leaflet, local CSS
- AI/VL: DashScope Qwen VL
- Agent controller: DeepSeek-V4.1-Flash (`deepseek-flash`, DeepSeek API, 2026-09-11 切换);`GLM_API_KEY`+`AGENT_PROVIDER=glm` 为回退旧链路
- Map source(影像源矩阵,2026-09-10 扩展):
  - 高清底图(参考级):Mapbox / 天地图 / Esri World Imagery,瓦片拼接下载
  - Sentinel-2 L2A through Element84 Earth Search + TiTiler(同平台含 sentinel-2-c1-l2a / l1c)
  - Sentinel-1 GRD SAR(全天候)与 Copernicus DEM GLO-30(地形),同一 STAC+TiTiler 管线
  - NASA GIBS 每日宏观底图(前端图层)
- Agent 证据工具(2026-09-10 新增):NASA FIRMS 火点、OSM Overpass 地物语义、Open-Meteo 气象、JRC GSW 水体基线、ESA WorldCover 土地覆盖
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
  views.py                    HTTP layer + AI analysis pipeline + reports (~1.5k lines)
  orchestrator.py             RemoteSensingAgent session orchestration
  sentinel_pipeline.py        Sentinel-2 selection / mosaic / render fallback / cache
  payloads.py                 Response payload builders + output normalization
  geo_math.py                 Pure bbox/GSD/no-data-crop math (no Django deps)
  media_paths.py              SAVE_DIR/REPORT_DIR + path-traversal guard
  middleware.py               Per-IP rate limiting for paid API endpoints
  models.py                   ChatHistory, DownloadTask, ImageryScene, AgentSession
  urls.py                     /api/ routes
  tests.py + test_*.py          479 tests: units + mocked Sentinel/Agent/report integration (2026-09-11)
  imagery_sources/            Provider abstraction: mapbox.py, earth_search.py
  utils/                      Download, preprocessing, query analysis, RemoteCLIP,
                              active perception, analysis strategy, Agent tools
  management/commands/
    smoke_pipeline.py         Project-level acceptance check
    cleanup_stale_tasks.py    Mark zombie downloads error; release stale Agent leases for recovery
    cleanup_media.py          Age-based media cleanup (+ DB record sync)

templates/
  home.html                   Landing page (route /)
  browser.html                Main workbench page (route /workbench/)
  design.html                 SPECTRA design-system reference page (route /design/)

static/
  home.css / home.js          Landing page assets (Three.js globe via CDN)
  browser.css / browser.js    Workbench UI styling and logic
  workbench-ui.js             Workbench micro-interactions
  design.css / design.js / spectra.css   Design page assets
  remixicon.css / .woff2      Local icon assets

deploy/                       nginx config + Gunicorn/Agent worker systemd units for the ECS
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
FIRMS_MAP_KEY=...        # 可选,NASA FIRMS 火点工具(免费申请);缺失时该工具不可用
TIANDITU_KEY=...         # 可选,天地图影像底图源;缺失时该源不可用
```

Sentinel tuning knobs:

```text
SENTINEL_MIN_COVERAGE_RATIO=0.92
SENTINEL_MIN_VALID_IMAGE_RATIO=0.88
SENTINEL_MAX_MOSAIC_CANDIDATES=6
AGENT_SENTINEL_CANDIDATE_LIMIT=15
```

Rate limiting (protects paid endpoints; per IP per minute):

```text
RATELIMIT_API_PER_MINUTE=120
RATELIMIT_AI_PER_MINUTE=30
# database 为默认值，多个 Web worker 共享同一令牌桶；本地纯演示可设 memory
RATELIMIT_BACKEND=database
# RATELIMIT_DISABLED=1    # demo mode only
# 生产建议由持久化 worker 执行 Agent，避免 Web 进程重启丢任务：
AGENT_EXECUTION_MODE=queue
```

`queue` 模式下，Agent 调查、Mapbox 影像下载和 Word 报告都会先持久化到数据库，再由
`run_agent_worker` 执行。Web 进程重启不会丢失任务；报告 worker 被中断后，
过期租约会被后续 worker 接管。影像先写 worker 专属临时文件，通过所有权
校验后才原子切换为正式图片，旧 worker 不能覆盖接管者结果。

Defaults are defined in `map_api/sentinel_pipeline.py`, `map_api/orchestrator.py` and `map_api/middleware.py`. Do not commit real API keys.

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

UI 迭代注意（2026-09-09 实测）：`.env` 未设 `DJANGO_DEBUG` 时按 false 运行——模板被
进程内缓存、`/static/` 改由 `STATIC_ROOT`（staticfiles/）提供。因此改模板需重启进程、
改静态文件需再跑 `collectstatic` 才生效；纯前端截图迭代推荐直接设 `DJANGO_DEBUG=1`
启动（模板与 static/ 都实时生效），验完再用默认模式复核一遍。

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
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-sentinel1
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-firms
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-esri
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline --live-tianditu
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
2. DeepSeek-flash parses goal into structured slots.
3. Gaode resolves place to administrative bbox.
4. Agent picks image source.
5. Sentinel-2 is used for water, vegetation, agriculture, land-use, timeliness, and change-screening tasks.
6. Mapbox is used for small targets, buildings, roads, and detail-heavy questions.
7. Sentinel imagery is retrieved and quality-checked.
8. Water tasks compute lightweight NDWI.
9. Qwen VL interprets image.
10. DeepSeek-flash reviews the final conclusion; Qwen VL remains the primary specialist interpreter.
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
GET  /api/analysis/indices/
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
- 辐射校准以 collection profile 的 `radiometry_override` 为准(2026-09-11 实测:Earth Search 的 sentinel-2-l2a COG 无 +1000 谐波偏移,元数据 offset=-0.1 不可直接使用;sentinel-2-c1-l2a 元数据正确)。 Landsat(PC)使用元数据 scale=2.75e-5/offset=-0.2,实测正确。
- SAR/DEM 场景的有效性判定走 PNG alpha(真实 nodata),不用亮度启发式;这两类场景也不做亮度驱动的边缘自动裁切。
- If a mosaic still has large no-data edges, the request fails and tells the user to shrink range, expand dates, or switch to Mapbox.
- Tiny edge crop is allowed only for small rendering artifacts. Large crop is treated as coverage failure.

Relevant functions (in `map_api/sentinel_pipeline.py` and `map_api/geo_math.py`; all re-exported from `map_api/views.py`):

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
deepseek-flash
```

If `DEEPSEEK_API_KEY` is missing, Agent creation should fail clearly. `GLM_API_KEY`+`AGENT_PROVIDER=glm` is a legacy fallback only. Do not silently fall back to rule-only planning for Agent mode.

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
- 2026-09-09 基线内极致精修：工作台空状态雷达/准星仪器动画、链路状态 pill、地图边缘电影暗角、历史记录 hover 金条指示、spectral-chip 过渡补全；首页 Hero/章节文字深空投影、能力卡片 hover 斜切扫光、Agent 流程条级联流光。实现见 `static/home.css` 末尾「极致精修层」与 `static/browser.css` 对应区块；截图工具 `scripts/shot_ui.py`。

Before changing UI heavily, verify with browser screenshots or at least reload `http://127.0.0.1:8000/` and check desktop/mobile/modal states.

## Known Constraints

- First Agent version uses administrative bbox screening, not exact polygon clipping.
- Sentinel-2 is 10 m class imagery. It is not suitable for counting vehicles, small buildings, roof materials, or narrow road details.
- Mapbox has better visual detail, but lacks traceable acquisition date/cloud/product metadata.
- Sentinel-2 is traceable and recent, but clouds, no-data edges, spatial resolution, and revisit cycle limit confidence.
- NDWI is a lightweight screening metric only. It is not a formal water-body mapping product.
- Sentinel-1 SAR(2026-09-10 新增)是全天候雷达影像:非光学,VL 解译可靠性低,结论限水体/淹没与宏观地物;不支持光谱指数。
- Cop-DEM(2026-09-10 新增)是静态高程模型(采集基线 2011-2015),只作地形参考,不代表拍摄时相地表状态。
- 天地图/Esri(2026-09-10 新增)与 Mapbox 同为参考级底图:无拍摄时间与传感器 GSD,不进入物理测量;Esri 免费条款限非营收且需署名,天地图需 key 且有日配额。
- FIRMS 火点工具需 FIRMS_MAP_KEY;GSW/WorldCover 为历史静态产品,不作近实时判断。
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

Most recent handoff checks(2026-09-10,数据源扩展 M0-M2 完成后):

```powershell
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py check
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py test map_api    # Ran 479 tests OK
C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py smoke_pipeline  # passed (mocked)
node --check static\browser.js
```

All passed. `--live-esri`/`--live-tianditu`/`--live-firms` 均已真调通过(FIRMS 实测巴西区域 5 天 1736 个火点解析正常)。

2026-09-11 全量实测(六影像源真实 API + 五证据工具真实数据 + 逐张视觉评审)修复了四个源契合度缺陷,详见 `docs/DATA_PIPELINE_AUDIT.md` 末节增补:
- sentinel-2-l2a 辐射元数据与实际 DN 语义不符(STAC offset=-0.1,实测 Element84 Sen2Cor COG 无 +1000 偏移)——曾导致 NDVI/NDWI 全部不可用,已由 profile `radiometry_override` 修复;
- SAR/DEM 被云量过滤误杀(SAR/DEM 无 eo:cloud_cover 属性,带过滤即零候选);
- Cop-DEM 静态数据被日期过滤误杀(datetime 是 2021 生产发布日期);
- SAR/DEM 的暗区被亮度启发式误杀(有效性改走 PNG alpha 真实 nodata),DEM 固定拉伸区间对低起伏区失效(改 bbox 内 p2~p98 自适应拉伸)。

## Handoff Advice For The Next Agent

1. Read `README.md` first, then `CLAUDE.md` for extra historical implementation detail.
2. Check `git status --short` before editing.
3. Use the conda Python absolute path.
4. If a user reports a visual problem, first decide whether it is frontend display or backend image content. For Sentinel black regions, suspect coverage/mosaic/no-data before CSS.
5. If touching Sentinel selection, add tests around coverage, valid image ratio, and no-data crop behavior.
6. If touching Agent, preserve the public observer timeline but do not expose hidden chain-of-thought.
7. If touching UI, keep it compact and tool-like; this is a remote-sensing workbench, not a landing page.
