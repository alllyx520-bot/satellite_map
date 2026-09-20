# SatelliteSense 审查-优化计划(Review & Optimization Plan)

> **状态：已完成的时点记录（2026-07-21 那一轮），不是当前计划。** 该轮 Phase 0–10 已全部执行，
> 执行结果见文末「执行状态」表。正文对代码的描述（`views.py` 3463/1541 行、`start.py --noreload`、
> 测试 60+/120/162 项、前端三张无构建页面、conda Python 路径等）**均已过时**：
> 现在 `views.py` 为 2471 行，`start.py` 不再用 `--noreload`，后端测试 608 项，前端是 React+Vite
> 构建产物，运行环境为 `.codex-runtime/venv/`。当前入口见 [CLAUDE.md](CLAUDE.md)。
> 正文与附录原样保留，仅作为当时的检查清单与方法参考。
>
> 注：本文档正文里的行号锚点已全部失效；需要定位代码请用 `grep`。

- **制定日期**:2026-07-21(全部基线数据为当日实测,非推测)
- **适用范围**:本仓库全部后端 / 前端 / 部署配置 / 文档 / 测试体系
- **使用方法**:按 Phase 顺序执行;每个 Phase 自带验收标准;每轮闭环 = `test` + `smoke_pipeline` 双绿 + `REVIEW_FINDINGS.md` 更新。对 AI 助手下指令时直接说"按 REVIEW_PLAN.md 执行第 N 轮 / Phase N"即可。

---

## 0. 总览

### 0.1 目标
1. 系统排查并修复**正确性 / 安全性 / 可靠性 / 可维护性**问题;
2. 建立可复跑的**回归基线**(单测 + smoke + 故障矩阵测试);
3. 让 **CLAUDE.md / README 与代码零漂移**。

### 0.2 范围
- **包含**:Django 后端 9 区块、前端 3 页面(home / workbench / design)、部署配置(deploy/ + .deploy/)、文档、测试体系。
- **不包含**:新增产品功能、模型与 prompt 调优、前端审美改动(全局规则:UI/样式修改必须先问;本计划前端部分只碰安全/契约/错误态)。

### 0.3 执行原则
1. **基线先行**:开工前 `manage.py test map_api` + `manage.py smoke_pipeline` 必须全绿,一切改动相对绿色基线做增量。
2. **外科手术式**:只改审查确认的问题,不顺带重构没坏的东西;代码风格随项目现状。
3. **每条可验证**:每个检查项带验收命令或手动步骤(见各 Phase 表格"验收"列)。
4. **先外后内**:先修外人能触发的(安全 / 容错),再修慢慢腐烂的(结构 / 文档)。
5. **决策点归用户**:有方案分歧的列入 §3,执行到相应 Phase 前拍板,不自作主张。

### 0.4 交付物
| 交付物 | 形式 |
|---|---|
| 问题清单 | 新建 `REVIEW_FINDINGS.md`(每条:severity + 文件:行号 + 状态 open/closed/wontfix) |
| 修复 | 按 Phase 分批的改动(仅在用户要求时 commit) |
| 回归基线 | 新增测试 + 故障矩阵测试 + CI(如批准) |
| 文档 | CLAUDE.md / README / DEPLOY.md 漂移修正 |

### 0.5 优先级与轮次
| 轮次 | 优先级 | 内容 |
|---|---|---|
| 第 1 轮 | P0 | Phase 0–2:基线锁定 + 红线核对 + 安全 |
| 第 2 轮 | P1 | Phase 3–5:正确性 + 可靠性 + 并发/进程模型 |
| 第 3 轮(按需) | P2 | Phase 6–9:性能 + 结构 + 测试/CI + 文档 |
| 第 4 轮(按需) | P3 | Phase 10:前端健壮性 |

---

## 1. 现状基线(2026-07-21 实测)

