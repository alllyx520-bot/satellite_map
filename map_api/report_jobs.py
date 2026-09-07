"""持久化 Agent 报告任务的入队、抢占和执行逻辑。"""

import json
import os
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AgentSession, ReportJob


def _delete_unclaimed_report(result):
    """旧 worker 失去租约后删除自己刚生成、尚未入库的孤儿报告。"""
    name = os.path.basename(str((result or {}).get("file_name") or ""))
    if not name.startswith("report_") or not name.lower().endswith(".docx"):
        return
    path = os.path.abspath(os.path.join(settings.MEDIA_ROOT, name))
    root = os.path.abspath(settings.MEDIA_ROOT)
    if os.path.commonpath([path, root]) == root and os.path.isfile(path):
        os.remove(path)


def enqueue_report_job(session, payload, request_key, report_token):
    """在同一事务中创建幂等报告任务；返回已有任务或新任务。"""
    job, created = ReportJob.objects.get_or_create(
        request_key=request_key,
        defaults={
            "agent_session": session,
            "payload": {**payload, "report_token": report_token},
            "status": ReportJob.STATUS_QUEUED,
        },
    )
    if not created and job.status == ReportJob.STATUS_FAILED:
        job.status = ReportJob.STATUS_QUEUED
        job.error = ""
        job.worker_claim = ""
        job.claimed_at = None
        job.payload = {**payload, "report_token": report_token}
        job.save(update_fields=["status", "error", "worker_claim", "claimed_at", "payload", "updated_at"])
    return job


def claim_next_report_job(worker_id, claim_timeout=900):
    """短事务抢占一条报告任务；不把长报告生成放在事务内。"""
    cutoff = timezone.now() - timedelta(seconds=max(30, int(claim_timeout)))
    with transaction.atomic():
        job = (
            ReportJob.objects.select_for_update()
            .filter(status=ReportJob.STATUS_QUEUED)
            .order_by("created_at")
            .first()
        )
        if not job:
            job = (
                ReportJob.objects.select_for_update()
                .filter(status=ReportJob.STATUS_RUNNING, claimed_at__lte=cutoff)
                .order_by("created_at")
                .first()
            )
        if not job:
            return None
        job.status = ReportJob.STATUS_RUNNING
        job.worker_claim = worker_id
        job.claimed_at = timezone.now()
        job.attempts = int(job.attempts or 0) + 1
        job.save(update_fields=["status", "worker_claim", "claimed_at", "attempts", "updated_at"])
        return job


def execute_report_job(job_id, worker_id):
    """执行一条已被当前 worker 抢占的报告任务并原子落库。"""
    from .views import build_report

    try:
        job = ReportJob.objects.get(id=job_id)
    except ReportJob.DoesNotExist:
        return False
    if job.status != ReportJob.STATUS_RUNNING or job.worker_claim != worker_id:
        return False
    try:
        response = build_report(dict(job.payload or {}))
        data = json.loads(response.content)
        if response.status_code != 200 or data.get("code") != 200:
            raise RuntimeError(data.get("msg", "报告生成失败"))
        result = data.get("data") or {}
        with transaction.atomic():
            locked_job = ReportJob.objects.select_for_update().get(id=job_id)
            if locked_job.status != ReportJob.STATUS_RUNNING or locked_job.worker_claim != worker_id:
                _delete_unclaimed_report(result)
                return False
            locked_job.status = ReportJob.STATUS_COMPLETED
            locked_job.result = result
            locked_job.error = ""
            locked_job.worker_claim = ""
            locked_job.claimed_at = None
            locked_job.save(update_fields=["status", "result", "error", "worker_claim", "claimed_at", "updated_at"])
            if locked_job.agent_session_id:
                session = AgentSession.objects.select_for_update().get(id=locked_job.agent_session_id)
                artifacts = dict(session.artifacts or {})
                if not artifacts.get("report"):
                    artifacts["report"] = result
                    session.messages = list(session.messages or []) + [{"role": "assistant", "content": "Word 报告已生成。"}]
                token = (locked_job.payload or {}).get("report_token")
                if not token or artifacts.get("report_generating") == token:
                    artifacts.pop("report_generating", None)
                    artifacts.pop("report_generating_at", None)
                artifacts.pop("report_error", None)
                session.artifacts = artifacts
                session.save(update_fields=["artifacts", "messages", "updated_at"])
        if job.agent_session_id and (job.payload or {}).get("message_id"):
            from .views import _finish_agent_message_request
            _finish_agent_message_request(job.agent_session_id, job.payload["message_id"])
        return True
    except Exception as exc:
        with transaction.atomic():
            locked_job = ReportJob.objects.select_for_update().get(id=job_id)
            if locked_job.status != ReportJob.STATUS_RUNNING or locked_job.worker_claim != worker_id:
                return False
            locked_job.status = ReportJob.STATUS_FAILED
            locked_job.error = str(exc)[:1000]
            locked_job.worker_claim = ""
            locked_job.claimed_at = None
            locked_job.save(update_fields=["status", "error", "worker_claim", "claimed_at", "updated_at"])
            if locked_job.agent_session_id:
                session = AgentSession.objects.select_for_update().get(id=locked_job.agent_session_id)
                artifacts = dict(session.artifacts or {})
                token = (locked_job.payload or {}).get("report_token")
                if artifacts.get("report_generating") == token:
                    artifacts.pop("report_generating", None)
                    artifacts.pop("report_generating_at", None)
                artifacts["report_error"] = str(exc)[:500]
                session.artifacts = artifacts
                session.save(update_fields=["artifacts", "updated_at"])
        if job.agent_session_id and (job.payload or {}).get("message_id"):
            from .views import _rollback_agent_message_request
            _rollback_agent_message_request(job.agent_session_id, job.payload["message_id"])
        return False
