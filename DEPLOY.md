# Deploy SatelliteSense To Alibaba Cloud ECS

This guide is based on the already verified deployment notes in
`D:\Projects\AAAprojects\ERP\README.md`.

Important server reality:

- ECS login: `root@101.200.128.20`
- Local SSH key path: `D:\Projects\AAAprojects\ERP\.codex-ssh\huixianglian_deploy_ed25519`
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
$key = 'D:\Projects\AAAprojects\ERP\.codex-ssh\huixianglian_deploy_ed25519'
ssh -i $key root@101.200.128.20 "echo SSH_OK && whoami && hostname"
```

All future deployments for this server should use this SSH style.

## 2. Package And Upload Source

This follows the ERP deployment style: package locally, upload the tarball, then
extract on the server.

```powershell
$key = 'D:\Projects\AAAprojects\ERP\.codex-ssh\huixianglian_deploy_ed25519'
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
  --exclude='.playwright-mcp' `
  -czf $archive .

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
pip install -r requirements-prod.txt
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
DEEPSEEK_API_KEY=replace-me
AMAP_KEY=replace-me
TITILER_ENDPOINT=https://titiler.xyz

SENTINEL_MIN_COVERAGE_RATIO=0.92
SENTINEL_MIN_VALID_IMAGE_RATIO=0.88
SENTINEL_MAX_MOSAIC_CANDIDATES=6
AGENT_SENTINEL_CANDIDATE_LIMIT=15
EOF
chmod 600 .env
```

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
python manage.py collectstatic --noinput
python manage.py check
python manage.py smoke_pipeline
```

Optional live checks:

```bash
python manage.py smoke_pipeline --live-mapbox
python manage.py smoke_pipeline --live-sentinel
python manage.py smoke_pipeline --live-ai
python manage.py smoke_pipeline --agent
```

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
systemctl daemon-reload
systemctl enable satellitesense
systemctl restart satellitesense
systemctl status satellitesense --no-pager
```

Logs:

```bash
journalctl -u satellitesense -f
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
Invoke-WebRequest -Uri 'http://101.200.128.20/' -UseBasicParsing
Invoke-WebRequest -Uri 'http://101.200.128.20/showcase/' -UseBasicParsing
```

Expected:

- `:8083/api/system/health/` returns SatelliteSense health JSON.
- `/` still returns the existing ImageFlow site.
- `/showcase/` still returns the ERP showcase.

## 11. Updating Later

Repeat the local package/upload step, then run on the ECS:

```bash
cd /opt/satellitesense
source .venv/bin/activate
pip install -r requirements-prod.txt
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check
chown -R www-data:www-data /opt/satellitesense
systemctl restart satellitesense
nginx -t && systemctl reload nginx
```

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

### Agent unavailable

```bash
curl http://127.0.0.1:8010/api/system/health/
```

Verify `.env` contains:

```text
DEEPSEEK_API_KEY
DASHSCOPE_API_KEY
AMAP_KEY
```

### Sentinel imagery fails

Run:

```bash
cd /opt/satellitesense
source .venv/bin/activate
python manage.py smoke_pipeline --live-sentinel
```

The project intentionally rejects Sentinel scenes with insufficient coverage or
large no-data black edges. This is safer than showing broken mosaics.