### 1.1 已核验通过(保持即可,不返工)
| # | 项 | 证据(文件:行) |
|---|---|---|
| 1 | `proxies={"http":None,"https":None}` 全覆盖 | 5 文件 9 处:`earth_search.py`×2、`get_satellite_image.py`×2、`agent_tools.py`×3、`views.py:2968`×1、`scripts/download_remoteclip.py`×1 |
| 2 | 所有 `requests` 调用带 `timeout` | `views.py:2971`(8s)、`earth_search.py:136/156`、`agent_tools.py`(45/12/timeout)、scripts(60s) |
| 3 | `CorsMiddleware` 位于 Session 与 Common 之间 | `settings.py` MIDDLEWARE 顺序 |
| 4 | dashscope 全部集中于 `_call_qwen`,VL kwargs 门控 + 结构化日志 | `views.py:139–159`(`VL_MODELS` 判断;`MultiModalConversation.call` 全库仅此 1 处) |
| 5 | `safe_media_path` 实现可靠 | `views.py:162–172`:basename 剥离路径成分 + realpath + `base_real+os.sep` 前缀校验(规避 `/a/b` vs `/a/b_evil` 误判) |
| 6 | `start.py` 使用 `--noreload`(PyInstaller 兼容) | `start.py:25` |
| 7 | 依赖全部版本锁定 | `requirements.txt` 全 `==`;可选 extras 注释清晰 |
| 8 | 下载进度已持久化到 DB(多 worker 基本安全) | `_persist_progress`→`DownloadTask` + `close_old_connections()`(`views.py:210–222`) |
| 9 | 测试 60+ 个,含 mock 掉 DeepSeek/Qwen 的 Agent 集成测试 | `tests.py` 2312 行 |
| 10 | 日志落文件(AI 调用单独记) | `settings.py:146` LOGGING → `logs/app.log` + `logs/ai_calls.log` |
| 11 | 仓库卫生 | `.gitignore` 覆盖全(output/.idea/db/media/models/.env/.deploy);`git ls-files` 无误入库杂项;全库零 TODO/FIXME |
| 12 | 生产 env 覆盖关键配置项 | `.deploy/server.env` 含 `DJANGO_DEBUG/SECRET_KEY/ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS/CORS_ALLOW_ALL_ORIGINS` + 各 key + Sentinel 调优参数(具体值属机密,未读取) |

### 1.2 已确认风险(本计划处理对象)
| # | 风险 | 严重度 | 归属 |
|---|---|---|---|
| R1 | 全部 API 无鉴权 + `@csrf_exempt`:公网部署 = 任何人可无限消耗你的 DashScope/Mapbox/DeepSeek 付费额度 | 高 | Phase 2 |
| R2 | `browser.js` 34 处 `innerHTML`,AI 回答 / POI 名称 / observer 消息均为外部文本,存在 XSS 面 | 高 | Phase 2 |
| R3 | 错误响应形状不一致(如 `get_progress` 用 `{code,msg}`,与别处不同) | 中 | Phase 4 |
| R4 | gunicorn `--workers 2` + 后台线程:worker 被回收/重启会杀死下载/Agent 线程,DB 中任务永久停留 `downloading` | 中 | Phase 5 |
| R5 | 内存 `_download_progress`(views.py 12 处引用)与 DB 双轨并存;跨 worker 轮询只能读 DB,DB 写入若滞后则进度显示落后 | 中 | Phase 5 |
| R6 | 主动感知坐标链(缩放→crop→反算原图→经纬度)只有各段单测,缺端到端数值验证 | 中 | Phase 3 |
| R7 | 6 个外部依赖(Mapbox/Earth Search/TiTiler/DashScope/DeepSeek/高德)的失败行为无 mocked 测试覆盖 | 中 | Phase 4 |
| R8 | 无 CI(仓库无 `.github/`),测试"存在但没有自动跑" | 中 | Phase 8 |
| R9 | CLAUDE.md 漂移 4 处:Constraints 仍写 Sentinel "single candidate"(README:270 已更新为 mosaic 事实);API 表缺 cleanup / imagery search / design 三个路由;Agent 槽位实为 DeepSeek+规则(`deterministic_extract_slots`)混合;测试描述低估为"pure-function unit tests" | 低 | Phase 9 |
| R10 | `media/` 无自动清理(现 16MB/38 文件;`_hd/_overview/_tile_*/_crop_*/_stage1_*` 衍生物累积);日志 FileHandler 无轮转 | 低 | Phase 6 |
| R11 | 无 LICENSE;Mapbox attribution 与 Sentinel license 展示待审 | 低 | Phase 9 |

---

## 2. 分阶段计划

### Phase 0 — 基线锁定(P0,约 10 min,不改代码)
| # | 动作 | 验收 |
|---|---|---|
| 0.1 | 跑 `C:\Users\Lenovo\anaconda3\envs\general\python.exe manage.py test map_api` | 退出码 0 |
| 0.2 | 跑 `…\python.exe manage.py smoke_pipeline`(默认模式,不调外部服务) | 全部 step `ok: true` |
| 0.3 | 新建 `REVIEW_FINDINGS.md`,头部记录日期、git HEAD、Python/Django 版本、上面两条基线输出摘要 | 文件存在 |

