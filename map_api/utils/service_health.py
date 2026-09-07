"""跨进程外部服务短期熔断。数据库不可用时自动退化为不熔断。"""

import hashlib
import os
from datetime import timedelta

import requests
from django.db import transaction
from django.utils import timezone


class ServiceCircuitOpen(requests.exceptions.ConnectionError):
    """服务近期连续失败，暂时不再发起新请求。"""


def service_key(kind, endpoint):
    raw = f"{kind}:{endpoint}:{os.environ.get('SATELLITESENSE_DIRECT_HTTP', '').strip().lower()}"
    return f"{kind}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def check_service(service_key_value):
    try:
        from ..models import ExternalServiceHealth
        row = ExternalServiceHealth.objects.filter(service_key=service_key_value).first()
        if row and row.open_until and row.open_until > timezone.now():
            remaining = max(1, int((row.open_until - timezone.now()).total_seconds()))
            raise ServiceCircuitOpen(f"外部服务暂时熔断，约 {remaining}s 后重试")
    except ServiceCircuitOpen:
        raise
    except Exception:
        # 健康表/数据库本身异常不能阻断主业务请求。
        return


def record_success(service_key_value):
    try:
        from ..models import ExternalServiceHealth
        now = timezone.now()
        ExternalServiceHealth.objects.filter(service_key=service_key_value).update(
            failure_count=0, open_until=None, last_error="", last_error_type="", last_success_at=now, updated_at=now
        )
    except Exception:
        return


def record_failure(service_key_value, error, threshold=2, cooldown_seconds=30):
    try:
        from ..models import ExternalServiceHealth
        now = timezone.now()
        with transaction.atomic():
            row, _ = ExternalServiceHealth.objects.select_for_update().get_or_create(
                service_key=service_key_value,
                defaults={"failure_count": 0},
            )
            row.failure_count += 1
            row.last_error = str(error)[:500]
            row.last_failure_at = now
            row.last_error_type = getattr(error, "error_type", "provider_unavailable")
            if row.failure_count >= max(1, threshold):
                row.open_until = now + timedelta(seconds=max(1, cooldown_seconds))
            row.save(
                update_fields=[
                    "failure_count",
                    "last_error",
                    "last_failure_at",
                    "last_error_type",
                    "open_until",
                    "updated_at",
                ]
            )
    except Exception:
        return
