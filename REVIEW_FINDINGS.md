# REVIEW_FINDINGS — SatelliteSense 审查问题清单

- **开审日期**:2026-07-21
- **基线**:git HEAD `ddac7d4` | Python 3.11.15 | Django 5.2.13
- **基线验证**(2026-07-21):`manage.py test map_api` → **120 tests OK**;`manage.py smoke_pipeline` → 全 ok;`smoke_pipeline --agent` → 全 ok
- **状态约定**:`open` → `closed`(带修复+验证)或 `wontfix`(带理由)

---

## P0 — 安全

### [P0][安全] R1 全部 API 无鉴权 + csrf_exempt,公网可烧付费额度
- 状态:**closed**(2026-07-21, Phase 2)
- 修复:新增 `map_api/middleware.py` RateLimitMiddleware(滑动窗口 60s,线程安全):普通 API 120/min·IP、高成本端点(AI/Agent/报告)30/min·IP;健康检查豁免;`RATELIMIT_DISABLED=1` 演示模式关闭;接入 settings(紧随 CorsMiddleware,429 也带 CORS 头)。5 个中间件单测(429/Retry-After/scope 独立/豁免/disabled/0 值关闭)。
- 验证:`manage.py test map_api` 全绿(137 tests);DEPLOY.md 将补充"生产建议内网部署或加 nginx auth"(Phase 9)

### [P0][安全] R2 browser.js innerHTML XSS 攻击面
- 状态:**closed**(2026-07-21, Phase 2)
- 审计:逐行审完 2154 行 browser.js 全部 34 处 innerHTML。Agent 面板/scene 面板/history/renderMarkdown(先转义再转 markdown)/renderMethodMeta 均已 escapeHtml 或 textContent(既有质量高)。实际漏点仅 3 处:
  1. :1323 `status.innerHTML='抓取失败:'+d.msg` → 改 textContent
  2. :1638 地图 popup 的 `t.label`(模型输出)→ 全字段 escapeHtml
  3. :1908 搜索结果 `name/display_name`(高德外部数据)→ escapeHtml
- 验证:修复后逐处复核;showToast 走 textContent、用户气泡走 textContent 已确认安全

### [P0][安全] safe_media_path 端点覆盖未枚举
- 状态:**closed**(2026-07-21, Phase 1)
- 位置:map_api/views.py(实现 :162,调用 9 处)
- 核验:所有**文件系统访问**路径(show-img:2567、ai_query:2701/2714、report 生成:3422、report 下载:3459、sentinel 后处理:787/1004/1020、历史图片可用性:1676)均经 safe_media_path;**仅 DB 查询**路径(progress:2996、history:3174/3188、report:3240、agent:2173)均 os.path.basename 包裹、不触碰文件系统;cleanup_cache(:3003)不接受用户文件名,仅 listdir 固定目录 + realpath 前缀守卫 + 扩展名白名单。无遗漏。

### [P3][硬化] H1 瓦片非 RGB 模式(RGBA/P 以外)保存 JPEG 会崩
- 状态:open
- 位置:map_api/utils/get_satellite_image.py:88
- 现象:`if img.mode in ('RGBA','P')` 只覆盖两种;LA/LA 等模式到 _save_jpeg_atomic 抛异常(被重试吞掉后整轮失败)
- 修复:改为 `if img.mode != 'RGB'` 统一转换(Phase 2 顺带)

## P1 — 可靠性/正确性/并发

### [P1][可靠性] R3 错误响应形状不一致
- 状态:**closed**(2026-07-21, Phase 4)
- 修复:全项目响应约定确认为 `{code, msg, data}` + HTTP 状态码语义一致;补齐 13 处"code≠200 但 HTTP 仍 200"的 status=(ai_query 400/404/500、geo_search 400/500×2、progress 404、history 400×4、report 500 等);限流 429 同时带 `ok:false` 兼容字段。前端按 JSON code 判断、fetch 不因 4xx 抛错,改动零破坏(旧测试断言已同步更新)。

