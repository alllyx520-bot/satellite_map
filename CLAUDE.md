# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

SatelliteSense — 大场景遥感图像智能问答 Agent（Django + React）。用户在地图上框选区域，或上传 PNG/JPEG/GeoTIFF；后端 Agent 自主检索影像、调用数据产品、运行 Python 分析，交付**带可点击证据**的答案。

> **文档坐标**：本文是"当前实现"的唯一入口。历史设计/审查记录（`REVIEW_PLAN.md`、`REVIEW_FINDINGS.md`、`docs/AGENT_*`、`docs/DATA_PIPELINE_AUDIT.md`、`docs/LEGACY_WORKBENCH.md`）保留为当时的时点记录，与本文冲突时以本文和当前代码为准。
>
> **最后与代码核对**：2026-09-14。上面这轮实测结果见下方 [Verification](#verification--smoke-checks)。

## 三代实现并存 — 读代码前先确认在看哪一代

同一个仓库里有三代东西在跑，这是最容易迷路的地方。**改任何代码前先确认它属于哪一代。**

| 代号 | 用户入口 | API 前缀 | 后端位置 | 状态 |
|------|---------|---------|---------|------|
| **V3**（主力） | `/`、`/workbench/`、`/agent/`、`/v3/` | `/api/v3/*` | `map_api/v3/` + `frontend/` | 开发中，功能最全 |
| **V2 DAG**（持久化运行引擎） | 无独立页面 | `/api/v2/agent/runs/*` | `map_api/run_*.py` + `map_api/agent/` | 已实现；`deploy/` 的 systemd 仍指向它的 worker |
| **V2 legacy**（旧工作台） | `/legacy/`、`/legacy/workbench/` | `/api/*`（非 v3） | `views.py`、`orchestrator.py`、`sentinel_pipeline.py` 等 | 保留可用，不再扩展 |

V3 与 V2 legacy 共用底层数据源、影像源 provider、光谱指数与部分工具函数（`utils/`、`imagery_sources/`、`remote_sensing_indices.py`）。改这些共享层会同时影响三代。

## Commands

### 运行环境（重要）

**默认用项目自带隔离环境，不要用 conda `general`**：conda `general`（Python 3.11）缺 `rasterio`/`pyproj`/`psycopg`，**跑不了 V3 代码和 V3 测试**（`map_api/test_v3_*.py` 在模块层 `import rasterio`）。

```bash
# 项目隔离环境（Python 3.11.15 / Django 5.2.13 / rasterio 1.4.4）—— 默认用这个
.codex-runtime/venv/Scripts/python.exe <args>
```

`PYTHONUTF8=1` 建议始终设置（中文输出/读取 UTF-8 文件必需，`start.py` 内部也会自己设）。

首次配置：

```powershell
.\.codex-runtime\venv\Scripts\python.exe -m pip install -r requirements-v3.txt
npm --prefix frontend ci
npm --prefix frontend run build      # 必需：Django 只服务构建产物 static/v3/
.\.codex-runtime\venv\Scripts\python.exe manage.py migrate
```

### 启动

```powershell
# 一键启动：Django + 持久化 V3 worker，自动开浏览器
.\.codex-runtime\venv\Scripts\python.exe start.py            # 可选 --port 8013 --no-browser

# 只起 Django（热重载，V3 任务不会被执行——需要另开 worker）
.\.codex-runtime\venv\Scripts\python.exe manage.py runserver
.\.codex-runtime\venv\Scripts\python.exe manage.py run_v3_worker
```

`start.py` 会先检查 `static/v3/.vite/manifest.json`，前端没构建就直接退出并提示。它还会做端口独占探测、只跑 `migrate --check`（不自动改数据库）、`DJANGO_DEBUG` 未开时先 `collectstatic`。

### 迁移与数据库

```powershell
.\.codex-runtime\venv\Scripts\python.exe manage.py makemigrations
.\.codex-runtime\venv\Scripts\python.exe manage.py migrate            # 当前迁移至 0027
```

### 测试与前端

```powershell
# 后端：608 项（2026-09-14 实测，约 55s；无需外部网络/密钥）
.\.codex-runtime\venv\Scripts\python.exe manage.py test map_api --noinput
# 前端：24 项 + 类型检查
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build        # 改完 TS/CSS 必须重建，否则页面不变
npm --prefix frontend run dev          # Vite dev server，/api 代理到 Django
```

### 全部 management commands

| 命令 | 作用 |
|------|------|
| `smoke_pipeline [--flags]` | 项目级验收自检，见下表 |
| `run_v3_worker [--once] [--conversations N]` | **V3 持久化 worker**：处理附件瓦片 + 执行 `AgentRun`（默认 2 并发会话 + 2 并发附件） |
| `run_agent_worker [--once] [--poll-seconds 5] [--max-sessions 1] [--stale-after N] [--claim-timeout 900]` | **V2 worker**：恢复 running 的 `AgentSession`、报告任务、下载任务 |
| `cleanup_stale_tasks [--minutes 10] [--dry-run]` | 释放超时的 `DownloadTask` 与 `AgentSession` 旧租约（V2；V3 租约由 worker 自己接管） |
| `cleanup_media [--age 7] [--all] [--dry-run]` | 删除 `media/satellite_imgs` + `media/reports` 中过期文件并同步 DB 记录。**不覆盖 `media/v3-assets`** |
| `backup_v3_history` / `migrate_v3_history [--apply]` | 旧 `ChatHistory`/`AgentSession` → V3 会话的显式迁移（先备份，dry-run 无写入） |
| `evaluate_v3 [--validate] [--run] [--variant baseline\|candidate-a\|candidate-b] [--harness-samples N] [--summarize]` | 公开题库评测；`--run` 才真调模型 |

### 验收脚本（`scripts/`）

| 脚本 | 用途 |
|------|------|
| `verify_v3_interactions.py` | 浏览器验收：真实瓦片、框选、滑杆、刷新、IME、窄屏（需本地 8000 已启动） |
| `verify_local_agent.py` | 真实模型 + 本地 Python 执行，核对像元均值/证据引用/产物哈希 |
| `validate_real_v3_products.py` | 联网读取原始公开 COG，验证真实数据产品 |
| `shot_ui.py <tag> [home,workbench,design]` | Playwright + msedge 截图 → `output/ui-review/` |
| `download_remoteclip.py` | 一次性下载 RemoteCLIP 权重（~350MB → `models/`） |

### 依赖

- `requirements.txt` — 核心 dev 依赖（Django 5.2.13、dashscope、numpy、Pillow、python-docx、requests、jsonschema、tifffile、lxml）。
- `requirements-v3.txt` — **V3 必需**：`-r requirements.txt` + rasterio 1.4.4、pyproj 3.7.2、shapely 2.1.2、scipy 1.17.1、`psycopg[binary]`。
- `requirements-prod.txt` — 最小生产集（含 gunicorn）。
- **RemoteCLIP**（`utils/clip_retriever.py`）需要 `torch` + `open_clip_torch`，**可选**（`requirements.txt` 里注释掉）：缺失时切块排序回退到颜色/边缘启发式。

### 环境变量

Secrets 在 `.env`（gitignored，由 `satellite_map/env.py` 的 `load_project_env` 读取）。

**模型与密钥**

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` + `DEEPSEEK_CHAT_URL` | V3 主控 `deepseek-flash`（原生工具调用 + 图像输入）；缺 key 时 V3 直接失败，无纯规则降级 |
| `DASHSCOPE_API_KEY` | 视觉复核；V3 基线 `qwen3-vl-plus` |
| `V3_VISION_MODEL` | 覆盖 V3 视觉角色模型（评测晋升后再改） |
| `MAPBOX_TOKEN` / `AMAP_KEY` | 默认底图 / 高德地名 |
| `FIRMS_MAP_KEY` / `TIANDITU_KEY` | 可选源；缺失时仅对应功能不可用 |
| `AGENT_MODEL` / `AGENT_PROVIDER` / `GLM_*` / `QWEN_CHAT_URL` | V2 legacy Agent 的模型选择（`AGENT_PROVIDER=glm` 为旧链路回退） |

**V3 运行时**

| 变量 | 用途 |
|------|------|
| `V3_PYTHON_RUNTIME` | `local`（默认，项目 Python 子进程，**非安全隔离边界**）或 `docker` |
| `V3_DATABASE_ENGINE=postgis` + `PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD` | 启用 PostGIS；默认本地 SQLite |
| `V3_COG_PROXY` + `V3_COG_PROXYUSERPWD` | COG 分块下载走代理。**本机实测**：直连 us-west-2/Azure 仅 0.3–0.5 MB/s，经 `http://127.0.0.1:7897` 约 1.3 MB/s |
| `COG_READ_MODE=small_ranges` | 对大 Range 响应截断的网络，改用严格校验的 64 KiB 原始请求 |
| `COG_INGEST_ATTEMPTS`、`PC_CIRCUIT_FAILURES`、`PC_CIRCUIT_SECONDS`、`TITILER_*`、`MAPBOX_TILE_*` | 重试与熔断调参 |

**限流与生产**

`RATELIMIT_API_PER_MINUTE`（默认 120）、`RATELIMIT_AI_PER_MINUTE`（默认 30）、`RATELIMIT_BACKEND`（默认 `database`，多 worker 共享令牌桶）、`RATELIMIT_DISABLED=1`（演示关闭）、`TRUST_PROXY_HEADERS`；生产：`DJANGO_DEBUG`/`DJANGO_SECRET_KEY`/`DJANGO_ALLOWED_HOSTS`/`DJANGO_CSRF_TRUSTED_ORIGINS`/`CORS_ALLOW_ALL_ORIGINS`（见 `DEPLOY.md`）。

其余调参（`SENTINEL_*`、`AGENT_SENTINEL_*`、`AGENT_VISION_*`、`AGENT_EXECUTION_MODE`、`SENTINEL_ALLOW_CROSS_DATE_MOSAIC`、`PLANETARY_COMPUTER_*`、`MAPBOX_STATIC_BASE_URL`、`TITILER_ENDPOINT`）用 `grep -rn "environ" map_api` 查当前全集。

## Verification / Smoke Checks

`smoke_pipeline` 是项目级验收自检。默认路径**不调用任何外部付费/网络服务**（造本地 smoke 图 + mock Qwen 响应）。

| 命令 | 证明什么 | 外部依赖 |
|------|---------|---------|
| `smoke_pipeline` | 后端核心闭环可用 | 无 |
| `smoke_pipeline --agent` | 被 mock 的 Agent 循环（会话状态、选景、NDWI、VL、复核、历史落库） | 无 |
| `smoke_pipeline --contract-change` | 数据契约与两期变化检测 API | 无 |
| `--live-mapbox` | Mapbox 底图下载、进度轮询、落库、清理 | `MAPBOX_TOKEN` + 配额 |
| `--live-sentinel` | Sentinel-2 STAC 检索、候选打分、TiTiler 渲染、场景元数据 | Earth Search、TiTiler |
| `--live-sentinel1` | Sentinel-1 GRD SAR 检索、vv 线性拉伸渲染、`source=sentinel1` 落库 | Earth Search、TiTiler |
| `--live-landsat` | Landsat C2 L2（经 Planetary Computer 匿名 SAS）检索/渲染 | Planetary Computer |
| `--live-agent-grid` | Agent 的大范围 Sentinel 网格检索/拼接（耗时长） | Earth Search、TiTiler |
| `--live-ai` | 真实 DashScope 视觉调用、输出解析/兜底、报告链 | `DASHSCOPE_API_KEY` + 配额 |
| `--live-firms` | FIRMS 火点 CSV 工具链 | `FIRMS_MAP_KEY`（缺失→`skipped`） |
| `--live-esri` | Esri World Imagery 瓦片拼接下载 | Esri（无 key，限非营收+署名） |
| `--live-tianditu` | 天地图影像瓦片拼接下载 | `TIANDITU_KEY`（缺失→`skipped`） |

输出 JSON 带逐步 `ok`。默认会清掉 smoke 产物（`--keep-artifacts` 保留）。**live flag 失败而默认通过时，先当成外部 key/网络/配额/供应商问题，而不是本地代码故障。**

**2026-09-14 实测基线**（`.codex-runtime/venv`）：

```text
manage.py check                     → System check identified no issues (0 silenced)
manage.py makemigrations --check    → No changes detected
manage.py test map_api --noinput    → Ran 608 tests in 55.3s, OK
npm --prefix frontend test          → 24 passed (4 files)
npm --prefix frontend run typecheck → 无错误
manage.py smoke_pipeline            → 5 步 ok，0 步 failed
```

## API Routes

### V3（主力，`/api/v3/`，实现在 `map_api/v3/urls.py` + `api.py` + `asset_api.py`）

| Method | Path | 用途 |
|--------|------|------|
| GET/POST | `/api/v3/conversations/` | 建会话 / 列会话（`{items:[...]}`） |
| GET | `/api/v3/conversations/<uuid>/` | 会话详情：`{conversation, messages, attachments, observations, runs, artifacts}` |
| POST | `/api/v3/conversations/<uuid>/messages/` | 发消息 → `{message, run}`；支持 `delivery: steer\|queue` 与 `request_id` 幂等 |
| GET | `/api/v3/conversations/<uuid>/events/` | SSE 事件流（`?after=N` 游标续传；`?format=json` 取 `{items, cursor}`） |
| GET | `/api/v3/runs/<id>/` | 运行状态：`status`/`goal`/`current_action`/`plan`/`budget`/`usage` |
| POST | `/api/v3/runs/<id>/actions/` | `{action: stop\|resume\|retry\|extend_budget, request_id, budget?}` |
| GET | `/api/v3/observations/`、`/evidence/`、`/artifacts/` | 观察 / 证据 / 产物查询 |
| GET | `/api/v3/artifacts/<id>/download` | 下载产物 |
| GET | `/api/v3/capabilities/` | 模型角色、数据源、工具清单、`execution`（`local`/`docker`）与限制常量 |
| GET | `/api/v3/places/?q=` | 地名 → WGS84 坐标（**不是行政边界**） |
| POST | `/api/v3/uploads/` → `PUT /api/v3/uploads/<id>/chunks/<i>` → `POST /api/v3/uploads/<id>/complete` | 分块上传（单文件 ≤ 1 GiB，断点恢复） |
| POST | `/api/v3/attachments/` | 从地图 `bbox` 建附件（异步下载影像） |
| GET | `/api/v3/attachments/<id>/tiles/{z}/{x}/{y}.png` | 影像金字塔瓦片（`z=0` 最粗，256px） |
| POST | `/api/v3/attachments/<id>/windows/` | 按原始像素读窗口 → 观察 + 预览 |

### 页面

| Path | 内容 |
|------|------|
| `/` = `/workbench/` = `/agent/` = `/v3/` | V3 React 工作台（`workbench_v3.html`）；前端未构建时返回 `workbench_unbuilt.html` (503) |
| `/legacy/`、`/legacy/workbench/` | V2 legacy 落地页 / 工作台（`home.html`、`browser.html`） |
| `/design/` | SPECTRA 设计系统参考页（`design.html`，仅视觉规范） |
| `/admin/` | Django admin |

### V2 legacy API（`/api/*`，未列 `v3`/`v2` 前缀者）

`system/health/`、`system/dependencies/`、`system/dependencies/probe/`、`analysis/indices/`、`analysis/change/`、`analysis/runs/<id>/`、`satellite/get-img/`、`satellite/get-sentinel-img/`、`satellite/show-img/`、`satellite/progress/`、`satellite/cleanup/`、`imagery/search/`、`imagery/recommend-source/`、`imagery/scenes/`、`imagery/scenes/<id>/`、`agent/sessions/`（含 `/events/`、`/events/stream/`、`/transcript/`、`/messages/`）、`agent/runs/<id>/`、`ai/query-region/`、`ai/history/`、`geo/search/`、`report/generate/`、`report/download/`。以 `map_api/urls.py` 为准。

### V2 DAG API（`/api/v2/agent/runs/*`）

`runs/`、`runs/<id>/`、`runs/<id>/actions/`、`runs/<id>/replan/`、`runs/<id>/artifacts/`（+下载）、`runs/<id>/evidence/`、`runs/<id>/events/`（+`/stream/`）。

## Architecture

单 Django app `map_api`。三层前端：V3 是 React+Vite 构建产物；legacy 是三张服务端渲染页面（无构建）。

### V3 运行链路（当前主线）

```
用户消息 → POST /api/v3/conversations/<id>/messages/
  → 落库 ConversationMessage + 创建/复用 AgentRun（execution_engine="harness"）
  → run_v3_worker 抢占租约（LEASE_SECONDS=120，心跳续租）
  → harness.execute_run() 主循环：
       build_prompt(run, context)        # 系统提示 + 历史 + 工具结果摘要 + 图像
       → provider.tool_call(...)         # deepseek-flash 原生 tool calling（出错重试 2 次）
       → execute_call(...)               # 真正执行工具（独立工具可并行；mutation/发布串行）
       → 结果落库：RunToolCall / RunEvidence / SpatialObservation / RunArtifact
       → 循环直到模型调用 finish_answer
  → 前端通过 SSE（events）实时渲染进度
```

`map_api/v3/` 模块分工：

- `harness.py`（589L）— 主循环、租约、checkpoint、并行工具执行、预算与终态判定。
- `provider.py` — OpenAI 兼容 tool-calling 客户端（`controller`→`deepseek-flash`；视觉→`V3_VISION_MODEL` 或 `qwen3-vl-plus`）。
- `tools.py` — `Tool(name, description, schema, handler, timeout, recovery, parallel)` 契约 + `registry()`；工具分组 `imagery`/`context`/`analysis` 按需加载。
- `spatial_tools.py` — 模型面向的看图操作（`view_overview`、`read_image_window`、`annotate_observation`、`update_plan`、`review_visual`、`finish_answer`…）。
- `data_tools.py` / `data_adapter.py` — 数据产品（`spectral_index`、`sar_backscatter`、`dem_terrain`、`landsat_surface_temperature`、`retrieve_imagery`、`search_scenes`、`compare_two_date_change`、`external_evidence`）+ 数据源边界。
- `assets.py` / `asset_api.py` — 附件与瓦片金字塔（`process_attachment`、`adopt_file`）。
- `raster_products.py`（626L）— COG 摄取与流式栅格产品、按 profile 校准、QA 掩膜、指数。
- `change_analysis.py` — 两期变化（仅在共同有效像元上计算）。
- `range_reader.py` — 严格小 Range 读取（应对截断大 Range 的代理）。
- `local_python.py` / `sandbox.py` — `python_analysis` 的执行环境（local / docker）。
- `runtime.py` — 协作式时限：只在可信分块边界打断（`ToolInterrupted`）。
- `context_store.py` — 压缩工具结果进 prompt，保留可寻址完整记录（`read_saved_result`）。
- `answer_validation.py` — 按已保存测量核算 `finish_answer.numeric_claims`。
- `conversations.py` / `common.py` / `api.py` — 会话状态、steer/FIFO 队列、wire 格式与 HTTP 层。
- `methods.py` / `places.py` / `spatial_index.py` — 方法目录与覆盖统计、地名、空间检索。

### V3 前端（`frontend/src/`）

`main.tsx`（941L，界面骨架）、`Canvas.tsx`（695L，OpenLayers 地图+框选）、`Composer.tsx`（输入框）、`ImageCanvas.tsx`（影像查看/滑杆对比）、`api.ts`（后端调用）、`store.ts`（zustand 状态）、`DataEvidence.tsx`、`ToolActivity.tsx`、`PythonArtifacts.tsx`、`geo.ts`、`types.ts`。

构建产物落在 `static/v3/`（**gitignored**）；`templates/workbench_v3.html` 从 `static/v3/.vite/manifest.json` 解析哈希后的 JS/CSS。改 TS/CSS 后必须 `npm run build`，Django 不会读源码。

### V2 DAG 运行引擎（`map_api/run_*.py` + `map_api/agent/`）

持久化 DAG：`run_kernel.py`（状态转移/checkpoint）、`run_scheduler.py`（事务化 reducer，租约 fence 外执行）、`run_executor.py`（Sentinel DAG handlers）、`run_journal.py`（版本化 journal + 不可变 checkpoint，同事务提交）、`run_api.py`（只读事件传输，从已提交游标恢复）、`run_products.py`（按 attempt 不可变的运行产物）、`run_acceptance.py`（缺 telemetry 记为 unknown，绝不记为成功）。

`map_api/agent/`：`loop.py`（1074L，模型驱动工具循环）、`decision.py`（严格决策边界，不用模型散文修补业务字段）、`providers.py`、`registry.py`、`durable_tools.py`（只重放已提交的成功调用）、`events.py`、`waiting.py`（HITL 等待/恢复）、`tools.py`。

### V2 legacy 路径

- `views.py`（2471L）— HTTP 层、AI 分析管线、Word 报告。移动出去的域函数按原名再导出，`from map_api.views import X` 与 `patch("map_api.views.X")` 继续可用。
- `orchestrator.py`（656L）— `RemoteSensingAgent` 会话编排（槽位 → 定位 → 选源 → 检索 → 质量门禁 → NDWI → VL → DeepSeek 复核）。可 patch 的名字（`resolve_district_bbox`、`compute_ndwi_*`、`call_deepseek`、`ai_query_region`、`generate_report`）经 `views` 模块**运行时查找**。
- `sentinel_pipeline.py`（827L）— Sentinel-2 选景、渲染回退链、多景马赛克、no-data 裁边、缓存、场景落库。
- `payloads.py`（626L）、`geo_math.py`（286L，零 Django 依赖）、`media_paths.py`（29L，`SAVE_DIR`/`REPORT_DIR` + `safe_media_path()` 唯一定义）。
- `middleware.py` — `RateLimitMiddleware`（按 IP 滑动窗口，保护付费端点）。
- `imagery_sources/` — provider 注册表（`get_provider`）：`mapbox.py`、`esri.py`、`tianditu.py`、`earth_search.py`、`planetary_computer.py`。
- `utils/` — `get_satellite_image.py`、`image_preprocessor.py`、`smart_query_analyzer.py`、`active_perception.py`、`analysis_strategy.py`、`clip_retriever.py`、`external_data.py`（FIRMS/Overpass/Open-Meteo/GSW/WorldCover）、`service_health.py`（跨进程熔断）、`http.py`（`request_proxies` 全项目唯一实现）。

### 影像策略

任务自适应双源：Mapbox 等高清底图（建筑/道路/设施/小目标，**无拍摄时间与传感器 GSD**）vs Sentinel-2 L2A 等公开可溯源影像（宏观地类、水体、植被、农业、变化筛查）。`/api/imagery/recommend-source/` 按问题推荐源。Sentinel 选景单景优先，覆盖不足时同日优先的贪心覆盖马赛克（跨日期需 `SENTINEL_ALLOW_CROSS_DATE_MOSAIC=1` 显式开启）——这是**覆盖拼接，不是辐射一致的月度合成**。

### 存储

- `media/v3-assets/`（`files`/`uploads`/`derived`/`previews`/`observations`）— V3 附件、派生瓦片与观察预览。**会持续增长，当前没有命令清理它**（2026-09-14 实测已 2.3 GB）。
- `media/satellite_imgs/`、`media/reports/` — legacy 影像与 `.docx`；由 `cleanup_media` / `POST /api/satellite/cleanup/` 清理。
- `media/v3/` — V3 运行时工作区（同样不被 `cleanup_media` 覆盖）：`<conversation_id>/` 存该会话的数据产品/空间索引缓存（JSON），`sandbox/run-<id>-<hash>/` 存 Python 分析任务目录（代码、输入、产物）。
- `db.sqlite3` 为默认库（WAL 经 `connection_created` 信号开启）。日志：`media/logs/app.log`、`media/logs/ai_calls.log`（轮转 10MB×5）。

## Constraints (do not break)

- **响应约定分代，别混用**：V3 成功返回**裸对象**（列表用 `{items:[...]}`），错误返回 `{"error":{"code","message"}}` + 语义化 HTTP 码；V2/legacy 返回 `{code, msg, data}`，前端按 `code` 分支。加端点时按所属代的约定写。
- **V3 所有权隔离**：V3 资源按 Django session key（`owner_session_key`）隔离，跨 owner 一律 404。新增 V3 端点必须走 `common.owner()` / `owned_conversation()`。
- **模块再导出契约**：`views.py` 按原名再导出搬走的域函数；不要删除，且 `orchestrator.py` 里可 patch 的名字必须经 `views` 模块运行时查找。
- **CORS 顺序**：`corsheaders.middleware.CorsMiddleware` 必须在 `SessionMiddleware` 与 `CommonMiddleware` 之间，`map_api.middleware.RateLimitMiddleware` 紧随 CORS（保证 429 也带 CORS 头）。
- **代理绕过**：出站 `requests` 统一走 `utils/http.py` 的 `request_proxies`（`{"http": None, "https": None}`）绕过系统 VPN；去掉会破坏下载。V3 的 `V3_COG_PROXY` 是另一条独立通道（COG 分块）。
- **VL-only kwarg**：`vl_high_resolution_images=True` 只对 `qwen3-vl-*` / `qwen-vl-*` 发送，不要给 unified 模型。
- **安全边界**：`safe_media_path()` 是所有用户提供文件名的唯一入口（legacy 路径）；V3 附件走 `assets._inside`（已处理 Windows `\\?\` 前缀，两侧 normcase）。路径比较必须两侧规范化。
- **工具数值只能来自真实栅格**：模型提供的数组不能当像素；原始栅格 + 有效掩膜 + 校准元数据才可用于数值结论；计数必须区分**检出/估计/完整统计**。
- **不伪造进度**：`observer`/`plan` 只反映真实事件，不得把未执行步骤推定为完成，不得暴露私有推理链。
- `start.py` 依赖已构建的 `static/v3/`；`manage.py runserver` 用于热重载开发。生产配置必须由环境变量提供（见 `DEPLOY.md`）。

## Known limitations (documented, not bugs)

- 几何假设局部平坦、非极地，且不处理跨 180° 经线的 bbox；legacy 工作台地图限制在中国范围（`maxBounds [[-10,70],[65,140]]`）。
- Sentinel-2 马赛克是**覆盖拼接**：逐景日期/云量/色彩差异仍在（写入场景 `limitations`）；跨日期马赛克默认拒绝。
- 非光学源结论受限：SAR 需辐射校准门禁、VL 解译可靠性低（限水体/淹没/宏观地物）；Cop-DEM 为静态高程（采集基线 2011–2015），只作地形参考。
- 参考级底图（Mapbox/Esri/天地图）无拍摄时间与传感器 GSD，不进入物理测量语义。
- 固定阈值 NDWI 只作线索筛查，不等于水体制图；`SCL=11` 只能称雪/冰类。
- 无地理变换的图像只能描述左/右/上/下，不能当作东南西北。
- V3 预算默认：单任务 120 分钟、128 次主控调用、128 次视觉调用、≤4 空间工具并发、2 个 Python 任务并发；耗尽保留 checkpoint，追加预算可继续。
- **已修（2026-09-14，随本轮文档同步）**：① `.github/workflows/ci.yml` 改为安装 `requirements-v3.txt`——此前只装 `requirements.txt`，缺 rasterio，`test_v3_*` 在测试发现阶段即 `ModuleNotFoundError`、整轮 `FAILED (errors=1)`（已用缺该包的 conda 环境实测复现）；② 新增 `deploy/satellitesense-v3-worker.service`——此前 `deploy/` 只有 V2 的 worker unit，生产的 `/` 提问不会被任何进程消费。两个 unit 必须都启用（见 `DEPLOY.md` §8）。
- **已知待办**：`media/v3-assets`（2026-09-14 实测 2.3 GB）只增不减，且 `cleanup_media` 不覆盖它，无对应清理命令。
