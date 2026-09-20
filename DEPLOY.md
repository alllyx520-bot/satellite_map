# Deploy SatelliteSense To Alibaba Cloud ECS

This guide is based on the already verified deployment notes in
`D:\Projects\AAAprojects\ERP\README.md`.

Important server reality:

- ECS login: `root@101.200.128.20`
- Local SSH key path: `D:\Projects\AAAprojects\ERP\private\showcase-ssh\huixianglian_deploy_ed25519`
- Existing root site: `http://101.200.128.20/`
- Existing ERP showcase: `http://101.200.128.20/showcase/`
- Existing nginx config on server: `/etc/nginx/conf.d/imageflow.conf`

Do not overwrite `/` or `/showcase/`. SatelliteSense is deployed as an
independent site on:

```text
http://101.200.128.20:8083/
```

The external nginx port is `8083`; Gunicorn stays internal on
`127.0.0.1:8010`.

> **本指南的主体（§1–§13）写于 V2 legacy 工作台时期。** 当前仓库的 `/`、`/workbench/`、
> `/agent/` 由 **V3**（React 前端 + `map_api/v3/` harness）提供，旧页面退到 `/legacy/`。
> V3 的部署增量集中在 **§4（依赖）、§4b（前端构建）、§5（环境变量）、§8（worker）、§10（验收）**，
> 这几节已按 V3 现状更新；其余步骤（SSH、打包、nginx、systemd 框架）仍然有效。
> 未做生产部署前不要按 §14 之前的旧结论判断 V3 已上线。

**V3 与 V2 部署差异速查**

| 项 | V2 legacy | V3（当前 `/`） |
|---|---|---|
| Python 依赖 | `requirements-prod.txt` | 需加 `rasterio`/`pyproj`/`shapely`/`scipy`（见 §4） |
| 前端 | 无构建步骤 | **必须先 `npm ci && npm run build`**，否则 `/` 返回 503 未构建页 |
| 后台进程 | `run_agent_worker` | 需**另有** `run_v3_worker`（两个 unit 都在 `deploy/`，必须都启用，见 §8） |
| API | `/api/*`、`/api/v2/agent/runs/*` | `/api/v3/*` |
| 附件存储 | `media/satellite_imgs` | `media/v3-assets`（无自带清理命令，需自行监控容量） |

生产 Agent 的模型配置：主控 `DEEPSEEK_API_KEY`（`deepseek-flash`，原生工具调用）；
视觉 `DASHSCOPE_API_KEY`（基线 `qwen3-vl-plus`，可用 `V3_VISION_MODEL` 覆盖）。
缺失 `DEEPSEEK_API_KEY` 时 V3 会直接失败——**没有纯规则降级**。旧链路可用
`AGENT_PROVIDER=glm` + `GLM_API_KEY` 回退，仅影响 V2 legacy Agent。

V2 会话事件可通过 `/api/agent/sessions/<id>/events/?after=<cursor>` 断线续传；
V3 会话事件走 `/api/v3/conversations/<id>/events/?after=<sequence>`（SSE，`?format=json` 取游标页）。

Never commit, upload to git, paste, or print the private key content. It is OK
to use the key path in local commands.

## 0. Security Group

In Alibaba Cloud ECS security group, allow inbound:

```text
22/tcp    SSH
8083/tcp  SatelliteSense website
```

Keep the existing `80/tcp` site untouched.

The ECS also uses `firewalld`. After SSH login, make sure the server firewall
allows the same port:

```bash
firewall-cmd --add-port=8083/tcp --permanent
firewall-cmd --reload
firewall-cmd --zone=public --list-ports
```

If `127.0.0.1:8083` works on the server but
`http://101.200.128.20:8083/` times out from your own computer, the usual root
cause is that the Alibaba Cloud security group is not open yet.

## 1. Test SSH

From Windows PowerShell:

```powershell
$key = 'D:\Projects\AAAprojects\ERP\private\showcase-ssh\huixianglian_deploy_ed25519'
ssh -i $key root@101.200.128.20 "echo SSH_OK && whoami && hostname"
```