### [P1][可靠性] R7 6 个外部依赖失败行为无 mocked 测试
- 状态:**closed**(2026-07-21, Phase 4)
- 修复:FaultMatrixTests 8 例——Mapbox 持续 429→重试耗尽清晰异常;Earth Search STAC 500→HTTPError 传播;TiTiler 非图片→ValueError(触发候选回退);DashScope 配额 429→500 JSON 带 `Throttling` 原因;DeepSeek 非 JSON→ValueError;高德缺 key→立即失败、查无行政区→清晰错误;RemoteCLIP 打分器异常→`_ranker=heuristic` 静默回退。

### [P1][可靠性] R-3 后台任务异常路径
- 状态:**closed**(2026-07-21, Phase 4)
- 修复:BackgroundTaskFaultTests(TransactionTestCase,因后台线程走独立连接读不到 TestCase 未提交事务)验证:下载线程抛异常 → DownloadTask 5 秒内落 `error` + error_message 含原因。

### [P2][文档] R-4 额度成本封顶
- 状态:open → Phase 9 写入 CLAUDE.md
- 实测上限:单次 AI 查询 = stage1 VL 1 次 + 放大重查 ≤2 次 + 可选 self_check 1 次 = **VL ≤4 次/查询**;Agent 会话另加 DeepSeek 3 次(槽位/计划/复核)+ 内嵌 1 次 AI 查询。限流默认 AI scope 30/min·IP 已封顶最坏烧额度速度。

### [P1][并发] R4 gunicorn workers=2 + 后台线程 → 僵尸 downloading 任务
- 状态:**closed**(2026-07-21, Phase 5)
- 修复:新增 `manage.py cleanup_stale_tasks [--minutes N] [--dry-run]`——超时未更新的 downloading 任务标 error(附"重新框选"提示)+ 清内存进度陈旧条目;4 个测试(僵尸标记/新任务不动/done 不动/dry-run 不改)。决策 D2:保持 workers=2(进度已 DB 化),DEPLOY.md 将加"部署后/定时执行本命令"说明。

### [P1][并发] R5 内存 _download_progress 与 DB 双轨
- 状态:**closed**(2026-07-21, Phase 5,审计通过不改代码)
- 审计结论:双轨**设计一致**——内存字典同 worker 热路径(每瓦片更新,progress_lock 保护),DB 经 progress_callback 逐瓦片镜像(`_persist_progress` + close_old_connections),跨 worker 轮询走 DB 回退(get_progress:2991)。两轨状态机相同(downloading/done/partial/error)。无需删轨。
- 另审计通过(P-3):ThreadPoolExecutor 用 with 上下文(无泄漏),粘贴在主线程 as_completed 顺序进行(避 PIL 画布竞态)。
- P-4 SQLite WAL:settings 经 connection_created 信号启用 `journal_mode=WAL` + `synchronous=NORMAL`,消除后台线程写 + Web 读的锁竞争。

### [P1][正确性] R6 主动感知坐标链缺端到端数值测试
- 状态:**closed**(2026-07-21, Phase 3)
- 修复:新增 CoordinateChainEndToEndTests——2000×1500 合成影像 + 手算精确值,验证 stage1(1024×768)→ ×ap_scale(2000/1024)→ 原图 bbox[1171,917,1328,1082] → cut_image_geom 扩 512 → orig_box(993,743,1505,1255) → 裁剪图 bbox 回溯[1193,943,1307,1057] → pixel_bbox_to_geo **精确回到 (108.25, 22.6)**(places=9);GSD 测量 228m×228m/51984m²;另有 >3584 缩小落盘的中心回映测试。
- 验证:test map_api 全绿

### [P1][正确性] C2 normalize_model_answer 对抗样本
- 状态:**closed**(2026-07-21, Phase 3)
- 修复:8 个对抗测试(空串/None/纯空白/裸文本/think-only/多 answer 标签取首个/带空白),兜底行为全部确定。

