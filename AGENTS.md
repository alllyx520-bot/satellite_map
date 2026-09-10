# SatelliteSense 项目协作规则

## 前端基线

- 用户于 2026-09-09 明确要求保留服务器当前部署版本的前端风格和大致结构；此要求优先于旧目标文档中的 Apple 风格重设计方案。
- 基线为现有 `templates/home.html`、`templates/browser.html` 及 `static/spectra.css`、`static/home.css`、`static/browser.css`：深色材质、金色点缀、原有字体层级与控件样式。
- 保留首页及地图工作台的基本结构：顶部工具栏、中央地图、左侧调查 Agent、右侧区域工作区。新增运行状态、计划、证据和控制功能应融入现有面板。
- `/agent/` 与 `/workbench/` 复用地图工作台，不另起浅色三栏页面，不用换主题代替功能集成。
- 前端修改需检查实际渲染；线上基线有变化时重新核对。不得因后端重构顺带重做视觉设计。

## 前端迭代环境坑（2026-09-09 实测）

- 本地 runserver 默认 `DJANGO_DEBUG=false`：模板被进程缓存、`/static/` 由 `staticfiles/`（STATIC_ROOT）提供。改模板必须重启进程，改 CSS/JS 必须 `manage.py collectstatic`，否则页面不更新。
- 纯 UI 截图迭代用 `DJANGO_DEBUG=1` 启动（模板与 static/ 实时生效），交付前再按 README 校验命令复核。
- 截图工具：`scripts/shot_ui.py <tag> [home,workbench,design]`（Python Playwright + msedge，输出到 `output/ui-review/`）。