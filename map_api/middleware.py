"""轻量 IP 限流中间件:保护付费 API 端点免受公网滥用(R1)。

策略:
- 仅作用于 /api/ 前缀;页面与静态资源不限流;健康检查端点豁免。
- 高成本端点(AI 分析 / Agent / 报告生成)用更严的额度,其余 API 用宽松额度。
- 滑动窗口 60s,内存计数(线程安全)。多 worker 部署时每 worker 独立计数——
  对本项目的并发量级足够,不引入 Redis 依赖。
- 环境变量:RATELIMIT_API_PER_MINUTE(默认 120)、RATELIMIT_AI_PER_MINUTE(默认 30);
  RATELIMIT_DISABLED=1 完全关闭(演示模式);值 ≤0 视为关闭该项。
"""
import os
import threading
import time

from django.http import JsonResponse

_LOCK = threading.Lock()
_BUCKETS = {}  # (scope, ip) -> [monotonic 时间戳]
_WINDOW_SECONDS = 60.0
_MAX_BUCKETS = 4000

_AI_PATH_PREFIXES = (
    "/api/ai/query-region/",
    "/api/agent/sessions",
    "/api/report/generate/",
)


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _limit_for(scope):
    if scope == "ai":
        raw = os.environ.get("RATELIMIT_AI_PER_MINUTE", "30")
    else:
        raw = os.environ.get("RATELIMIT_API_PER_MINUTE", "120")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 30 if scope == "ai" else 120


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def reset_rate_limit_state():
    """测试辅助:清空计数桶。"""
    with _LOCK:
        _BUCKETS.clear()


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _truthy(os.environ.get("RATELIMIT_DISABLED")):
            return self.get_response(request)

        path = request.path
        if not path.startswith("/api/") or path.rstrip("/") == "/api/system/health":
            return self.get_response(request)

        scope = "ai" if any(path.startswith(prefix) for prefix in _AI_PATH_PREFIXES) else "api"
        limit = _limit_for(scope)
        if limit <= 0:
            return self.get_response(request)

        key = (scope, _client_ip(request))
        now = time.monotonic()
        with _LOCK:
            hits = [ts for ts in _BUCKETS.get(key, ()) if now - ts < _WINDOW_SECONDS]
            if len(hits) >= limit:
                _BUCKETS[key] = hits
                retry_after = max(1, int(_WINDOW_SECONDS - (now - hits[0])) + 1)
                response = JsonResponse(
                    {"ok": False, "code": 429, "msg": "请求过于频繁,请稍后再试"},
                    status=429,
                )
                response["Retry-After"] = str(retry_after)
                return response
            hits.append(now)
            _BUCKETS[key] = hits
            if len(_BUCKETS) > _MAX_BUCKETS:
                stale = [k for k, v in _BUCKETS.items() if not v or now - v[-1] >= _WINDOW_SECONDS]
                for k in stale:
                    _BUCKETS.pop(k, None)

        return self.get_response(request)
