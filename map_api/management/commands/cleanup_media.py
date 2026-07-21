"""按文件年龄清理媒体目录(R10)——cron/手动版,与 /api/satellite/cleanup/ 共用同一套核心逻辑。

清理范围:media/satellite_imgs(含全部衍生物 _hd/_overview/_tile_*/_crop_*/_stage1_*)
与 media/reports 中 mtime 超过 --age 天的文件,并同步删除相关 DB 记录。

用法:
    python manage.py cleanup_media --dry-run        # 先看会删什么
    python manage.py cleanup_media --age 7          # 删 7 天前的
    python manage.py cleanup_media --all            # 清空全部(含 DB 记录,慎用)
"""
from django.core.management.base import BaseCommand

from map_api.views import cleanup_media_core


class Command(BaseCommand):
    help = "按年龄清理卫星图衍生物与报告文件,同步清理相关数据库记录。"

    def add_arguments(self, parser):
        parser.add_argument("--age", type=int, default=7, help="文件年龄阈值(天,默认 7)")
        parser.add_argument("--all", action="store_true", dest="clean_all",
                            help="清空全部媒体与相关记录(慎用)")
        parser.add_argument("--dry-run", action="store_true", help="只统计不删除")

    def handle(self, *args, **options):
        stats = cleanup_media_core(days=options["age"], clean_all=options["clean_all"],
                                   dry_run=options["dry_run"])
        verb = "将删除" if stats["dry_run"] else "已删除"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {stats['deleted_files']} 个文件({stats['freed_mb']} MB),"
            f"涉及 {stats['deleted_image_records']} 条影像记录。"
        ))