All future deployments for this server should use this SSH style.

## 2. Package And Upload Source

> **仅限首次安装。** 下面的 `rm -rf /opt/satellitesense` 会连同服务器的 `.env`、`db.sqlite3`、
> `.venv`、`media/` 一起删除，而这些都不在压缩包里（见下方 `--exclude`），删掉不会自动恢复。
> **更新既有部署请直接跳到 §11**，不要执行本节。

This follows the ERP deployment style: package locally, upload the tarball, then
extract on the server.

```powershell
$key = 'D:\Projects\AAAprojects\ERP\private\showcase-ssh\huixianglian_deploy_ed25519'
$src = 'D:\Projects\AAAprojects\satellite_mapV2'
$deployDir = 'D:\Projects\AAAprojects\satellite_mapV2\.deploy'
$archive = "$deployDir\satellitesense.tar.gz"

if (!(Test-Path -LiteralPath $deployDir)) {
  New-Item -ItemType Directory -Path $deployDir | Out-Null
}
if (Test-Path -LiteralPath $archive) {
  Remove-Item -LiteralPath $archive -Force
}

tar -C $src `
  --exclude='.git' `
  --exclude='.idea' `
  --exclude='.vscode' `
  --exclude='.env' `
  --exclude='db.sqlite3' `
  --exclude='design-qa.md' `
  --exclude='media' `
  --exclude='models' `
  --exclude='output' `
  --exclude='page-snap.json' `
  --exclude='test-results' `
  --exclude='staticfiles' `
  --exclude='.deploy' `
  --exclude='.codex-runtime' `
  --exclude='.research' `
  --exclude='.playwright-mcp' `
  --exclude='frontend/node_modules' `
  --exclude='frontend/*.tsbuildinfo' `
  -czf $archive .

> **不要排除 `static/v3/`。** 它是 Vite 构建产物（gitignored，但必须随包上传），少它等于没有前端。
> `frontend/node_modules` 一定要排除（体积大，可由 `npm ci` 重建）。

ssh -i $key root@101.200.128.20 "rm -rf /opt/satellitesense && mkdir -p /opt/satellitesense"
scp -i $key $archive root@101.200.128.20:/tmp/satellitesense.tar.gz
ssh -i $key root@101.200.128.20 "tar -xzf /tmp/satellitesense.tar.gz -C /opt/satellitesense && rm -f /tmp/satellitesense.tar.gz"
```

## 3. Install Server Packages

On the ECS:

```bash
dnf install -y nginx gcc gcc-c++ make openssl-devel bzip2-devel libffi-devel zlib-devel sqlite-devel python3 python3-pip
```

Django 5.2 needs Python 3.10 or newer:

```bash
python3 --version
```

If system Python is too old, install Miniconda and adjust the systemd service
`ExecStart` to the conda Gunicorn path. The default deployment assumes:

```text
/opt/satellitesense/.venv/bin/gunicorn
```

## 4. Create Python Environment

On the ECS:

```bash
cd /opt/satellitesense
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
# V3 需要 rasterio/pyproj/shapely/scipy/psycopg：
#   requirements-v3.txt = -r requirements.txt + 上述包
# 只要 V2 legacy 时可用 requirements-prod.txt（更小），但 /  会因缺 rasterio 报错。
pip install -r requirements-v3.txt
```

`rasterio` 在部分平台需要额外系统库（`libexpat`、`gdal` 视 wheel 而定）；若 `pip install`
报编译错误，优先确认 Python 版本与 wheel 匹配（本机验证组合：Python 3.11 + rasterio 1.4.4）。

## 4b. Build The Frontend (V3 only)

`/`、`/workbench/`、`/agent/` 由 V3 React 前端提供；Django **只服务构建产物 `static/v3/`**，
不读 `frontend/src/`。任一前端源码改动后都必须重建，否则页面停在旧版本或直接 503。

推荐在**本地**构建后连产物一起打包（§2 的 tar 已包含 `static/v3/`）：

```powershell
npm --prefix frontend ci
npm --prefix frontend run build      # 产出 static/v3/ 与 static/v3/.vite/manifest.json
```

若要在服务器上构建，需额外安装 Node：

```bash
dnf install -y nodejs npm
cd /opt/satellitesense && npm --prefix frontend ci && npm --prefix frontend run build
```

校验产物存在（缺它 `/` 会返回 503 的"前端尚未构建"页）：

```bash
test -f /opt/satellitesense/static/v3/.vite/manifest.json && echo BUILD_OK
```

## 5. Configure Environment Variables

Create `/opt/satellitesense/.env` on the server:

```bash
cd /opt/satellitesense
cat > .env <<'EOF'
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_ALLOWED_HOSTS=101.200.128.20,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://101.200.128.20:8083
CORS_ALLOW_ALL_ORIGINS=true

