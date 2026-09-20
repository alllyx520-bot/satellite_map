# 大场景问答 V3 实施记录

## 授权和边界

2026-09-12 用户批准完整计划。保留既有未提交改动；不执行 commit、push 或生产部署。模型基线为 deepseek-flash + qwen3-vl-plus；新视觉候选通过真实盲测再晋升。用户研究不得用代理或合成评分冒充。

## 集成约定

- Django 同源 `/api/v3/`，响应统一为对象；列表使用 `items`。错误 `{error: {code, message}}`。
- Conversation: `id` UUID, `title`, `created_at`, `updated_at`, `active_run`, `event_sequence`。
- Message: `id` UUID, `role`, `content` 文本, `parts` 数组, `status`, `delivery` steer/queue, `run_id`, `created_at`。
- Attachment: `id` UUID, `name`, `status`, `kind`, `coordinate_space` image_pixels/geographic, `width`, `height`, `bbox`, `geometry`, `crs`, `transform`, `metadata`, `preview_url`, `tile_url`, `scene_id`。无可靠地理信息时 bbox 为 null。
- Conversation detail: `{conversation, messages, attachments, observations, runs, artifacts}`。
- POST conversations `{title?}`；POST conversations/id/messages `{content, attachment_ids, references?, delivery?, request_id}` 返回 `{message, run}`。
- GET conversations/id/events?after=N 返回事件 SSE；`?format=json` 返回 `{items, cursor}`；事件 `{sequence,type,payload,created_at}`；类型 message.created/message.updated/run.updated/tool.started/tool.completed/observation.created/attachment.ready。
- Run: 数字 `id`, `status`, `goal`, `model`, `current_action`, `plan`, `budget`, `usage`, `error`；POST runs/id/actions `{action: stop|resume|retry|extend_budget, request_id, budget?}`。
- POST uploads `{name,size_bytes,conversation_id?}` 返回 `{id,chunk_size,received_chunks}`；PUT uploads/id/chunks/index 原始二进制；POST uploads/id/complete 返回 `{attachment}`；GET uploads/id 返回进度。
- POST attachments `{name,bbox,geometry?,source?,conversation_id?}` 从地图区域创建附件，返回 `{attachment}`。源默认 esri；下载可异步，消息接受 processing 附件并等待。
- 附件瓦片 `/api/v3/attachments/id/tiles/{z}/{x}/{y}.png`，图像金字塔 z=0 最粗，tile_size=256；metadata.tile_max_zoom，metadata.resolutions 给出 OpenLayers 像素投影所需尺度。
- POST attachments/id/windows `{x,y,width,height,max_size?}` 返回窗口观察与预览；几何以原始图像左上为原点，y 向下。
- Observation: `id` UUID, `attachment_id`, `label`, `kind`, `geometry`（像素 GeoJSON）, `window` [x,y,w,h], `summary`, `confidence`, `evidence_refs`, `preview_url`, `metadata`。
- API /capabilities 给出数据源、模型角色和当前执行环境状态；默认 `execution` 为 `{runtime: 'local', available: true, isolated: false, label: '本地执行'}`，并保留兼容的 `sandbox` 字段。不能把目录注册当作在线可用证明。

## 模块所有权

- Root：models/migrations、v3 会话 API、harness、worker、沙箱、配置、迁移、评测与集成。
- Frontend agent：frontend/ 工程与模板 workbench_v3.html；只使用以上 API。
- Asset agent：map_api/v3/assets.py、asset_api.py、空间计算与大图测试；不编辑 models.py 或全局 routes/settings。
- Data agent：数据产品、指数、跨瓦片证据，以及独立数据产品测试；不编辑 v3 会话和前端。

## 工具实现约定

`map_api/v3/tools.py` 的 `Tool(name, description, schema, handler, timeout=120, recovery='replay_safe', parallel=False)`；handler 接收 `(args, ctx)`，ctx 包括 `run`（AgentRun）、`attachment_ids`（当前已发送附件字符串 ID 列表）、`version`、`call_key`。返回 JSON 可序列化事实；不要将模型提供的数组当真实像素。空间图像通过返回 `image_refs`（SpatialObservation ID）交给主控。数据模块在 `data_adapter.py` 提供 `registered_tools(Tool, schema)` 返回工具列表；引用存在性由工具验证。

## 验收记录

此文档在实施过程中记录实际命令、成功、失败和未满足的外部条件。未执行的验证不得写为通过。

## 历史迁移与回切

- 先在维护窗口执行 `python manage.py migrate_v3_history`，只输出候选数量和未归属/未关联记录数量；它不会写入任何数据。
- 在执行 `--apply` 前，备份数据库和 `media/satellite_imgs/`；迁移会复制而不移动明确由 `ChatHistory.image_file` 关联的文件，并记录原始 SHA-256。`AgentSession.history`、`ChatHistory.scene` 和 `AgentSession.scene` 是唯一允许使用的关联，绝不根据文本、名称或相似内容猜测合并。
- `python manage.py migrate_v3_history --apply` 会输出 sessions、conversations、messages、attachments、owner conflicts 和迁移后候选数；核对这些计数与 dry-run 后再开放 V3。重复执行通过 `LegacyConversationLink` 保持幂等。
- 回切时停止 V3 worker，恢复迁移前的数据库备份，并保留新复制的 `media/v3-assets/files/` 供审计；原始 legacy 行和 `media/satellite_imgs/` 从不被该命令修改或删除。

