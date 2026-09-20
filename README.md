# SatelliteSense · 大场景遥感图像智能问答

面向非专业用户的遥感影像 Agent。地图框选、PNG/JPEG 和 GeoTIFF 进入同一段对话；主控按问题查看原图窗口、调用数据产品、标注发现并交付可点击证据。工作台采用深石墨色、暖金强调，以及可调整的聊天与影像画布。

## 本地启动

当前项目隔离环境位于 `.codex-runtime/venv/`，无需修改全局 Python。首次配置先安装 `requirements-v3.txt`，前端使用 Node 和 npm：

```powershell
.\.codex-runtime\venv\Scripts\python.exe -m pip install -r requirements-v3.txt
npm --prefix frontend ci
npm --prefix frontend run build
.\.codex-runtime\venv\Scripts\python.exe manage.py migrate
.\.codex-runtime\venv\Scripts\python.exe start.py
```

`start.py` 同时运行 Django 和持久化 V3 worker；打开 `http://127.0.0.1:8000/`。可用 `--port 8013 --no-browser` 指定端口。Ctrl+C 收尾子进程；重启后 worker 接管过期租约。启动器只检查待执行迁移，不会自动更改已有数据库。

开发前端时运行 `npm --prefix frontend run dev`，Vite 将 `/api` 代理到本地 Django。Django 入口使用已构建的 `static/v3/`，修改 TS/CSS 后须重新构建。新工作台 `/`、`/workbench/`、`/agent/` 相同；旧页面保留在 `/legacy/` 和 `/legacy/workbench/`。

## 运行配置

应用通过既有环境加载器读取配置；不要将实际密钥写入源码、报告或命令日志。

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 主控 `deepseek-flash`，原生工具调用和图像输入 |
| `DASHSCOPE_API_KEY` | 视觉复核；基线 `qwen3-vl-plus` |
| `V3_VISION_MODEL` | 经过评测后覆盖视觉角色配置 |
| `DJANGO_DEBUG` | 本地开发设为 `1`；关闭时启动器先 collectstatic |
| `COG_READ_MODE=small_ranges` | 对大 Range 响应截断的网络，直接使用严格校验的 64 KiB 原始数据请求 |
| `V3_COG_PROXY` | COG 分块下载（rasterio 与 range_reader）走指定 HTTP 代理（如 `http://127.0.0.1:7897`），可选 `V3_COG_PROXYUSERPWD` 提供代理认证；默认直连 |
| `V3_PYTHON_RUNTIME=local` | Python 分析执行环境；可显式设为 `docker` |
| `V3_DATABASE_ENGINE=postgis` | 启用 PostgreSQL/PostGIS，默认本地 SQLite |
| `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSWORD` | PostgreSQL 连接配置 |

`GET /api/v3/capabilities` 返回模型配置、数据工具和当前执行环境状态；默认 `execution` 为 `local`，同时保留兼容的 `sandbox` 字段。注册数据源不等于服务在线，也不等于当前影像满足质量要求。

## 数据与执行

- 输入支持分块恢复上传，单文件上限 1 GiB。保留原始字节和 SHA-256，后台生成分块 TIFF、预览与金字塔；浏览器只请求视口瓦片。
- 对话按 owner 隔离，消息、空间附件、观察、运行、证据和产物均可追溯。SSE 支持游标重放；中途补充和 FIFO 队列持久化。
- 主控自主选择工具；按需加载影像、背景证据和分析能力。概览、原始窗口、位置标注、覆盖检查与引用验证形成空间问答链。
- 默认每任务 120 分钟、128 次主控、128 次视觉调用，最多 4 个空间工具和 2 个 Python 任务并发。预算耗尽保留检查点，追加预算后继续。
- 已实现 Sentinel-2/Landsat 光谱指数、Landsat Level-2 ST_B10、DEM 地形、SAR 校准门禁、两期指数差异、同日同源拼接，以及水体/地类/道路/火点/气象背景查询。
- 大图数值处理逐块执行，原始比例与显示缩放分离。COG 读取失败不会用缩略图伪装数值产品。变化仅在共同有效像元上计算，输出候选与覆盖限制。
- 工具在分块边界检查时限和租约。底层网络调用另有限时；取消不承诺立即终止已经发出的远程请求。Python 分析默认由项目本地 Python 子进程执行，单次默认 120 秒，结果继续记录代码、输入和产物哈希。
- 附件原始文件、派生瓦片与观察预览落在 `media/v3-assets/`，**只会增长，目前没有对应的清理命令**（2026-09-14 实测 2.3 GB）。旧链路的 `media/satellite_imgs` 与 `media/reports` 由 `manage.py cleanup_media` 清理。

