"""僵尸下载任务清理(R4)。

gunicorn 多 worker / 部署重启会杀死正在跑的后台下载线程,
DownloadTask 记录会停在 downloading。本命令释放超时下载和 Agent 的旧 worker
租约，并清掉内存进度字典里的陈旧条目，交给 run_agent_worker 恢复。

用法:
    python manage.py cleanup_stale_tasks               # 清理 10 分钟无更新的
    python manage.py cleanup_stale_tasks --minutes 30
    python manage.py cleanup_stale_tasks --dry-run     # 只列出,不动数据
生产建议:部署后执行一次,或挂 cron 定时跑。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from map_api.models import AgentSession, DownloadTask
from map_api.utils.get_satellite_image import _download_progress


class Command(BaseCommand):
    help = "释放僵尸下载和超时 Agent 的旧租约，供持久化 worker 恢复。"

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=10, help="无更新超过该分钟数视为僵尸(默认 10)")
        parser.add_argument("--dry-run", action="store_true", help="只列出僵尸任务,不修改数据")

    def handle(self, *args, **options):
        minutes = max(1, options["minutes"])
        cutoff = timezone.now() - timedelta(minutes=minutes)
        self._clean_downloads(cutoff, options["dry_run"])
        self._clean_sessions(cutoff, options["dry_run"])

    def _clean_downloads(self, cutoff, dry_run):
        stale = list(
            DownloadTask.objects.filter(status="downloading", updated_at__lt=cutoff)
            .values("id", "file_name", "updated_at")
        )
        if not stale:
            self.stdout.write(self.style.SUCCESS("没有僵尸下载任务。"))
            return
        for item in stale:
            self.stdout.write(
                f"  僵尸任务 #{item['id']} {item['file_name']} 最后更新 {item['updated_at'].isoformat()}"
            )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"dry-run:发现 {len(stale)} 个僵尸任务,未修改。"))
            return
        updated = DownloadTask.objects.filter(
            id__in=[item["id"] for item in stale]
        ).update(
            worker_claim="",
            claimed_at=None,
            error_message="后台下载长时间无心跳，已释放旧租约等待 worker 接管。",
            updated_at=timezone.now(),
        )
        for item in stale:
            _download_progress.pop(item["file_name"], None)
        self.stdout.write(self.style.SUCCESS(f"已释放 {updated} 个僵尸下载任务的旧租约，等待 worker 恢复。"))

    def _clean_sessions(self, cutoff, dry_run):
        stale_sessions = list(
            AgentSession.objects.filter(status=AgentSession.STATUS_RUNNING, updated_at__lt=cutoff)
            .values("id", "goal", "updated_at")
        )
        if not stale_sessions:
            self.stdout.write(self.style.SUCCESS("没有僵尸 Agent 会话。"))
            return
        for item in stale_sessions:
            self.stdout.write(
                f"  僵尸会话 #{item['id']} {item['goal'][:40]} 最后更新 {item['updated_at'].isoformat()}"
            )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"dry-run:发现 {len(stale_sessions)} 个僵尸会话,未修改。"))
            return
        released = 0
        for item in stale_sessions:
            with transaction.atomic():
                session = AgentSession.objects.select_for_update().filter(
                    id=item["id"], status=AgentSession.STATUS_RUNNING, updated_at__lt=cutoff
                ).first()
                if not session:
                    continue
                artifacts = dict(session.artifacts or {})
                artifacts.pop("worker_claim", None)
                artifacts.pop("worker_claimed_at", None)
                artifacts["recovery_pending_at"] = timezone.now().isoformat()
                artifacts["recovery_reason"] = "后台执行长时间无心跳，已释放旧租约等待 worker 接管。"
                session.artifacts = artifacts
                session.save(update_fields=["artifacts", "updated_at"])
                released += 1
        self.stdout.write(self.style.SUCCESS(f"已释放 {released} 个僵尸 Agent 会话的旧租约，等待 worker 恢复。"))