### [P1][正确性] C3 前后端契约对照表
- 状态:**closed**(2026-07-21, Phase 3,零不一致)
- 核验:逐字段对照 browser.js 读取 vs 后端 payload——
  | 端点 | 前端读取字段 | 后端返回 | 结论 |
  |---|---|---|---|
  | get-img / get-sentinel-img | file_name,total_tiles,gsd_m,area_km2,scene_id,scene | 同 | ✅ |
  | progress | done,failed,status∈{done,partial,error} | 内存/DB 双轨状态一致(get_satellite_image:229 置 partial) | ✅ |
  | agent sessions | id,status,timeline,observer,artifacts.{ndwi,final_answer,report,waiting,image_url,scene,bbox} | agent_session_payload + artifacts 键全对 | ✅ |
  | ai query-region | answer,active_stages,targets[],scene,analysis_method | 同 | ✅ |
  | recommend-source | recommendation.{alignment,recommended_label,action} | 同 | ✅ |
  | geo search | name,display_name,lat,lon | 同(display_name 恒为字符串,前端 \|\| 安全) | ✅ |
  | history list / detail | list: id/scene/image_file/spatial_context/时间;detail: +messages/bbox/scene_id | 同 | ✅ |
  | report generate | download_url,file_name | 同 | ✅ |

### [P1][正确性] C4 报告链边界
- 状态:**closed**(2026-07-21, Phase 3)
- 修复:新增对比模式(`__compare__` 无图)+ 超长回答(1500+ 字)+ emoji 的 docx 生成测试。

### [P1][正确性] C5 已知局限登记
- 状态:open → Phase 9 写入 CLAUDE.md
- 内容:haversine/bbox 数学在极地(高纬度面积缩小)与跨 180° 经线区域不成立;产品已通过地图 maxBounds[[-10,70],[65,140]] 将交互限制在中国范围,但 API 层未显式拒绝跨界 bbox——文档登记为已知限制。

### [P1][可靠性] R7 6 个外部依赖失败行为无 mocked 测试
- 状态:open
- 修复:(Phase 4,故障矩阵)

## P2 — 工程/文档

### [P2][工程] R8 无 CI
- 状态:**closed**(2026-07-21, Phase 8)
- 修复:新增 `.github/workflows/ci.yml`(ubuntu + Python 3.11 + requirements.txt + `test map_api` + 默认 `smoke_pipeline`,全程无外部依赖/无需 key)。commit/push 需经用户允许,推送后自动生效。

### [P2][结构] Phase 7 views.py 拆分(D3 已执行)
- 状态:**closed**(2026-07-21)
- 结果:views.py **3463 → 1541 行(-55%)**,拆为 6 个模块:
  | 模块 | 行数 | 职责 |
  |---|---|---|
  | views.py | 1541 | HTTP 层 + AI 分析管线 + 报告生成 |
  | orchestrator.py | 637 | RemoteSensingAgent 编排(槽位→定位→检索→NDWI→VL→复核) |
  | sentinel_pipeline.py | 706 | Sentinel-2 选景/渲染回退链/马赛克/缓存 |
  | payloads.py | 466 | 质量/可信度/源推荐/场景/历史等 payload 构造 |
  | geo_math.py | 239 | bbox/GSD/no-data 裁边等纯数学(零 Django 依赖) |
  | media_paths.py | 29 | SAVE_DIR/REPORT_DIR 唯一定义 + safe_media_path |
- 兼容性:views 按原名全部再导出,`from map_api.views import X` 零破坏;测试仅 1 处 patch 目标随搬家更新(find_cached_sentinel_scene→sentinel_pipeline);被 patch 的可替换名(resolve_district_bbox/compute_ndwi_*/call_deepseek 等)在 orchestrator 内经 views 模块运行时查找,patch 语义不变。
- 验证:每搬一个模块跑一次全量测试;终验 162 tests OK + smoke 默认/--agent 双绿。

### [P2][文档] R9 CLAUDE.md 漂移 4 处 + R11 LICENSE
- 状态:**closed**(2026-07-21, Phase 9)
- CLAUDE.md 修正:① API 表补 `/design/`、`/api/satellite/cleanup/`、`/api/imagery/search/`;② Constraints 章 Sentinel 描述由"单选最佳候选、不做月度合成"改为真实"单景优先 + 贪心覆盖马赛克回退、非辐射月度合成";③ Agent 段补"DeepSeek 计划 + `deterministic_extract_slots`/`merge_agent_slots` 规则混合槽位";④ 测试描述由"pure-function unit tests"改为 162 测试含 mocked 集成;另补模块拆分结构图、限流/WAL/日志轮转/清理命令说明、响应约定与再导出契约约束、"已知局限"小节(极地/180° 经线、马赛克性质、单次会话成本上限)。
- README 修正 3 处:目录树(views 1.5k + 6 新模块 + design 页)、默认值归属(sentinel_pipeline/orchestrator/middleware)、Sentinel 函数归属。
- LICENSE:新增 MIT(D5 默认;占位版权人 SatelliteSense Contributors,可随时改)。
- DEPLOY.md:补 RATELIMIT_* 环境变量、部署后/定时 `cleanup_stale_tasks`+`cleanup_media` 维护章节与 cron、安全加固建议(限流非访问控制→边缘 allow/deny 或 basic auth;HTTP→TLS 建议)。