## 本地 Python、Docker 与 PostGIS

Docker 不是启动工作台、聊天、地图、影像处理、数据产品或 Python 分析的前提。默认 `V3_PYTHON_RUNTIME=local`，使用项目本地 Python 子进程，继承本机能够访问的网络与文件权限；Docker 只在需要本地 PostGIS 服务或显式设置 `V3_PYTHON_RUNTIME=docker` 使用容器化分析时使用。

可选的 Docker/PostGIS 环境：

```powershell
docker compose -f docker/compose.v3.yml up -d database
docker compose -f docker/compose.v3.yml --profile build build analysis-image
```

本地 compose 的数据库仅绑定 `127.0.0.1:5433`，使用开发 trust 认证；不能把该配置直接用于生产。首次启用设置 `PGPORT=5433` 和 `V3_DATABASE_ENGINE=postgis` 后执行迁移。迁移 0027 创建空间列和 GiST 索引。现有 SQLite 不会被自动导入 PostgreSQL；未启用 PostGIS 时，默认 SQLite 路径仍可运行完整本地工作台。

可选分析镜像使用禁网、只读、CPU/内存/进程限制，并仅挂载已授权输入。它用于可复现的容器化分析，不决定本地功能是否可用。无论执行环境如何，代码、输入引用和产物哈希都会随运行保存。

## 历史迁移

先备份，再进行显式迁移：

```powershell
.\.codex-runtime\venv\Scripts\python.exe manage.py backup_v3_history
.\.codex-runtime\venv\Scripts\python.exe manage.py migrate_v3_history
.\.codex-runtime\venv\Scripts\python.exe manage.py migrate_v3_history --apply
```

仅采用已有数据库关联；不按相似文本或文件名猜测合并。原记录和原文件保留，迁移映射幂等。具体备份核对、未归属记录和回切说明见 [实施记录](docs/V3_IMPLEMENTATION.md)。

## 验证

```powershell
.\.codex-runtime\venv\Scripts\python.exe manage.py check
.\.codex-runtime\venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.codex-runtime\venv\Scripts\python.exe manage.py test map_api --noinput
npm --prefix frontend test
npm --prefix frontend run build
.\.codex-runtime\venv\Scripts\python.exe scripts/verify_v3_interactions.py
.\.codex-runtime\venv\Scripts\python.exe -X utf8 scripts/verify_local_agent.py
.\.codex-runtime\venv\Scripts\python.exe scripts/validate_real_v3_products.py
```

浏览器验收需要本地 8000 工作台与 worker；脚本检查真实瓦片内容、框选、滑杆、刷新、IME 与窄屏，失败以非零退出。真实产品验收联网读取原始公开 COG，会产生真实网络耗时。

**2026-09-14 实测基线**（`.codex-runtime/venv`）：`manage.py check` 无问题；`makemigrations --check` No changes detected；后端 `test map_api` → **608 tests OK**（55s）；前端 `npm test` → **24 passed**，`npm run typecheck` 无错误；`smoke_pipeline` → 5 步 ok、0 步 failed。

实现细节（模块分工、API 路由全表、环境变量全集、约束与已知限制）见 [CLAUDE.md](CLAUDE.md)；协作约定与前端基线见 [AGENTS.md](AGENTS.md)。

`verify_local_agent.py` 使用真实模型读取港区影像并执行本地 Python，生成统计 JSON 与图表；独立核对原图像元均值、证据引用和产物哈希。它会调用已配置的模型服务并保存测试会话。结果保存在 `output/ui-review/v3-local-agent-results.json`。

模型评测参见 [评测说明](evaluation/v3/README.md)。已存在的 120 题 direct-vision 结果不等于新旧 harness 对照，也不包含定位或多轮真值；未通过晋升条件时保留当前视觉基线。真人可用性测试与生产部署另有明确验收边界，不将代理测试冒充真人结果。

## 工程结构

```text
frontend/src/                 React 19 / TypeScript / OpenLayers 工作台
map_api/v3/                   会话 API、harness、工具、原始影像、沙箱
map_api/management/commands/   worker、迁移、备份与评测命令
evaluation/v3/                公开题库、来源、评分与逐题结果
docker/                       PostGIS 本地环境与分析镜像
scripts/                      浏览器和真实数据验收
static/v3/                    Vite 构建产物
```

旧工作台及服务器维护资料完整保留于 [Legacy 文档](docs/LEGACY_WORKBENCH.md)。[DEPLOY.md](DEPLOY.md) 中的旧服务配置须经过 V3 候选部署验收后再切换；本次未进行生产部署。