### Phase 1 — 红线核对(P0,约 0.5h,grep 级,不改代码)
§1.1 的 12 项已核验;本 Phase 补齐剩余待审项,全部结果记入 FINDINGS:

| # | 检查项 | 验收方法 | 现状 |
|---|---|---|---|
| 1.1 | `safe_media_path` **覆盖**所有接收文件名的端点(`show-img`/`progress`/`history/<id>`/`report/download`/`cleanup`) | 枚举端点参数 vs 14 处调用点,输出覆盖表 | ⬜ 待审(实现已 ✅,覆盖未枚举) |
| 1.2 | RGBA/P→RGB 转换覆盖所有 JPEG 保存点 | grep `\.convert\("RGB"\)` vs 所有 `.save(` | ⬜ |
| 1.3 | 分辨率钳制 `[1,4096]` 在所有入口生效 | 审 `compute_image_plan` + `parse_number_param` 调用 | ⬜ |
| 1.4 | 生产 `server.env` 值安全(`DJANGO_DEBUG=false`、`CORS_ALLOW_ALL_ORIGINS=false`、SECRET_KEY 为随机值、ALLOWED_HOSTS 为具体域名) | **用户自验**:生产机访问 `/api/system/health/` 看 config 布尔字段(该端点不回显值,已有测试保证) | ⬜ 待用户 |

### Phase 2 — 安全审查与修复(P0)
| # | 检查/修复项 | 验收 |
|---|---|---|
| S1 | 为 `safe_media_path` 补**对抗性单测**:`../../etc/passwd`、`..\\..\\`、绝对路径 `C:\\x`、空串、`.JPG` 大小写、双扩展名 `a.php.jpg`、符号链接;按 Phase 1 覆盖表补齐遗漏端点 | 新测试全绿;手动 `curl "/api/satellite/show-img/?file=..%2F..%2Fmanage.py"` 不泄漏源码 |
| S2 | **XSS 审计**:34 处 `innerHTML` 逐一分类(静态文案 / 动态转义 / 危险);危险位(AI 回答、targets label、POI 名、observer message、history 消息)改 `textContent` 或统一 `escapeHtml` | 发送含 `<img src=x onerror=alert(1)>` 的问题,渲染区不弹窗、查看元素已转义 |
| S3 | **滥用面治理**(决策点 D1):a) 简易内部 token 中间件;b) 按 IP 限流;c) 维持现状 + DEPLOY.md 声明仅限内网 + nginx 加 basic auth | 按拍板方案实施并验证:循环请求 20 次触发限制/拒绝 |
| S4 | 机密泄漏扫描:git 历史与已跟踪文件 grep `sk-`/高熵串;确认生成的 docx 报告不嵌入任何 key | 扫描零命中 |
| 验收汇总 | 全套测试绿 + S1/S2 手动脚本通过 + FINDINGS 中 R1/R2 关闭 | |

### Phase 3 — 正确性审查(P1)
| # | 检查/修复项 | 验收 |
|---|---|---|
| C1 | **坐标链端到端数值测试**:构造合成用例 `geo_bbox → pixel_bbox_to_geo 逆变换 → crop 坐标 → map_bbox_to_original → 还原经纬度`,断言 round-trip 误差 < 1e-9;主动感知 ≤2 级嵌套坐标传递各写 1 例 | 新测试绿 |
| C2 | `normalize_model_answer` 对抗样本:空串 / 纯空白 / 只有 `<think>` 无 `<answer>` / 多个 `<answer>` / markdown 代码块包裹 | 新测试绿 |
| C3 | **前后端契约对照表**:枚举 `browser.js` 所有 `data.*`/`session.*` 字段读取 vs 后端 payload 函数(`scene_payload`/`agent_session_payload`/`chat_history_payload`/`sentinel_response_data`/`sentinel_retrieval_timeline_payload`),输出对照表;不一致即修 | 对照表零不一致 |
| C4 | 报告链全字段演练:`generate_report` 在 compare 模式 / 图片文件缺失 / 超长回答 / emoji 场景各生成一次,打开 docx 检查 | 人工打开 4 份无乱版 |
| C5 | 已知局限登记:极地/跨 180° 经线的 bbox 行为 → 明确写入文档作已知限制,不允许静默出错 | 文档有记载 |