MAPBOX_TOKEN=replace-me
DASHSCOPE_API_KEY=replace-me
# Agent 控制器默认 DeepSeek-V4.1-Flash(2026-09-11 切换)
DEEPSEEK_API_KEY=replace-me
AGENT_MODEL=deepseek-flash
DEEPSEEK_CHAT_URL=https://api.deepseek.com/v1/chat/completions
# 回退旧链路(可选):AGENT_PROVIDER=glm + 下行两项
# GLM_API_KEY=replace-me
# GLM_CHAT_URL=https://open.bigmodel.cn/api/paas/v4/chat/completions
AMAP_KEY=replace-me
TITILER_ENDPOINT=https://titiler.xyz
# 可选数据源 key(2026-09-10;留空则仅对应功能不可用)
FIRMS_MAP_KEY=
TIANDITU_KEY=

SENTINEL_MIN_COVERAGE_RATIO=0.92
SENTINEL_MIN_VALID_IMAGE_RATIO=0.88
SENTINEL_MAX_MOSAIC_CANDIDATES=6
AGENT_SENTINEL_CANDIDATE_LIMIT=15

# IP rate limiting (protects paid API endpoints; defaults shown)
RATELIMIT_API_PER_MINUTE=120
RATELIMIT_AI_PER_MINUTE=30
RATELIMIT_BACKEND=database
# RATELIMIT_DISABLED=1   # uncomment only for offline demos
# Agent 长任务交给持久化 worker；不要依赖 Web 进程 daemon thread
AGENT_EXECUTION_MODE=queue

