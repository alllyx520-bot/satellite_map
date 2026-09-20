# SatelliteSense V3 frontend

`npm install` installs the local development dependencies. Use `npm run dev` for the Vite development server, `npm run typecheck` for TypeScript validation, `npm test` for component-state tests, and `npm run build` to emit the Django-served bundle to `../static/v3/`.

The Django template must resolve Vite's `static/v3/.vite/manifest.json` and load the hashed JS/CSS entries. The placeholder template is intentionally limited to the V3 mount point so the server route can own manifest resolution.

## Facts worth knowing (2026-09-14)

- Stack: React 19 + TypeScript 5.7 + Vite 6 + zustand（状态）+ OpenLayers 10（地图）+ Radix UI + TanStack Query/Virtual + `@fontsource`（Inter / Noto Sans SC 本地字体）。
- `npm test` 当前 **24 passed / 4 files**（`store`、`geo`、`PythonArtifacts`、`ToolActivity`）；`npm run typecheck` 无错误。
- `static/v3/` 是**构建产物且被 gitignore**。`start.py` 会在缺 `static/v3/.vite/manifest.json` 时直接退出，Django 缺它时 `/` 返回 503 的"前端尚未构建"页——**改完 TS/CSS 必须 `npm run build`**，服务端不会读 `src/`。
- 模板 `templates/workbench_v3.html` 只从 manifest 注入入口 JS/CSS，产物文件名带内容哈希，因此换版本不需要手工升 `?v=`。
- `npm run dev` 时 Vite 把 `/api` 代理到本地 Django（默认 8000）。
- 相关后端：`map_api/v3/api.py` 的 `workbench` 视图负责解析 manifest；接口契约见根 [CLAUDE.md](../CLAUDE.md) 的"V3"一节。