### Phase 4 — 可靠性与容错(P1)
| # | 检查/修复项 | 验收 |
|---|---|---|
| R-1 | **故障矩阵落地为 mocked 测试**(每个依赖至少 1 例):Mapbox 429/空白响应 → 重试后带原因失败;Earth Search 500/超时 → 清晰错误 JSON;TiTiler 渲染失败 → 回退下一候选;DashScope `status_code≠200`/配额耗尽 → 兜底回答+错误提示;DeepSeek 返回非 JSON → 规则槽位兜底或明确失败;高德 key 无效 → 明确报错 | 矩阵测试全绿 |
| R-2 | **错误形状统一**:审所有端点错误返回 → 统一 `{ok:false, error}` + 语义正确 HTTP 码;迁移 `{code,msg}` 式响应;`browser.js` 只解析一种形状 | 契约测试绿 + 手动触发 3 种错误看前端展示 |
| R-3 | 后台任务容错:审下载线程/Agent 线程每条异常路径都写入 `status=failed`+`error_message`(异常注入测试) | 注入异常后轮询返回 failed 而非卡 downloading |
| R-4 | 额度成本封顶文档化:实测 precise 模式单会话 VL 调用上限(stage1 + ≤2 级 zoom + 瓦片数)与 DeepSeek 调用数,写入 CLAUDE.md;可选在 health 端点加用量提示 | 文档有数字 |

### Phase 5 — 并发与进程模型(P1/P2)
| # | 检查/修复项 | 验收 |
|---|---|---|
| P-1 | **僵尸任务治理**(R4):worker 重启后遗留 `downloading` 记录 → 增加孤儿扫描(启动时或 progress 查询时,将超时 N 分钟无更新的任务标 `failed`+可恢复提示),或 management command `cleanup_stale_tasks` | 模拟:建 downloading 记录 → 触发扫描 → 状态变 failed |
| P-2 | 双轨进度审计(R5):确认 DB 为 source of truth、内存字典仅同 worker 加速;核对 `_persist_progress` 写入频率 vs 前端轮询间隔;如行为不一致则删一条路径(外科手术式) | 跨 worker 轮询进度单调递增不回退 |
| P-3 | `ThreadPoolExecutor` 生命周期:确认 `with`/`shutdown` 用法,异常路径无线程泄漏 | 代码审计结论记入 FINDINGS |
| P-4 | SQLite 并发:后台线程写 + Web 读,建议开 WAL(`settings.py` 一行 `OPTIONS`/PRAGMA)减少锁竞争 | 并发下载测试无 `database is locked` |
| 决策点 D2 | workers 保持 2 + 僵尸清理 vs 改 1 | 推荐保持 2(进度已 DB 化) |

### Phase 6 — 性能与资源(P2)
| # | 检查/修复项 | 验收 |
|---|---|---|
| F-1 | 新增 management command `cleanup_media`:`--dry-run`/`--age N天`,按衍生物命名模式(`_hd/_overview/_tile_*/_crop_*/_stage1_*`)与过期报告清理;审 `cleanup_cache` 端点覆盖范围是否一致 | dry-run 输出合理;真跑后 `du -sh media/` 下降 |
| F-2 | 日志轮转:`FileHandler` → `RotatingFileHandler`(如 10MB×5) | 重启后配置生效 |
| F-3 | RemoteCLIP 单例:确认懒加载时机(首次查询变慢需文档说明)、GPU 显存不释放是否有碍 | 审计结论入 FINDINGS |
| F-4 | 前端资源:`design.html` 12 个 CDN 字体 → 本地化或 `preconnect`;审 Three.js/Leaflet CDN 版本是否锁定 | 断网加载不白屏或有可读错误 |

### Phase 7 — 结构改进(P2,可选,决策点 D3)
> 原则:纯搬家 + import,零行为变更;搬一个文件跑一次全量测试;`tests.py` 中 `patch("map_api.views.X")` 路径通过在 views 保留 re-export 别名避免批量改动。

| # | 拆分方案(views.py 3463 行) | 验收 |
|---|---|---|
| M-1 | `map_api/geo_math.py`:bbox/GSD/geo 换算/crop 几何(已有完整单测,最安全,先搬) | 测试全绿,diff 仅移动+import |
| M-2 | `map_api/sentinel_pipeline.py`:mosaic / cache / 候选选择 / `scene_from_*` | 同上 |
| M-3 | `map_api/payloads.py`:各 `*_payload` 构造函数 | 同上 |
| M-4 | `map_api/agents/orchestrator.py`:views.py 1892–2563 的 Agent 编排段 | 同上 + `smoke_pipeline --agent` 绿 |