## 本地执行与可选 Docker 环境

V3 默认 `V3_PYTHON_RUNTIME=local`，以项目本地 Python 子进程执行分析，因此 Docker 不可用不会阻塞聊天、地图、影像读取、数据产品或 Python 分析。分析调用获得已授权输入、独立输出目录和项目目录；单次默认 120 秒，可取消。执行记录保存代码、输入引用、环境信息和产物哈希，供后续核对与复现。

`docker build -t satellitesense-analysis:local docker/analysis` 与 `docker compose -f docker/compose.v3.yml up -d database` 是可选的本地容器化分析和 PostGIS 开发环境。显式设为 `V3_PYTHON_RUNTIME=docker` 时，容器分析镜像采用禁网、只读、CPU/内存/进程限制，并只挂载已授权输入；未设置该模式时系统继续使用本地执行路径。本地执行继承运行账户的权限，并非安全隔离边界。

## 2026-09-13 本地执行验收

用户明确要求停止处理 Docker，开发阶段直接本地执行，并且不因缺少沙箱禁用 Python 能力。此决定取代原计划中“只能在独立容器执行模型代码”的开发环境要求；数据库继续使用本地 SQLite，无数据库切换。

- 本地代码使用项目 `sys.executable`；提供 `inputs`、`output_dir`、`project_dir`，允许联网、读写本地文件和创建子进程，无逐次批准界面。
- 保留独立任务目录、真实 stdout/stderr、超时及取消、代码/输入/产物 SHA-256 与 Python 包版本。子进程失败可由主控读取错误后调整代码。
- 成功 Python 调用创建 `code_execution` 证据并关联产物；最终回答可以使用返回的真实 `evidence_ids`。这证明代码实际执行及其输入来源，不自动证明任意算法具有遥感量算精度。
- 当前执行环境通过 `/api/v3/capabilities` 的 `execution` 返回，旧 `sandbox` 字段兼容保留；界面显示“本地执行”。
- 后端全量 557 项测试通过；随后针对 Python 证据接入、运行恢复和空间引用修改的专项回归通过。前端类型检查、构建及 12 项测试通过。
- 真实模型调用读取 884×1024 港区影像，生成 JSON 与 PNG，像元均值与独立读取结果一致，文件下载 SHA-256 一致，回答引用已保存的 Python 证据。记录：`output/ui-review/v3-local-agent-results.json`。
- 浏览器再次通过地图真实拖拽框选、双影像上传、原图 ROI、同步对比/滑杆、中文输入法、刷新恢复与窄屏检查，无浏览器错误。

Docker 未重装，用户曾自行重置。目录重定向试验仍未通过 Docker 实际启动；撤回试验的命令被自动审批规则拒绝（仅返回 `blocked by policy`），没有执行。两处 junction、原目录备份及诊断记录保留，位置见 `output/docker-runtime-recovery.json`。遵照用户后续指令停止继续修改 Docker。

完整原计划中的模型候选晋升、全部长任务及真人可用性研究尚未完成；既有 direct-vision 结果不能当作 harness 架构对照。Docker/PostGIS 真实容器验收已按用户要求暂停。

## 2026-09-14 稳定性与真实问答纠错验收

Run 34（哈尔滨春季水体）审计发现的六类问题已全部修复并在真实问答中回归验证；后端 607 项测试、前端 24 项测试与类型检查全部通过。

