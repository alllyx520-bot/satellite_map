"""僵尸下载任务清理(R4)。

gunicorn 多 worker / 部署重启会杀死正在跑的后台下载线程,
DownloadTask 记录就永远停在 downloading。本命令把超时未更新的
downloading 任务标为 error(附可恢复提示),并清掉内存进度字典里的陈旧条目。

用法:
    python manage.py cleanup_stale_tasks               # 清理 10 分钟无更新的
    python manage.py cleanup_stale_tasks --minutes 30
    python manage.py cleanup_stale_tasks --dry-run     # 只列出,不动数据
生产建议:部署后执行一次,或挂 cron 定时跑。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from map_api.models import DownloadTask
from map_api.utils.get_satellite_image import _download_progress


class Command(BaseCommand):
    help = "把超时未更新的 downloading 任务标记为 error,避免前端永远看到下载中。"

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=10, help="无更新超过该分钟数视为僵尸(默认 10)")
        parser.add_argument("--dry-run", action="store_true", help="只列出僵尸任务,不修改数据")

    def handle(self, *args, **options):
        minutes = max(1, options["minutes"])
        cutoff = timezone.now() - timedelta(minutes=minutes)
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

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"dry-run:发现 {len(stale)} 个僵尸任务,未修改。"))
            return

        updated = DownloadTask.objects.filter(
            id__in=[item["id"] for item in stale]
        ).update(
            status="error",
            error_message="后台下载线程被中断(常见于服务重启),请重新框选该区域重试。",
        )
        for item in stale:
            _download_progress.pop(item["file_name"], None)
        self.stdout.write(self.style.SUCCESS(f"已把 {updated} 个僵尸任务标记为 error。"))