### Phase 8 — 测试与 CI(P2)
| # | 检查/修复项 | 验收 |
|---|---|---|
| T-1 | Phase 2–5 新增测试统一并入 `tests.py`(沿用现有 unittest 风格) | 套件绿 |
| T-2 | CI(决策点 D4):`.github/workflows/ci.yml`,ubuntu + Python 3.11 + `pip install -r requirements.txt` + `manage.py test map_api` + `smoke_pipeline`(默认模式,无外部调用、无需 key) | 本地双绿 + (commit 经你允许后)push 见绿勾 |
| T-3 | 可选:`pytest-cov` 出一份覆盖率基线(requirements.txt 已注释预留) | 报告存档 |

### Phase 9 — 文档同步(P2)
| # | 检查/修复项 | 验收 |
|---|---|---|
| D-1 | CLAUDE.md 修正 4 处漂移(见 R9):Constraints single-candidate → mosaic 现状(与 README:270 对齐);API 表补 `/api/satellite/cleanup/`、imagery search、`/design/`(路由以 `urls.py` 为准核对);Agent 段补"DeepSeek + `deterministic_extract_slots` 规则混合";测试描述改为"60+ 单测与 mocked 集成测试" | 逐节对代码,零漂移 |
| D-2 | LICENSE(决策点 D5)+ 署名审计:地图 Mapbox attribution 可见、Sentinel `license_type` 随报告带出 | 前端可见 + 报告含 license 字段 |
| D-3 | DEPLOY.md vs `deploy/` 实况核对:workers=2、端口 8010/8083、TLS 终止建议、僵尸任务注意事项 | 文档与配置一致 |
| 决策点 D6 | design 页保留+文档化 vs 删除(README:313 已有 Frontend Design State 章节) | 推荐保留+文档化 |

### Phase 10 — 前端健壮性(P3,不改审美)
| # | 检查/修复项 | 验收 |
|---|---|---|
| U-1 | 错误态 UX:下载失败 / AI 配额耗尽 / 网络断开 三场景界面提示 | 手动演练 3 场景 |
| U-2 | 轮询清理全路径审计:`agentPollTimer`/`pollTimer` 已有成对 clear(基本合格),核对页面卸载、任务重发前的残留路径 | 审计结论入 FINDINGS |
| U-3 | CDN 失效兜底:Leaflet/Three 不可加载时给可读错误而非白屏 | 断网手动验证 |
| U-4 | CJK 排版纪律:行高 ≥1.7、禁微软雅黑/宋体系统默认、`word-break: keep-all` | 逐页检查 |
| 验收方式 | 手动脚本 + 系统 Chrome `--remote-debugging-port` + CDP 截图核验(无需装 playwright) | 截图存档 |

---

## 3. 决策点汇总(执行到相应 Phase 前拍板)
| # | 决策 | 选项 | 推荐 | 时机 |
|---|---|---|---|---|
| D1 | 滥用面治理 | a) token 中间件 b) IP 限流 c) 声明仅限内网+nginx auth | b+c 组合 | Phase 2 |
| D2 | gunicorn workers | 保持 2 + 僵尸清理 / 改 1 | 保持 2 + 清理 | Phase 5 |
| D3 | views.py 拆分 | 执行 M-1..M-4 / 暂缓 | 第 3 轮再定(先补测试再搬家) | Phase 7 |
| D4 | 引入 CI | GitHub Actions / 暂不 | 引入(默认 smoke 不耗配额) | Phase 8 |
| D5 | LICENSE | MIT / 其他 / 暂不 | 由你定 | Phase 9 |
| D6 | design 页去留 | 保留+文档化 / 删除 | 保留(README 已有章节) | Phase 9 |

---

## 4. 轮次闭环
| 轮次 | 内容 | 闭环验收 |
|---|---|---|
| 1 | Phase 0–2 | 双绿;FINDINGS P0 项全部 closed/wontfix;S1/S2 手动安全演练通过 |
| 2 | Phase 3–5 | 双绿;故障矩阵测试绿;前后端契约表零不一致;僵尸任务模拟通过 |
| 3 | Phase 6–9 | 双绿;CLAUDE.md/README 零漂移;CI 绿(若 D4 批准) |
| 4 | Phase 10 | 手动演练脚本全通过 |

