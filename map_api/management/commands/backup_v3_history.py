"""Create and verify an additive local SQLite/history-image backup."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Back up local SQLite and original legacy images without altering source data"

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            raise CommandError("PostgreSQL 环境请使用 pg_dump 和影像文件备份；本命令只处理本地 SQLite")
        root = Path(settings.BASE_DIR) / ".codex-runtime" / "backups" / ("v3-history-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
        root.mkdir(parents=True, exist_ok=False)
        connection.ensure_connection()
        with sqlite3.connect(root / "database.sqlite3") as backup:
            connection.connection.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CommandError("数据库备份完整性检查失败")
        source_root = (Path(settings.MEDIA_ROOT) / "satellite_imgs").resolve()
        image_root = root / "satellite_imgs"
        image_root.mkdir()
        manifest = []
        if source_root.is_dir():
            for source in sorted(source_root.iterdir()):
                if not source.is_file() or source.is_symlink():
                    continue
                target = image_root / source.name
                digest = checksum(source)
                shutil.copy2(source, target)
                if checksum(target) != digest:
                    raise CommandError("影像在备份期间改变，请重新备份")
                manifest.append({"name": source.name, "size": source.stat().st_size, "sha256": digest})
        (root / "images.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps({"backup": str(root), "database_integrity": "ok", "images": len(manifest),
            "image_bytes": sum(row["size"] for row in manifest), "hashes_verified": True}, ensure_ascii=False))
