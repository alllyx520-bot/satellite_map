"""运行持久化 Agent 会话，供服务重启后的恢复和定时任务调用。"""
import time
import uuid
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db import transaction
from django.utils import timezone

from map_api.models import AgentSession
from map_api.run_kernel import sync_session
from map_api.orchestrator import run_agent_session
from map_api.report_jobs import claim_next_report_job, execute_report_job
from map_api.download_jobs import claim_next_download_task, execute_download_task


class Command(BaseCommand):
    help = "处理数据库中 running 状态的 AgentSession；可用于重启恢复或计划任务。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="只扫描并处理一轮后退出")
        parser.add_argument("--poll-seconds", type=float, default=5.0, help="持续模式轮询间隔")
        parser.add_argument("--max-sessions", type=int, default=1, help="每轮最多处理的会话数")
        parser.add_argument("--stale-after", type=int, default=0, help="只恢复超过 N 秒未更新的会话；0 表示全部 running")
        parser.add_argument("--claim-timeout", type=int, default=900, help="已有 worker 租约超过 N 秒后允许接管")

    def _wait(self, seconds):
        try:
            time.sleep(seconds)
            return True
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Agent worker 收到停止信号，已安全退出。"))
            return False

    def handle(self, *args, **options):
        once = bool(options["once"])
        poll_seconds = max(0.5, float(options["poll_seconds"]))
        max_sessions = max(1, int(options["max_sessions"]))
        stale_after = max(0, int(options["stale_after"]))
        claim_timeout = max(30, int(options["claim_timeout"]))
        total = 0
        failed = 0
        worker_id = uuid.uuid4().hex[:10]
        while True:
            close_old_connections()
            # 报告任务与 Agent 会话共用持久化 worker，避免报告生成重新退回 HTTP/daemon thread。
            report_job = claim_next_report_job(worker_id, claim_timeout)
            if report_job:
                self.stdout.write(f"生成报告任务 #{report_job.id}")
                report_ok = execute_report_job(report_job.id, worker_id)
                if once and max_sessions <= 1:
                    self.stdout.write(f"本轮处理 1 个报告任务，失败 {0 if report_ok else 1} 个。")
                    return
            download_task = claim_next_download_task(worker_id, claim_timeout)
            if download_task:
                self.stdout.write(f"下载影像任务 #{download_task.id}：{download_task.file_name}")
                download_ok = execute_download_task(download_task.id, worker_id)
                if once and max_sessions <= 1:
                    self.stdout.write(f"本轮处理 1 个影像任务，失败 {0 if download_ok else 1} 个。")
                    return
            qs = AgentSession.objects.filter(status=AgentSession.STATUS_RUNNING).order_by("updated_at")
            if stale_after:
                cutoff = timezone.now() - timedelta(seconds=stale_after)
                qs = qs.filter(updated_at__lte=cutoff)
            # 不能只取 max_sessions 条：最老的一条可能被其它 worker 租约占用，
            # 否则跳过后会让后面的可执行任务长期饿死。
            candidate_limit = min(100, max_sessions * 10)
            session_ids = list(qs.values_list("id", flat=True)[:candidate_limit])
            if not session_ids:
                if once:
                    self.stdout.write("没有可恢复的 running Agent 会话。")
                    return
                if not self._wait(poll_seconds):
                    return
                continue
            processed_this_round = 0
            for session_id in session_ids:
                if processed_this_round >= max_sessions:
                    break
                from map_api.models import AgentRun
                candidate_session = AgentSession.objects.filter(pk=session_id).first()
                candidate_run = AgentRun.objects.filter(pk=(candidate_session.artifacts or {}).get("run_id")).first() if candidate_session else None
                if candidate_run and candidate_run.execution_engine == "dag":
                    from map_api.run_executor import execute_run
                    execute_run(candidate_run.id, worker_id)
                    processed_this_round += 1
                    total += 1
                    continue
                # 短事务抢占，避免多个 worker 同时重复跑同一会话；长耗时执行在事务外完成。
                try:
                    with transaction.atomic():
                        session = AgentSession.objects.select_for_update().get(id=session_id)
                        artifacts = dict(session.artifacts or {})
                        claim = artifacts.get("worker_claim")
                        claimed_at = artifacts.get("worker_claimed_at")
                        if claim and claimed_at:
                            try:
                                claimed_dt = datetime.fromisoformat(claimed_at)
                                if timezone.is_naive(claimed_dt):
                                    claimed_dt = timezone.make_aware(claimed_dt, timezone.get_current_timezone())
                                if (timezone.now() - claimed_dt).total_seconds() < claim_timeout:
                                    continue
                            except (TypeError, ValueError):
                                pass
                        artifacts["worker_claim"] = worker_id
                        artifacts["worker_claimed_at"] = timezone.now().isoformat()
                        session.artifacts = artifacts
                        session.save(update_fields=["artifacts", "updated_at"])
                except AgentSession.DoesNotExist:
                    # 扫描与抢占之间任务可能被用户清理，跳过这个已消失的 id。
                    continue
                self.stdout.write(f"恢复 Agent 会话 #{session.id}：{session.goal[:80]}")
                processed_this_round += 1
                try:
                    run_agent_session(session.id, {
                        "resume_with_scene": bool(session.scene_id),
                        "worker_claim": worker_id,
                    })
                    sync_session(session.id)
                    total += 1
                except Exception as exc:
                    self.stderr.write(f"会话 #{session.id} 执行异常：{exc}")
                    failed += 1
                    self._mark_worker_failure(session.id, worker_id, exc)
                    try:
                        sync_session(session.id, error=str(exc))
                    except AgentSession.DoesNotExist:
                        pass
            if once:
                self.stdout.write(f"本轮处理 {total} 个 Agent 会话，失败 {failed} 个。")
                return

    def _mark_worker_failure(self, session_id, worker_id, exc):
        """未捕获异常不能留下 running+租约的永久僵尸。"""
        try:
            with transaction.atomic():
                session = AgentSession.objects.select_for_update().get(id=session_id)
                artifacts = dict(session.artifacts or {})
                if session.status != AgentSession.STATUS_RUNNING or artifacts.get("worker_claim") != worker_id:
                    return
                artifacts.pop("worker_claim", None)
                artifacts.pop("worker_claimed_at", None)
                artifacts["worker_exception"] = str(exc)[:500]
                session.status = AgentSession.STATUS_FAILED
                session.error = f"Agent worker 执行异常：{str(exc)[:500]}"
                session.artifacts = artifacts
                session.messages = list(session.messages or []) + [{
                    "role": "assistant",
                    "content": "Agent worker 异常退出，任务已停止，可重新发起调查。",
                }]
                session.save(update_fields=["status", "error", "artifacts", "messages", "updated_at"])
        except AgentSession.DoesNotExist:
            return