每轮结束更新 `REVIEW_FINDINGS.md`(新增 / 关闭 / 状态流转),并在本文件对应 Phase 标注 ✅。

## 5. 风险与回滚
- 每个 Phase 的改动成批,**仅在用户要求时 commit**;某 Phase 验收不过只回滚该 Phase,不碰其他 Phase 与用户已有改动。
- **Phase 7 风险最高**(tests 的 patch 路径)→ 用 re-export 别名方案保证测试零改动;搬一个文件跑一次全量测试。
- `smoke_pipeline --live-*` 消耗外部配额,默认不开启;仅需证明外部依赖时按 CLAUDE.md 表格选用。
- 前端修复只限安全/契约/错误态;任何审美改动另行先问。

## 6. 完成定义(Definition of Done)
1. `REVIEW_FINDINGS.md` 中 P0/P1 全部 closed 或 wontfix(带理由);
2. `manage.py test map_api` + `smoke_pipeline` 双绿;
3. 故障矩阵(6 个外部依赖)各有 ≥1 个 mocked 测试;
4. 前后端契约对照表零不一致;
5. CLAUDE.md / README 与代码零漂移;
6. 手动安全演练(路径穿越 + XSS)通过。

---

## 附录 A:常用核验命令
```bash
PY=C:\Users\Lenovo\anaconda3\envs\general\python.exe
$PY manage.py test map_api                      # 单测基线
$PY manage.py smoke_pipeline                    # 核心闭环 smoke(无外部调用)
$PY manage.py smoke_pipeline --agent            # Agent 闭环(mocked)
# 红线 grep:
grep -rn "requests\.\(get\|post\)(" map_api scripts   # 所有外呼 → 逐个核对 proxies/timeout
grep -rn "vl_high_resolution_images" map_api          # 应只在 _call_qwen 出现
grep -c "innerHTML" static/browser.js                 # XSS 面计数(基线 34)
grep -rn "safe_media_path" map_api/views.py           # 覆盖点(基线 14 处)
du -sh media/ && find media -type f | wc -l           # 膨胀监测(基线 16MB/38)
```

## 附录 B:REVIEW_FINDINGS.md 条目模板
```
### [P0][安全] 简短标题
- 状态:open | closed | wontfix
- 位置:文件:行号
- 现象:具体输入 → 具体错误后果
- 修复:做了什么
- 验证:命令或步骤 + 日期
```

---

## 执行状态（2026-07-21 全部完成）

| Phase | 结果 | 要点 |
|---|---|---|
| 0 基线 | ✅ | 120→最终 162 测试 OK；smoke 默认/--agent 双绿 |
| 1 红线 | ✅ | 6 条红线全部核验通过；H1 瓦片模式 `!= 'RGB'` 统一转换 |
| 2 安全 | ✅ | 限流中间件(R1) + 3 处 XSS 修复(R2) + 机密扫描零命中；safe_media_path 对抗测试 |
| 3 正确性 | ✅ | 坐标链端到端数值测试 + normalize 对抗 + 契约表零不一致 + 报告边界 |
| 4 可靠性 | ✅ | 故障矩阵 8 例(R7) + 13 处 HTTP 状态码语义化(R3) + 后台异常落库(R-3) |
| 5 并发 | ✅ | cleanup_stale_tasks 僵尸治理(R4) + 双轨审计(R5) + SQLite WAL(P-4) |
| 6 性能 | ✅ | cleanup_media + 日志轮转(R10) + preconnect + CDN 版本锁定 |
| 7 结构 | ✅ | views.py 3463→1541 行；拆 6 模块；再导出零破坏；每搬一模块跑全测 |
| 8 测试/CI | ✅ | .github/workflows/ci.yml(test + 双 smoke，无外部依赖) |
| 9 文档 | ✅ | CLAUDE.md/README 漂移修正 + MIT LICENSE + DEPLOY.md 加固(R9/R11) |
| 10 前端 | ✅ | 逐屏截图审查：5 处 UI/UX 修复 + 2 项健壮性 + CJK 排版合规 |

**最终核验**：`manage.py test map_api` → 162 OK；`smoke_pipeline` 与 `smoke_pipeline --agent` → 全 ok。
**决策记录**：D1=限流中间件+部署声明；D2=workers=2+僵尸清理；D3=已执行拆分；D4=已引入 CI；D5=MIT；D6=design 页保留+文档化。
**未提交**：按全局规则，所有改动未 commit（待用户指示）。
