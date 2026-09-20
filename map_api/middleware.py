"""跨进程 IP 限流中间件:保护付费 API 端点免受公网滥用(R1)。

策略:
- 仅作用于 /api/ 前缀;页面与静态资源不限流;健康检查端点豁免。
- 高成本端点(AI 分析 / Agent / 报告生成)用更严的额度,其余 API 用宽松额度。
- 默认使用数据库共享令牌桶，多 Gunicorn worker 共用额度；数据库暂不可用时
  降级为进程内滑动窗口，避免限流故障拖垮全部 API。
- 环境变量:RATELIMIT_API_PER_MINUTE(默认 120)、RATELIMIT_AI_PER_MINUTE(默认 30);
  RATELIMIT_BACKEND=database(默认)或 memory；RATELIMIT_DISABLED=1 完全关闭
  (演示模式);值 ≤0 视为关闭该项。
"""
import hashlib
import hmac
import logging
import math
import os
import re
import threading
import time
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from .models import ApiRateLimitBucket


logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BUCKETS = {}  # (scope, ip) -> [monotonic 时间戳]
_WINDOW_SECONDS = 60.0
_MAX_BUCKETS = 4000
_LAST_DB_CLEANUP = 0.0

_AI_PATH_PREFIXES = (
    "/api/ai/query-region/",
    "/api/report/generate/",
)


def _is_expensive_request(request):
    """只把会触发模型/报告执行的写请求计入 AI 限额。

    Agent 状态 GET 会被前端周期性轮询，属于只读轻请求，不应消耗模型调用额度。
    """
    path = request.path
    if path.startswith('/api/v3/'):
        return request.method == 'POST' and bool(re.search(r'/(messages|actions)/?$', path))
    if any(path.startswith(prefix) for prefix in _AI_PATH_PREFIXES):
        return request.method in ("POST", "PUT", "PATCH")
    if path.startswith("/api/agent/sessions"):
        return request.method in ("POST", "PUT", "PATCH")
    return False


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _limit_for(scope):
    if scope == "ai":
        raw = os.environ.get("RATELIMIT_AI_PER_MINUTE", "30")
    elif scope == 'v3_raster':
        raw = os.environ.get('RATELIMIT_RASTER_PER_MINUTE', '3000')
    elif scope == 'v3_upload':
        raw = os.environ.get('RATELIMIT_UPLOAD_PER_MINUTE', '1200')
    elif scope == 'v3_read':
        raw = os.environ.get('RATELIMIT_V3_READ_PER_MINUTE', '360')
    else:
        raw = os.environ.get("RATELIMIT_API_PER_MINUTE", "120")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 30 if scope == "ai" else 120


def _client_ip(request):
    # 直接暴露公网时，客户端可以伪造 X-Forwarded-For；默认只信 Django
    # 看到的对端地址。只有明确配置了可信反向代理才解析该请求头。
    if _truthy(os.environ.get("TRUST_PROXY_HEADERS")):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def reset_rate_limit_state():
    """测试辅助:清空计数桶。"""
    global _LAST_DB_CLEANUP
    with _LOCK:
        _BUCKETS.clear()
        _LAST_DB_CLEANUP = 0.0
    try:
        ApiRateLimitBucket.objects.all().delete()
    except DatabaseError:
        # 测试数据库尚未迁移或启动早期时，内存状态仍应可重置。
        pass


def _memory_limit(scope, client_ip, limit):
    """数据库暂不可用时的单进程滑动窗口降级。"""
    key = (scope, client_ip)
    now = time.monotonic()
    with _LOCK:
        hits = [ts for ts in _BUCKETS.get(key, ()) if now - ts < _WINDOW_SECONDS]
        if len(hits) >= limit:
            _BUCKETS[key] = hits
            return False, max(1, int(_WINDOW_SECONDS - (now - hits[0])) + 1)
        hits.append(now)
        _BUCKETS[key] = hits
        if len(_BUCKETS) > _MAX_BUCKETS:
            stale = [k for k, v in _BUCKETS.items() if not v or now - v[-1] >= _WINDOW_SECONDS]
            for stale_key in stale:
                _BUCKETS.pop(stale_key, None)
    return True, 0