### [P2][资源] R10 media 无自动清理 + 日志无轮转
- 状态:**closed**(2026-07-21, Phase 6)
- 修复:抽 `cleanup_media_core` 为共享核心,新增 `manage.py cleanup_media`(与 `POST /api/satellite/cleanup/` 同逻辑,支持 --age/--all/--dry-run);日志 `FileHandler`→`RotatingFileHandler`(10MB×5);RemoteCLIP 审计通过(三态懒单例、CUDA→CPU、异常回退,首次加载延迟与显存常驻为设计取舍,已记入文档);design/home 页补 `<link rel=preconnect>` 加速 CDN 字体首连;Leaflet1.9.4/GSAP3.12.5/Three0.160.0 均已锁版本。

### [P3][前端] Phase 10 逐屏截图审查(桌面+移动,三页)
- 状态:**closed**(2026-07-21)
- 审查方法:Claude Browser 预览逐页截图 + 控制台错误扫描 + scrollWidth 溢出检测 + 计算样式核查;预览面板物理宽 452px(无法模拟宽桌面,但布局用 clamp 平滑缩放,已重点验证更难的窄屏)。
- 发现并修复 5 处 UI/UX 缺陷:
  1. 首页 hero 标题窄屏被裁切(`clamp` 下限 64px 在 <720 视口溢出)→ 移动端改 `clamp(32px,12vw,60px)`,同时修正 452–720 中间宽度的裁切。
  2. 首页顶栏 CTA"进入工作台"窄屏折行 → `.nav-cta` 加 `white-space:nowrap` + 移动端 nav 收窄。
  3. 工作台窄屏三栏叠层重叠(桌面优先工具的退化态)→ 窄屏"手风琴":默认收起工作区、展开一栏自动收起另一栏 + resize 重算(桌面布局不受影响)。
  4. design 页水平溢出 20px(`#stars` canvas 替换元素 `inset:0` 不拉伸、用了 innerWidth*dpr 固有宽;极光光晕越界)→ canvas 显式 `width/height:100%` + `html{overflow-x:clip}`(不破坏 sticky/纵向滚动)。
  5. 工作台 AI 回答 markdown 中文标题 line-height 1.5 偏紧 → 1.7(符合 CJK 排版纪律)。
- 健壮性增强 2 项:① Leaflet CDN 不可达时地图区给可读提示而非整页脚本崩溃留空白地图;② 改动静态资源全部升版本号(home.css v4 / browser.js v24 / browser.css v24 / design.css v2)确保缓存客户端生效。
- 审计通过项:三页零控制台错误;CJK 字体栈无禁用字体(微软雅黑/宋体),正文 Noto Sans SC、base line-height 1.7、各正文块 1.7–1.9;错误态 UX(toast + status 文案)与轮询清理(agentPollTimer/pollTimer/_pollTimer 成对 clear)经代码核对完整;CDN 版本均锁定。
- 状态:open
- 位置:CLAUDE.md Constraints("single candidate")、API 表(缺 cleanup/imagery-search/design 路由)、Agent 段(槽位为 DeepSeek+规则混合)、测试描述(实为 120 个含集成测试)
- 修复:(Phase 9)

### [P2][资源] R10 media/ 无自动清理;日志无轮转
- 状态:open
- 位置:media/(基线 16MB/38 文件)、settings.py:146 LOGGING
- 修复:(Phase 6)

### [P2][合规] R11 无 LICENSE;attribution 待审
- 状态:open
- 修复:(Phase 9)
