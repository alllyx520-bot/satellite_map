# SatelliteSense 专用 Agent Harness 源码对照

## 研究对象

- `xai-org/grok-build`：Apache-2.0。重点阅读 `xai-grok-shell`、`xai-grok-tools`、`xai-grok-workspace`、`xai-workflow`。
- `sst/opencode`：重点阅读 `packages/core` 的 Session runner、Tool registry、durable event/history 和 permission 设计。

研究源码位于本机隔离目录 `.research/`，不进入产品发布包。

## 可移植的核心设计

### 1. Runner 不等于业务流程

Grok Build 的 workflow engine 只负责执行确定性 workflow、记录 host-call journal、处理中断/暂停/取消/预算和恢复；OpenCode 的 Session runner 负责 provider turn、输入队列、steer/queue、执行协调和事件投影。业务能力通过工具注册提供，而不是写死在 runner 中。

SatelliteSense 当前相反：`run_agent_loop` 外层仍包含固定的理解、定位、影像、NDWI、视觉和复核阶段，模型只在固定遥感工具集合中选择下一步。这是“领域状态机”，不是通用 harness。

### 2. Tool 是带契约的 opaque capability

OpenCode V2 的 Tool Definition 同时拥有：

- 输入 codec/schema
- 输出 codec/schema
- 唯一 executor
- invocation context（session、agent、assistant message、tool call）
- permission boundary
- model-facing projection
- 输出大小限制与托管存储
- stale registration rejection

SatelliteSense 当前 `REGISTRY` 只有 description、parameters 和函数，缺少统一输入校验、输出校验、调用身份、权限分类和输出边界。后续要把 Sentinel、Mapbox、TiTiler、VL 都封装为同一 Tool contract。

### 3. Durable event 是事实来源，UI 是 projection

OpenCode 的 Session history 以 durable aggregate sequence 为游标；实时文本/推理片段可以是 ephemeral，但不能伪装成可恢复历史。Grok workflow journal 使用连续 seq、请求 hash、结果回放和 divergence 检查，避免恢复时重复执行副作用。

SatelliteSense 当前把 `timeline`、`artifacts.tool_history`、`observer` 混在一起，前端直接读取固定阶段 projection。应增加统一 ExecutionEvent：`turn_started`、`model_decision`、`tool_started`、`tool_result`、`plan_changed`、`approval_requested`、`checkpoint`、`completed`、`failed`，并以事件序号恢复和推送。

### 4. 输入队列和执行协调必须独立

OpenCode 将 `steer` 与 `queue` 分开：steer 在安全的 provider-turn 边界插入，queue 保持 FIFO；SessionRunCoordinator 保证同一 Session 不重复执行，不同 Session 可以并发。重连通过 durable history 游标继续，而不是依赖内存线程。

SatelliteSense 当前消息接口会直接触发线程/worker，缺少统一 turn 边界和输入队列模型。现有租约保护解决了多进程所有权，但还没有 provider-turn 协调层。

### 5. 中断、失败、暂停、预算是不同终态

Grok workflow 明确区分 Completed、Paused、Cancelled、BudgetExceeded、Fatal；OpenCode 也区分 ToolFailure、interruption、defect、stale call。不能把所有错误都转成“等待用户确认”或“状态读取失败”。

SatelliteSense 需要把以下情况分开：

- 可重试网络错误
- 工具返回的业务质量不足
- 需要用户授权的副作用
- 模型格式错误
- 超过预算/循环上限
- 用户取消
- 不可恢复系统错误

## SatelliteSense 移植边界

不移植：TUI、xAI 登录、Rust 专属 workspace daemon、xAI 计费和内部 telemetry。

移植到现有 Django/SQLite：

1. Python `HarnessSessionRunner`：turn 协调、输入队列、取消和恢复。
2. Python `ToolDefinition/ToolRegistry`：schema、调用上下文、权限、输出投影和边界。
3. SQLite `ExecutionEvent`：连续序号、请求 hash、事件重放和断点。
4. `Checkpoint`：计划、事实、假设、证据、工具结果和模型上下文摘要。
5. 前端真实事件流：不再把固定 `STEP_VOCAB` 当作 Agent 思考过程。

## 第一阶段验收标准

- 同一个用户目标可产生不同工具路径，而不是固定八阶段。
- Sentinel 失败时记录结构化失败原因，Agent 能在预算内自主重规划，而不是直接弹固定确认框。
- worker 强杀后从 checkpoint/event cursor 恢复，不重复已经成功的副作用工具。
- 每个工具调用都能追溯到 session、turn、assistant message 和 tool call ID。
- 前端展示真实事件、计划变更和工具结果摘要；没有伪造的“当前阶段”。
- 241 个现有回归测试继续通过，并新增 runner、registry、event replay 和恢复测试。