def _database_limit(scope, client_ip, limit):
    """跨进程令牌桶：容量=每分钟额度，按 limit/60 每秒补充。"""
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8", errors="replace"),
        client_ip.encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()[:32]
    bucket_key = f"{scope}:{digest}"
    now = timezone.now()
    with transaction.atomic():
        bucket, created = ApiRateLimitBucket.objects.select_for_update().get_or_create(
            bucket_key=bucket_key,
            defaults={
                "tokens": float(max(0, limit - 1)),
                "capacity": limit,
                "last_refill_at": now,
            },
        )
        if created:
            allowed, retry_after = True, 0
        else:
            elapsed = max(0.0, (now - bucket.last_refill_at).total_seconds())
            tokens = min(float(limit), float(bucket.tokens) + elapsed * (float(limit) / _WINDOW_SECONDS))
            bucket.capacity = limit
            bucket.last_refill_at = now
            if tokens >= 1.0:
                bucket.tokens = tokens - 1.0
                allowed, retry_after = True, 0
            else:
                bucket.tokens = tokens
                allowed = False
                retry_after = max(1, math.ceil((1.0 - tokens) * _WINDOW_SECONDS / float(limit)))
            bucket.save(update_fields=["tokens", "capacity", "last_refill_at", "updated_at"])
    _maybe_cleanup_database_buckets()
    return allowed, retry_after


def _maybe_cleanup_database_buckets():
    global _LAST_DB_CLEANUP
    now_mono = time.monotonic()
    with _LOCK:
        if now_mono - _LAST_DB_CLEANUP < 600:
            return
        _LAST_DB_CLEANUP = now_mono
    cutoff = timezone.now() - timedelta(days=1)
    try:
        ApiRateLimitBucket.objects.filter(updated_at__lt=cutoff).delete()
    except DatabaseError as exc:
        # 主请求的令牌消费已经提交；清理失败不能触发第二次 fallback 消费。
        logger.warning("rate limit bucket cleanup failed: %s", exc)


def consume_rate_limit(scope, client_ip, limit):
    backend = str(os.environ.get("RATELIMIT_BACKEND", "database")).strip().lower()
    if backend == "memory":
        return _memory_limit(scope, client_ip, limit)
    try:
        return _database_limit(scope, client_ip, limit)
    except DatabaseError as exc:
        logger.warning("shared rate limit unavailable; using process-local fallback: %s", exc)
        return _memory_limit(scope, client_ip, limit)


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _truthy(os.environ.get("RATELIMIT_DISABLED")):
            return self.get_response(request)

        path = request.path
        if not path.startswith("/api/") or path.rstrip("/") == "/api/system/health":
            return self.get_response(request)

        scope = "ai" if _is_expensive_request(request) else "api"
        if path.startswith('/api/v3/') and scope != 'ai':
            if request.method == 'GET' and ('/tiles/' in path or path.endswith('/preview')):
                scope = 'v3_raster'
            elif request.method == 'PUT' and '/chunks/' in path:
                scope = 'v3_upload'
            elif request.method == 'GET':
                scope = 'v3_read'
        limit = _limit_for(scope)
        if limit <= 0:
            return self.get_response(request)

        allowed, retry_after = consume_rate_limit(scope, _client_ip(request), limit)
        if not allowed:
            response = JsonResponse(
                {"ok": False, "code": 429, "msg": "请求过于频繁,请稍后再试",
                 "error": {"code": "rate_limited", "message": "请求过于频繁，稍后自动重试"}},
                status=429,
            )
            response["Retry-After"] = str(retry_after)
            return response

        return self.get_response(request)