# ---- V3（/ 与 /workbench/ 的实际后端）----
# 视觉角色；不设时基线为 qwen3-vl-plus（经评测晋升后才覆盖）
V3_VISION_MODEL=qwen3-vl-plus
# Python 分析执行环境：local（默认，项目 Python 子进程，非安全隔离边界）| docker
V3_PYTHON_RUNTIME=local
# COG 分块下载走代理。实测直连 us-west-2/Azure 仅 0.3–0.5 MB/s，经本地代理约 1.3 MB/s；
# 服务器能直连且带宽正常时留空。
# V3_COG_PROXY=http://127.0.0.1:7897
# V3_COG_PROXYUSERPWD=
# 对大 Range 响应会被截断的网络启用：
# COG_READ_MODE=small_ranges
# 启用 PostgreSQL/PostGIS（迁移 0027 建空间列 + GiST 索引）；默认本地 SQLite
# V3_DATABASE_ENGINE=postgis
# PGHOST=127.0.0.1
# PGPORT=5432
# PGDATABASE=satellitesense
# PGUSER=satellitesense
# PGPASSWORD=replace-me
# 跨日期 Sentinel 覆盖拼接默认拒绝；确认业务需要时才显式开启
# SENTINEL_ALLOW_CROSS_DATE_MOSAIC=1
EOF
chmod 600 .env
```

`RATELIMIT_BACKEND=database` 使用共享令牌桶，Gunicorn 多 worker 不会各自放大额度。
客户端标识以 `DJANGO_SECRET_KEY` 做 HMAC 后入库，不保存原始 IP。数据库临时不可用时
会记录 warning 并降级到进程内限流，不会因此让全部业务接口返回 500。

该设置同时覆盖 Agent 调查、Mapbox 影像下载和 Agent Word 报告。报告任务使用
数据库中的 `ReportJob` 持久化，下载使用 `DownloadTask` 租约和临时文件原子发布；
worker 异常退出后会按 `--claim-timeout` 接管。不要把
生产值改回 `thread`，否则 Web reload 会中断进程内任务。

Generate a Django secret if needed:

```bash
/opt/satellitesense/.venv/bin/python - <<'PY'
from django.core.management.utils import get_random_secret_key
print(get_random_secret_key())
PY
```

Do not copy the local `.env` into git or chat.

## 6. Initialize Django

```bash
cd /opt/satellitesense
source .venv/bin/activate
python manage.py migrate
python manage.py makemigrations --check --dry-run   # 期望 "No changes detected"
python manage.py collectstatic --noinput
python manage.py check
python manage.py smoke_pipeline                     # 默认全程 mock，不烧配额
```

> V3 的 `static/v3/` 由 nginx 直接提供，`collectstatic` 不处理它——它来自 §4b 的前端构建。

Optional live checks:

```bash
python manage.py smoke_pipeline --agent             # 被 mock 的 Legacy Agent 循环
python manage.py smoke_pipeline --contract-change
python manage.py smoke_pipeline --live-mapbox
python manage.py smoke_pipeline --live-sentinel
python manage.py smoke_pipeline --live-sentinel1
python manage.py smoke_pipeline --live-landsat
python manage.py smoke_pipeline --live-ai
python manage.py smoke_pipeline --live-firms      # 需 FIRMS_MAP_KEY
python manage.py smoke_pipeline --live-esri
python manage.py smoke_pipeline --live-tianditu   # 需 TIANDITU_KEY
```

`smoke_pipeline` 只覆盖 legacy 核心闭环，**不覆盖 V3**；V3 的验收在 §10 用真实页面走一遍。

## 7. Test Gunicorn Internally

```bash
cd /opt/satellitesense
source .venv/bin/activate
gunicorn satellite_map.wsgi:application --bind 127.0.0.1:8010 --workers 2 --timeout 180
```

In another SSH terminal:

```bash
curl http://127.0.0.1:8010/api/system/health/
```

Stop the manual Gunicorn process with `Ctrl+C`.

## 8. Install systemd Service

```bash
id www-data || useradd --system --no-create-home --shell /sbin/nologin www-data
chown -R www-data:www-data /opt/satellitesense
cp /opt/satellitesense/deploy/satellitesense.service /etc/systemd/system/satellitesense.service
cp /opt/satellitesense/deploy/satellitesense-agent-worker.service /etc/systemd/system/satellitesense-agent-worker.service
cp /opt/satellitesense/deploy/satellitesense-v3-worker.service /etc/systemd/system/satellitesense-v3-worker.service
systemctl daemon-reload
systemctl enable satellitesense
systemctl enable satellitesense-agent-worker
systemctl enable satellitesense-v3-worker
systemctl restart satellitesense
systemctl restart satellitesense-agent-worker
systemctl restart satellitesense-v3-worker
systemctl status satellitesense --no-pager
systemctl status satellitesense-agent-worker --no-pager
systemctl status satellitesense-v3-worker --no-pager
```

> **三个 unit 缺一不可，V3 worker 尤其不能少，否则 `/` 提的问永远不会被执行。**
> - `satellitesense.service` — Gunicorn（Web）。
> - `satellitesense-agent-worker.service` — **V2**：`manage.py run_agent_worker`，处理旧
>   `AgentSession`/报告任务/下载任务。
> - `satellitesense-v3-worker.service` — **V3**：`manage.py run_v3_worker`，处理附件瓦片并执行
>   `/api/v3/` 创建的 `AgentRun`（`execution_engine="harness"`）。
>
> 两条 worker 链路消费的是不同的队列（V2 只认 `execution_engine="dag"` 且需 `AgentSession`
> 关联），**互不代替**；只跑其中一个，另一代的提问就会一直停在队列里。
> `run_v3_worker` 默认 2 个会话并发 + 2 个附件并发，可通过 `--conversations N` 调整。
> worker 被强杀后，在途运行的租约过期会被后续 worker 接管（无需人工清理）。

> ⚠️ **unit 文件必须是 LF 行尾**。仓库未配置 `.gitattributes` 而 `core.autocrlf=true`，
> 若某个 unit 在工作区变成 CRLF，systemd 会以"Invalid argument"之类的原因拒绝启动。
> 打包前确认（三个都应输出 `LF`）：
>
> ```bash
> for f in deploy/*.service; do printf "%s: " "$f"; grep -q $'\r' "$f" && echo CRLF || echo LF; done
> ```

Logs:

```bash
journalctl -u satellitesense -f
journalctl -u satellitesense-v3-worker -f
```

## 9. Configure nginx

```bash
cp /opt/satellitesense/deploy/nginx-satellitesense.conf /etc/nginx/conf.d/satellitesense.conf
nginx -t
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx
```

Open:

```text
http://101.200.128.20:8083/
```

Health check:

```text
http://101.200.128.20:8083/api/system/health/
```

## 10. Post-deploy Validation

Validate SatelliteSense and also confirm existing ERP/ImageFlow routes were not
overwritten:

```powershell
Invoke-WebRequest -Uri 'http://101.200.128.20:8083/api/system/health/' -UseBasicParsing
Invoke-WebRequest -Uri 'http://101.200.128.20:8083/api/v3/capabilities/' -UseBasicParsing
Invoke-WebRequest -Uri 'http://101.200.128.20:8083/' -UseBasicParsing      # V3 工作台页面
Invoke-WebRequest -Uri 'http://101.200.128.20/' -UseBasicParsing           # 其他站点
Invoke-WebRequest -Uri 'http://101.200.128.20/showcase/' -UseBasicParsing  # ERP showcase
```

Expected:

- `:8083/api/system/health/` returns SatelliteSense health JSON.
- `:8083/api/v3/capabilities/` returns `models`/`sources`/`tools`/`execution` JSON
  （返回 404 或 HTML 说明部署版本里没有 V3）。
- `:8083/` returns the **V3 workbench** page. 若看到"前端尚未构建"页（HTTP 503），
  说明 §4b 的 `static/v3/` 没上传或没构建。
- `:8083/legacy/` returns the old landing page（V2 保留入口）。
- `/` still returns the existing ImageFlow site.
- `/showcase/` still returns the ERP showcase.

**V3 功能验收（`/api/v3/capabilities/` 通过之后）**：打开 `/` → 在左侧对话里提一个需要真实影像的问题
（例如"帮我看看这个区域的水体"）→ 确认出现工具活动、运行状态推进、最终答案带可点击证据。
若运行一直停在 `queued`，说明 §8 的 **V3 worker 没有在跑**。

## 11. Updating Later

更新**不要**重跑 §2 的 `rm -rf /opt/satellitesense`——它只适用于首次安装。
正确做法是「备份 → 覆盖解压 → 迁移 → 重启」，`.env` / `db.sqlite3` / `.venv` / `media/`
都不在压缩包里，覆盖解压不会碰到它们。

本地只跑 §2 里的 `tar` 打包和 `scp` 上传两条命令（跳过 `rm -rf`），然后在 ECS 上执行：

```bash
# 1) 先备份：关键资产（.env / db.sqlite3 / media）必须留在备份里，.venv 可重建故排除
cd /opt
tar --exclude='./satellitesense/.venv' -czf /opt/satellitesense-backup-$(date +%Y%m%d-%H%M%S).tar.gz satellitesense
tar -tzf $(ls -t /opt/satellitesense-backup-*.tar.gz | head -1) | grep -E 'satellitesense/(\.env|db\.sqlite3)$'  # 两条都必须列出

# 2) 记录校验和，覆盖解压后比对，证明关键资产未被触碰
cd /opt/satellitesense
md5sum .env db.sqlite3 | tee /tmp/pre.md5
tar -xzf /tmp/satellitesense.tar.gz -C /opt/satellitesense   # 注意：不 rm -rf
rm -f /tmp/satellitesense.tar.gz
md5sum -c /tmp/pre.md5

# 3) 依赖与数据
source .venv/bin/activate
pip install -r requirements-v3.txt
python manage.py makemigrations --check --dry-run     # 期望 No changes detected
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check

# 4) 重启（含 V3 worker；前端产物已随 §2 的 tar 一起上传）
chown -R www-data:www-data /opt/satellitesense
systemctl restart satellitesense satellitesense-agent-worker satellitesense-v3-worker
systemctl is-active satellitesense satellitesense-agent-worker satellitesense-v3-worker

# 5) nginx 配置若有改动：先备份、再 nginx -t 校验，通过才 reload（见 §9）
diff -u /etc/nginx/conf.d/satellitesense.conf /opt/satellitesense/deploy/nginx-satellitesense.conf
cp -a /etc/nginx/conf.d/satellitesense.conf /root/satellitesense.conf.bak-$(date +%Y%m%d-%H%M%S)
cp /opt/satellitesense/deploy/nginx-satellitesense.conf /etc/nginx/conf.d/satellitesense.conf
nginx -t && systemctl reload nginx

# 6) 重启会杀掉在途任务：标记下载失败、释放 Agent 租约，持久化 worker 会自动接手
python manage.py cleanup_stale_tasks --minutes 1

# 7) 验收
python manage.py smoke_pipeline          # 默认不调外部付费服务（不含 V3）
test -f /opt/satellitesense/static/v3/.vite/manifest.json && echo FRONTEND_OK
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8083/api/system/health/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8083/api/v3/capabilities/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8083/          # 期望 200（非 503）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/               # 其他站点未被影响
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/showcase/
```

回滚：`tar -xzf /opt/satellitesense-backup-<时间戳>.tar.gz -C /opt`，再 `systemctl restart`。

**注意 `collectstatic` 后要核对静态资源确实换了版本**：生产由 nginx 直接提供
`/static/`，模板里的 `?v=N` 与本地一致才算发布成功。可比对字节数：

```bash
# 本地: wc -c static/browser.css      远端: curl -s .../static/browser.css?v=34 | wc -c
```

V3 前端产物不受 `?v=N` 管：`templates/workbench_v3.html` 从 `manifest.json` 读取
**带内容哈希的文件名**，所以换版本时哈希自动变化，不需要手工升版号。核对方式是
比对 `manifest.json` 里的入口文件名与 `ls static/v3/assets`。

## 12. Maintenance

```bash
cd /opt/satellitesense
source .venv/bin/activate
# Mark stuck downloads error and release stale Agent leases for worker recovery:
python manage.py cleanup_stale_tasks --dry-run
python manage.py cleanup_stale_tasks --minutes 10
# satellitesense-agent-worker.service continuously scans and recovers released sessions.
# Delete media files older than 30 days (+ their DB rows); preview first:
python manage.py cleanup_media --age 30 --dry-run
python manage.py cleanup_media --age 30
# V3 worker 的租约由 worker 自己接管（harness 租约 120s），不需要 cleanup_stale_tasks
systemctl is-active satellitesense-v3-worker
```

**磁盘容量是这套部署最容易踩的坑**：`cleanup_media` 只清 `media/satellite_imgs`（legacy 影像）
和 `media/reports`，**不清 `media/v3-assets/`**（V3 附件原图、派生瓦片、观察预览）。
该目录只增不减——2026-09-14 在开发机上已到 2.3 GB，生产上按上传量线性增长。
当前没有对应命令，需自行监控并人工处理（例如按会话清理已归档会话的附件）。先看占用：

```bash
du -sh /opt/satellitesense/media/* | sort -h
```

Recommended cron (daily 04:17):

```cron
17 4 * * * cd /opt/satellitesense && .venv/bin/python manage.py cleanup_stale_tasks --minutes 10 && .venv/bin/python manage.py cleanup_media --age 30
```

Application logs rotate by themselves (`media/logs/*.log`, 10MB×5).

## 13. Security Hardening Notes

- All API endpoints are unauthenticated by design (single-user demo product). The
  built-in `RateLimitMiddleware` caps per-IP request rates (AI-scope 30/min by
  default) so a public deployment cannot burn unlimited paid API quota, but it is
  **not** access control. For anything beyond a demo, restrict access at the edge:
  nginx `allow`/`deny` or basic auth in front of `:8083`, or keep port 8083 out of
  the public security group and use an SSH tunnel / VPN.
- V3 资源按 Django session key（`owner_session_key`）隔离，跨会话访问一律 404——这防的是
  "A 看到 B 的会话"，**不是**访问控制：任何人仍能开新会话消耗你的模型额度。
  V3 上传单文件上限 1 GiB；公开部署前请按上一段在边缘层限制访问。
- The current deployment serves plain HTTP on 8083. If this becomes a lasting
  service, terminate TLS at nginx (certbot) and set `DJANGO_CSRF_TRUSTED_ORIGINS`
  to the `https://` origin.
- `GET /api/system/health/` reports whether keys are configured but never prints
  key values — safe to use as an uptime probe.

## Troubleshooting

### 502 Bad Gateway

```bash
systemctl status satellitesense --no-pager
journalctl -u satellitesense -n 120 --no-pager
curl http://127.0.0.1:8010/api/system/health/
```

### Static files missing

```bash
cd /opt/satellitesense
source .venv/bin/activate
python manage.py collectstatic --noinput
systemctl restart satellitesense
systemctl reload nginx
```

### DisallowedHost or CSRF failure

Check `/opt/satellitesense/.env`:

```env
DJANGO_ALLOWED_HOSTS=101.200.128.20,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://101.200.128.20:8083
```

Then:

```bash
systemctl restart satellitesense
```

### V3 workbench shows "前端尚未构建" (HTTP 503)

`static/v3/.vite/manifest.json` 不存在。本地跑 `npm --prefix frontend run build`，
确认 `static/v3/` 随 §2 的 tar 上传，或直接在服务器上构建（§4b）。

```bash
test -f /opt/satellitesense/static/v3/.vite/manifest.json && echo OK
ls /opt/satellitesense/templates/workbench_v3.html
```

### V3 提问后一直停在 queued / 没有任何工具活动

`AgentRun` 只由 `run_v3_worker` 消费。检查：

```bash
systemctl status satellitesense-v3-worker --no-pager
journalctl -u satellitesense-v3-worker -n 120 --no-pager
```

unit 不存在就按 §8 新建。若日志显示 `external_service_unavailable`，
先看是不是 DeepSeek/DashScope 余额或限流（属于外部服务问题，不是应用故障）；
`402` 类额度问题会在 run 上以该终态落库，恢复后重新提问即可。

### Legacy Agent unavailable（V2 `AgentSession`）

```bash
curl http://127.0.0.1:8010/api/system/health/
```

Verify `.env` contains:

```text
DEEPSEEK_API_KEY     # V2 主控（deepseek-flash）；缺失时 V2/V3 都直接失败
DASHSCOPE_API_KEY
AMAP_KEY
```

`GLM_API_KEY` 只在显式设置 `AGENT_PROVIDER=glm` 时作为旧链路回退，**不是**默认依赖。

### Sentinel imagery fails

Run:

```bash
cd /opt/satellitesense
source .venv/bin/activate
python manage.py smoke_pipeline --live-sentinel
```

The project intentionally rejects Sentinel scenes with insufficient coverage or
large no-data black edges. This is safer than showing broken mosaics.
