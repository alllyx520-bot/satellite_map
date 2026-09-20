"""Persistent V3 asset + conversation worker."""
import time
from concurrent.futures import ThreadPoolExecutor
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone
from map_api.models import AgentRun, SpatialAttachment
from map_api.v3.assets import process_attachment
from map_api.v3.harness import execute_run


class Command(BaseCommand):
    help = "Process V3 assets and leased conversation runs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--conversations", type=int, default=2)

    def handle(self, *args, **options):
        futures, asset_futures = {}, {}
        capacity = max(1, min(8, options["conversations"]))
        with ThreadPoolExecutor(max_workers=capacity) as pool, ThreadPoolExecutor(max_workers=2) as assets:
            while True:
                close_old_connections()
                for id, future in list(asset_futures.items()):
                    if future.done():
                        future.result()
                        del asset_futures[id]
                available_assets = SpatialAttachment.objects.filter(status__in=["pending", "processing"]).filter(
                    Q(processing_lease_until__lt=timezone.now()) | Q(processing_lease_until=None)
                ).exclude(pk__in=asset_futures).order_by("created_at").values_list("id", flat=True)[:2-len(asset_futures)]
                for id in available_assets:
                    asset_futures[id] = assets.submit(process_attachment, id)
                for id, future in list(futures.items()):
                    if future.done():
                        future.result()
                        del futures[id]
                free = capacity - len(futures)
                ids = AgentRun.objects.filter(execution_engine="harness", status__in=["queued", "running", "cancelling"]).filter(Q(lease_until__lt=timezone.now()) | Q(lease_until=None)).exclude(pk__in=futures).order_by("created_at").values_list("id", flat=True)[:free]
                for id in ids:
                    futures[id] = pool.submit(execute_run, id)
                if options["once"]:
                    for future in asset_futures.values():
                        future.result()
                    for future in futures.values():
                        future.result()
                    return
                time.sleep(1)