- 数据契约：`bbox` 必填 `min_lng/min_lat/max_lng/max_lat` 并带示例与按产品日期约束；SchemaValidationError→`invalid_arguments` 附字段细节、KeyError→`missing_field`；按工具名连续失败升级提示。
- 断点续传：`retrieve_imagery` 超时分块边界保留进度（sidecar 于 `v3-assets/files/.partial/`），返回 `ingest_interrupted`（retryable），同参数再调即续传，完成后与一次性 ingest 逐字节一致；harness 的 no_progress 对该错误码放行。单景外接矩形不覆盖 AOI 时在下载前拒绝并附场景范围（Run 51 曾因此两次白下 14+ 分钟）。
- 上下文：`context_store` 按引用读取完整工具结果（`read_saved_result` 支持 path 与分页，定位失败附当前层可用键/索引），证据摘要+截断上限进入 prompt。
- 数值核算：`finish_answer.numeric_claims` 按已保存测量核算（同 unit 同 scope_id），不符打回；真实运行中多次拦截并促使模型改对。
- Python 资产链：`import_python_output` 把产物注册为真实附件+全图观察（根目录不一致已修，经 `adopt_file` 复制入 v3-assets）；中文图表字体已配置并实测无方框。
- 并行窗口间歇失败根因：Windows 并发 `resolve()` 返回 `\\?\` 前缀，`_inside` 已规范化（50/50 循环通过）。
- COG 传输：本机直连 us-west-2/Azure 仅 0.3–0.5 MB/s，`V3_COG_PROXY` 可为 ingest 与 range_reader 指定代理（实测约 1.3 MB/s）。
- 真实回归（DeepSeek+Qwen 在线）：Run 47 哈尔滨 3 附件/2 观察/8 证据，18 数字全部溯源；Run 48 上传影像 Python 链路+中文图表；Run 52 武汉东湖两期变化 11 数字全部溯源；Run 53 北京概述较修复前 -78% 耗时；Run 51/54 上海多轮追问。报告见 `output/v3-cases/phase-b-*.md`。

## 2026-09-14 文档同步与基线核对

对全部项目文档做了一次与代码的一致性核对，并重跑基线。**本节只记录核对结果，未改动代码。**

实测（`.codex-runtime/venv`，Python 3.11.15 / Django 5.2.13 / rasterio 1.4.4）：

```text
manage.py check                     → System check identified no issues (0 silenced)
manage.py makemigrations --check    → No changes detected（迁移至 0027）
manage.py test map_api --noinput    → Ran 608 tests in 55.3s, OK
npm --prefix frontend test          → 24 passed (4 files)
npm --prefix frontend run typecheck → 无错误
manage.py smoke_pipeline            → 5 步 ok，0 步 failed
```

相对此前记录的修正：后端测试数 607 → **608**（历史文档里 557/607/608 混用，此后统一以本表为准）；
conda `general` 环境**缺 `rasterio`/`pyproj`/`psycopg`**，已确认不能运行 V3 代码与 V3 测试，
文档统一改用项目隔离环境 `.codex-runtime/venv/`。

文档侧更新：`CLAUDE.md` 按"三代实现并存"（V3 主力 / V2 DAG / V2 legacy）重写，补全 `/api/v3/*`
路由表、V3 模块分工、V3 环境变量与限制常量；`DEPLOY.md` 补 V3 增量（前端构建、`requirements-v3.txt`、
V3 worker systemd unit、`/` 与 `/api/v3/capabilities/` 验收、`media/v3-assets` 容量）；
`README.md`、`AGENTS.md` 补实测基线与运行环境；历史类文档（`AGENT_HARNESS_SOURCE_REVIEW.md`、
`AGENT_RUN_IMPLEMENTATION_STATUS.md`、`DATA_PIPELINE_AUDIT.md`、`REVIEW_PLAN.md`、
`REVIEW_FINDINGS.md`、`LEGACY_WORKBENCH.md`）标注为时点记录并指向当前入口。

核对中发现三项不一致，**前两项已当场修复**，第三项记录为待办：

1. **已修** — CI 依赖不全。`.github/workflows/ci.yml` 原本只安装 `requirements.txt`，缺 `rasterio`；
   而 `map_api/test_v3_*.py` 在模块层 `import rasterio`，测试发现阶段即报
   `ModuleNotFoundError: No module named 'rasterio'`、整轮 `FAILED (errors=1)`
   （已用缺该包的 conda 环境实测复现）。**修复**：CI 改为 `pip install -r requirements-v3.txt`
   （= `requirements.txt` + rasterio/pyproj/shapely/scipy/psycopg）。**验证**：YAML 解析通过、步骤序列正确；
   本地在装有该依赖集的环境下 `test map_api` 608 项 OK；并已确认 CI 目标平台（cp311 + linux x86_64）
   下这五个依赖都有**预编译 wheel**（`pip download --only-binary` 实测可下载：rasterio 1.4.4 /
   pyproj 3.7.2 为 `manylinux_2_28`，shapely 2.1.2 / psycopg-binary 3.3.5 为 `manylinux2014`，
   scipy 1.17.1 为 `manylinux_2_27/2_28`），因此在 ubuntu-latest 上不会退化成源码编译。
   **仍未在真实 Actions 上跑过**——推送后以首次 CI 结果为准。
2. **已修** — 生产缺 V3 worker。`deploy/satellitesense-agent-worker.service` 执行的是 V2 的
   `run_agent_worker`（只认 `ReportJob`/`DownloadTask`/`AgentSession` 及 `execution_engine="dag"`
   的 AgentRun），而 `/api/v3/` 创建的 `AgentRun` 是 `execution_engine="harness"`，
   **只由 `run_v3_worker` 消费**，仓库内原本没有对应 unit → 按现状部署时 `/` 的提问永远不会被执行。
   **修复**：新增 `deploy/satellitesense-v3-worker.service`，`DEPLOY.md` §8 已改为三 unit 一起安装。
   **验证**：`manage.py run_v3_worker --help` 可正常解析、ExecStart 路径风格与既有 unit 一致；
   **未在真实 systemd 上启用过**（本机为 Windows），首次部署按 §10 的"运行一直停在 queued"
   检查项确认。
3. **待办** — `media/v3-assets/` 只增不减（开发机 2026-09-14 实测 2.3 GB），无对应清理命令。
