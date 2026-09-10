# AgentRun 重构验收状态

当前实现尚未达到目标文件的发布条件。数据库表存在和旧测试通过不能证明完整主链路已完成。

## 已有实现与覆盖

- AgentRun、RunStep、RunCheckpoint、RunEvidence、RunArtifact 的迁移与 session adapter。
- 运行详情按关联 AgentSession 的浏览器 owner 隔离；无 owner 和其他浏览器均返回 404。
- 验收摘要没有足够信息时返回未通过及未知 telemetry，不能固定返回未降级/未切换。
- 证据 ID 采用规范 JSON 的 SHA-256；完整工具 summary 保存为 value，避免文本截断为 method。
- adapter 写入状态、证据、产物使用一个事务；证据验证失败回滚全部写入。
- 旧阶段名映射到 DAG ID；通用失败事件保留真实当前步骤，不虚构最终复核失败。
- 取消等待 worker 退出确认；租约不匹配的旧 worker 无权确认或持久化 checkpoint。
- 主模型规划、决策失败暂停；不自动继续规则工具。规划重试重新进入 planner。
- 新增 RunEvent；新运行创建与 reducer 状态变化写入版本化事件、数据库事件游标和完整状态/计划/上下文 checkpoint。
- command_key 同内容重放不重复落库，不同内容报冲突；SQLite 并发提交同一命令只产生一条事件和一个 checkpoint。
- v2 事件分页和 SSE 可从 Last-Event-ID 重放；两种传输均校验 owner 和游标，事务失败不暴露未提交事件。
- worker 重新进入循环时可从最新版本化 checkpoint 恢复指标、证据、工具历史及 completed_steps，并跳过重复规划。
- 本地迁移 0023、0024 已验证并应用；0023 之前的 SQLite 备份保存在 gitignored `.codex-runtime/schema-backups/`。0024 为新增 RunToolCall 表。
- 相关回归覆盖在 `map_api/test_run_recovery.py`、`map_api/test_run_journal.py` 与 `map_api/tests.py::AgentRunKernelTests`；最近完整测试 373 项通过。这不等于真实服务 golden path 验收。
- 当前完整回归再次通过 373 项；公开 Element84 检索返回 3 景，真实 TiTiler/COG NDWI 因 AOI 有效像元低于 60% 明确拒绝输出比例。远端 `http://101.200.128.20:8083/` 健康接口返回 200，但该部署的 `/api/v2/agent/runs/` 返回 404，证明部署版本尚未包含 v2 DAG 实现。

- RunToolCall 保存调用身份、有效输入、尝试次数、租约、结果和上下文补丁；成功回执重放不会再次调用工具；中断结果未知和影像输出丢失都需要显式重试。
- 失败调用不把内存中间结果写为成功事实；取消和 worker 接管拒绝新调用/旧结果；旧 session 也执行结构化结果检查。
- Sentinel 失败不再受环境开关控制而自动切换 Mapbox；日期、云量和视觉输出门禁不能用 force_continue 绕过；工具失败立即暂停；空模型决策不触发规则工具或合成完成结论。
- 模型 JSON 拒绝自由文本截取、代码围栏、重复字段和非有限数；决策协议校验单一动作、字段和工具；工具使用完整 JSON Schema（新增 jsonschema 4.26.0 运行依赖）。
- observer 不再把当前阶段之前的节点推定为完成；未执行步骤保持 pending。
- v2 创建/列表/动作/重新规划/证据/产物接口已接入；新 API 强制持久化队列，即使客户端请求 sync 或环境为 thread 也不会启动 Web 线程。
- 重新规划增加版本、清理旧工作记忆并保留历史 checkpoint；接口覆盖 owner 隔离和操作幂等；错误状态可经显式重试/规划恢复，已完成/已取消运行不能重新打开。
- 公开决策、工具结果、质量和用户事件双写 v2 journal；事件追加与 checkpoint 同事务。
- 浏览器事件缓冲器的乱序补齐、重复去重和 run 隔离已通过 Node 检查；JS 语法检查通过。真实 Sentinel/模型验收尚未执行。

## 尚未完成的硬性门禁

1. **真实主链路**：v2 队列运行已切换到 AgentRun DAG reducer；步骤领取、租约、工具回执、产物、证据与 checkpoint 均由新执行器负责。旧非队列 API 仍保留兼容执行器，不能声称所有入口已迁移。
2. **DAG 执行**：v2 队列路径已有 15 节点依赖图、逐步租约、过期接管、取消 fencing、输出 Schema、结构化工具回执和产物下载；本地验证通过，远端部署仍是旧版本，尚未验证部署环境 worker。
3. **状态与事件事务**：RunEvent 与新 checkpoint 已统一事务；旧 ExecutionEvent 仍有独立写入路径，工具调用事件还未全部迁入 v2。恢复入口已读取新 checkpoint，但旧版无版本快照不能冒充完整恢复记录，工具级成功回执和失败/中断重试已接入，逐步 DAG 租约仍待实现。
4. **完成条件**：旧 completed 路径尚未统一校验所有必需步骤、质量门禁、required evidence、最终引用和限制。运行详情的验收结果因此不能证明成功。
5. **Provider 协议**：已加入 GLM/Qwen/OpenAI-compatible 统一接口、六类 decision schema、流式完整性、输入图像/上下文预算和 usage/latency telemetry；官方在线文档抓取本轮返回 403，真实兼容端点仍待部署验证。
6. **数据与工具**：v2 Sentinel 路径已接入 DataRequirements、能力匹配、日期/云量/覆盖门禁、真实 COG 读取、scale/offset、SCL、AOI、同日拼接、两期共同网格、NDWI/NDVI/MNDWI、栅格/报告产物和证据契约。
7. **前端**：按用户 2026-09-09 的最新要求保留线上深色地图工作台，`/agent/` 复用 `/workbench/` 的模板。撤除独立浅色工作台；v2 创建、运行状态、计划、事件、暂停、重试、目标修改与重新规划、取消、证据引用及产物链接已接入原有左侧 Agent 面板，中央地图和右侧区域工作区结构保留。地址中的 run ID 用于刷新恢复与窗口隔离；显式 `?legacy=1` 保留旧 Agent 入口，不在 v2 失败时自动切换。已只读核对线上首页和地图页，原版核心样式、首页及工作区交互脚本逐字节一致。`scripts/test_map_run_ui.cjs` 的隔离浏览器测试通过上述交互、事件去重、完成门禁、证据链接、键盘焦点和 1440px 布局；实际查看了本地渲染截图，未出现脚本错误。相关控制 API 与 fixture golden path 共 14 项通过。这些都是本地验证，完整移动端、真实 SSE 重连和真实运行回溯尚未验收。
8. **真实验收**：公开 Sentinel 检索和真实 TiTiler 已只读验证；当前真实样本未通过有效像元门禁，远端旧部署缺少 v2 API，真实主模型到报告、重启/取消/重试/SSE 恢复仍未通过。
9. **发布**：未执行部署；上述门禁未齐全前不得标记正式版本通过。

当前验收摘要将缺失的执行 telemetry 表示为 null。后续必须用真实、版本化、可溯源的执行记录替换未知值，不能通过硬编码 false/0 消除缺口。
