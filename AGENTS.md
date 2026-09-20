# SatelliteSense 项目协作规则

## 运行环境与验证基线（2026-09-14 实测）

- **Python 用项目隔离环境 `.codex-runtime/venv/Scripts/python.exe`**（3.11.15 / Django 5.2.13 / rasterio 1.4.4）。conda `general` 缺 `rasterio`/`pyproj`/`psycopg`，**跑不了 V3 代码和 V3 测试**；本文档以下所有命令均以该 venv 为准。
- **改前端必须重建**：Django 只服务 `static/v3/`（gitignored 构建产物），`start.py` 会在缺 `static/v3/.vite/manifest.json` 时直接退出。`npm --prefix frontend run build` 之前改 TS/CSS 不会生效。
- 当前基线：`manage.py check` 无问题；`makemigrations --check` No changes detected；后端 **608 tests OK**（55s）；前端 24 tests + typecheck 通过；`smoke_pipeline` 5 步 ok / 0 失败。
- 2026-09-14 已修：`.github/workflows/ci.yml` 改为安装 `requirements-v3.txt`（此前只装 `requirements.txt`，缺 rasterio 导致 V3 测试收集即失败）；新增 `deploy/satellitesense-v3-worker.service`（V3 队列只由 `run_v3_worker` 消费，两个 worker unit 必须都启用）。CI 尚未在真实 Actions 上验证过。
- 仍待办：`media/v3-assets` 约 2.3 GB，只增不减且无清理命令。

## 前端基线

- 2026-09-12 用户明确授权完整重构为“大场景遥感图像智能问答”Agent：采用 React/TypeScript、深石墨色与少量暖金、统一聊天和可伸缩影像画布。此最新要求取代下列 2026-09-09 的布局与旧组件外观限制；原有功能、数据和历史应迁移保留。

- 用户于 2026-09-09 明确要求保留服务器当前部署版本的前端风格和大致结构；此要求优先于旧目标文档中的 Apple 风格重设计方案。
- 基线为现有 `templates/home.html`、`templates/browser.html` 及 `static/spectra.css`、`static/home.css`、`static/browser.css`：深色材质、金色点缀、原有字体层级与控件样式。
- 保留首页及地图工作台的基本结构：顶部工具栏、中央地图、左侧调查 Agent、右侧区域工作区。新增运行状态、计划、证据和控制功能应融入现有面板。
- `/agent/` 与 `/workbench/` 复用地图工作台，不另起浅色三栏页面，不用换主题代替功能集成。
- 前端修改需检查实际渲染；线上基线有变化时重新核对。不得因后端重构顺带重做视觉设计。

## 前端迭代环境坑（2026-09-09 实测）

- 本地 runserver 默认 `DJANGO_DEBUG=false`：模板被进程缓存、`/static/` 由 `staticfiles/`（STATIC_ROOT）提供。改模板必须重启进程，改 CSS/JS 必须 `manage.py collectstatic`，否则页面不更新。
- 纯 UI 截图迭代用 `DJANGO_DEBUG=1` 启动（模板与 static/ 实时生效），交付前再按 README 校验命令复核。
- **切勿加 `--noreload`**（2026-09-10 实测定位）：Django 5.2 的 `Engine` 对默认模板加载器无条件套 `cached.Loader`（`django/template/engine.py:41`），模板热更完全依赖 autoreload 监听模板目录后 `reset_loaders()`；`--noreload` 关掉 autoreload 后模板在整个进程生命周期内冻结（static/ 不受影响，仍实时）。另注意 Windows 允许 SO_REUSEADDR 双绑 8000 端口——重复启动 runserver 不会报错，新旧两个进程轮流接请求，表现为"改了不生效"的灵异现象；启动前先确认旧进程已杀净（`netstat -ano | grep ":8000 "` 只应有一条 LISTENING）。
- 截图工具：`scripts/shot_ui.py <tag> [home,workbench,design]`（Python Playwright + msedge，输出到 `output/ui-review/`）。

## 数据链路与运行环境坑（2026-09-14 实测）

- **COG 直连极慢**：本机直连 `sentinel-cogs.s3.us-west-2` 与 Azure blob 吞吐仅 0.3–0.5 MB/s（rasterio 小块读低至 0.01 MB/s，且大 Range 会被截断报错）；经 `http://127.0.0.1:7897` 代理约 1.3 MB/s。大城市 AOI 的多波段 `retrieve_imagery` 因此必然超过单次工具时限。启动服务时设 `V3_COG_PROXY=http://127.0.0.1:7897`（ingest 的 rasterio Env 与 range_reader 读取走该代理，默认直连）。
- **retrieve_imagery 已支持断点续传**：超时返回 `ingest_interrupted`（retryable），同参数再次调用即续传，进度 sidecar 在 `v3-assets/files/.partial/`；harness 的 no_progress 拦截对该错误码放行。续传完成后有一次本地确定性重写，保证交付文件与一次性 ingest 逐字节一致。
- **答案数值核算**：`finish_answer.numeric_claims` 会按 evidence 中保存的测量值（value/percentage/difference，同 unit 同 scope_id）核算，不符即 `answer_validation_failed` 打回；模型须改数值后重新交付。
- **Windows 路径并发坑**：`Path.resolve()` 在父目录正被并发 `mkdir` 时间歇返回 `\\?\` 扩展前缀路径，`assets._inside` 必须两侧 normcase+剥前缀再比较，否则误判"附件路径无效"（曾是并行窗口测试 12 vs 11 的根因）。
- **DeepSeek 402 是外部余额/窗口问题**：run 会以 `external_service_unavailable` 终态落库，不是应用 bug；恢复后重新提问即可。
