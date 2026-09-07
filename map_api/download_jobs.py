"""可恢复的 Mapbox 下载任务执行器。"""

import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .media_paths import SAVE_DIR
from .models import DownloadTask
from .utils.get_satellite_image import fetch_satellite_image


def claim_next_download_task(worker_id, claim_timeout=900):
    cutoff = timezone.now() - timedelta(seconds=max(30, int(claim_timeout)))
    with transaction.atomic():
        task = (
            DownloadTask.objects.select_for_update()
            .filter(status="downloading", worker_claim="")
            .order_by("created_at")
            .first()
        )
        if not task:
            task = (
                DownloadTask.objects.select_for_update()
                .filter(status="downloading", claimed_at__lte=cutoff)
                .order_by("created_at")
                .first()
            )
        if not task:
            return None
        task.worker_claim = worker_id
        task.claimed_at = timezone.now()
        task.attempts = int(task.attempts or 0) + 1
        task.error_message = ""
        task.save(update_fields=["worker_claim", "claimed_at", "attempts", "error_message", "updated_at"])
        return task


def _temporary_name(task, worker_id):
    return f".{task.file_name}.{worker_id}.part.jpg"


def _remove_file(path):
    if path and os.path.isfile(path):
        os.remove(path)


def execute_download_task(task_id, worker_id):
    try:
        task = DownloadTask.objects.get(id=task_id)
    except DownloadTask.DoesNotExist:
        return False
    if task.status != "downloading" or task.worker_claim != worker_id:
        return False

    temp_name = _temporary_name(task, worker_id)
    temp_path = os.path.join(SAVE_DIR, temp_name)
    final_path = os.path.join(SAVE_DIR, task.file_name)
    _remove_file(temp_path)

    def progress(_file_name, info):
        # fetch 的 done 只代表临时文件完成；最终文件尚未通过所有权门禁。
        status = "error" if info.get("status") == "error" else "downloading"
        DownloadTask.objects.filter(
            id=task_id, status="downloading", worker_claim=worker_id
        ).update(
            status=status,
            total=int(info.get("total", 1) or 1),
            done=int(info.get("done", 0) or 0),
            failed=int(info.get("failed", 0) or 0),
            error_message=str(info.get("error", ""))[:500],
            claimed_at=timezone.now(),
            updated_at=timezone.now(),
        )

    try:
        generated = fetch_satellite_image(
            task.min_lng, task.min_lat, task.max_lng, task.max_lat,
            save_dir=SAVE_DIR,
            file_name=temp_name,
            target_resolution=task.resolution_px,
            progress_callback=progress,
        )
        with transaction.atomic():
            locked = DownloadTask.objects.select_for_update().get(id=task_id)
            if locked.worker_claim != worker_id or locked.status not in ("downloading", "error"):
                _remove_file(temp_path)
                return False
            if not generated or not os.path.isfile(temp_path):
                locked.status = "error"
                locked.error_message = locked.error_message or "影像下载失败"
                locked.worker_claim = ""
                locked.claimed_at = None
                locked.save(update_fields=["status", "error_message", "worker_claim", "claimed_at", "updated_at"])
                _remove_file(temp_path)
                return False
            os.replace(temp_path, final_path)
            locked.status = "partial" if locked.failed else "done"
            locked.done = locked.total
            locked.worker_claim = ""
            locked.claimed_at = None
            locked.error_message = ""
            locked.save(update_fields=["status", "done", "worker_claim", "claimed_at", "error_message", "updated_at"])
            return True
    except Exception as exc:
        _remove_file(temp_path)
        DownloadTask.objects.filter(id=task_id, worker_claim=worker_id).update(
            status="error", error_message=str(exc)[:500], worker_claim="", claimed_at=None,
            updated_at=timezone.now(),
        )
        return False
