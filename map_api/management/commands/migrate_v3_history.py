"""Import explicitly linked legacy records without deleting or inventing history."""
import hashlib
import json
import shutil
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from map_api.models import AgentSession, ChatHistory, Conversation, ConversationMessage, LegacyConversationLink, SpatialAttachment
from map_api.run_journal import digest


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class Command(BaseCommand):
    help = "Preview/import owner-scoped legacy history; original rows/files remain intact"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @staticmethod
    def _legacy_image(history):
        """Return only the direct ChatHistory image relation, never guessed files."""
        if not history or not history.image_file:
            return None
        name = Path(history.image_file).name
        source_root = (Path(settings.MEDIA_ROOT) / "satellite_imgs").resolve()
        source = (source_root / name).resolve()
        if source.parent != source_root or not source.is_file() or source.is_symlink():
            return None
        return source

    @staticmethod
    def _copy_attachment(conversation, history):
        """Copy a directly linked legacy image and retain its original digest."""
        source = Command._legacy_image(history)
        if source is None:
            return False
        digest_value = file_hash(source)
        target_root = (Path(settings.MEDIA_ROOT) / "v3-assets" / "files").resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower()
        attachment = SpatialAttachment.objects.create(
            owner_session_key=conversation.owner_session_key,
            conversation=conversation,
            scene=history.scene,
            name=source.name,
            kind="geotiff" if suffix in {".tif", ".tiff"} else "image",
            status="pending",
            size_bytes=source.stat().st_size,
            sha256=digest_value,
            metadata={"legacy": {"source_model": "ChatHistory", "source_pk": history.pk,
                                  "original_name": source.name, "original_sha256": digest_value}},
        )
        target = (target_root / f"{attachment.id}{suffix}").resolve()
        if target.parent != target_root:
            raise ValueError("迁移附件路径无效")
        shutil.copy2(source, target)
        # Hash the copied bytes too: a changed source during copy cannot be represented as trusted input.
        copied_hash = file_hash(target)
        if copied_hash != digest_value:
            target.unlink(missing_ok=True)
            raise ValueError("迁移期间原始影像发生变化")
        attachment.file_path = str(target)
        attachment.save(update_fields=["file_path"])
        # The explicit legacy image relation belongs to this imported history.
        # Attach it to an actual historical user message when one exists; a
        # conversation without such a message keeps only the source relation.
        message = conversation.messages.filter(role="user", status="completed").first()
        if message:
            message.attachments.add(attachment)
            message.parts = [*message.parts, {"type": "image_ref", "attachment_id": str(attachment.id),
                "sha256": digest_value, "name": attachment.name, "source": "legacy_explicit_relation"}]
            message.save(update_fields=["parts"])
        return True

    def handle(self, *args, **options):
        sessions = AgentSession.objects.exclude(owner_session_key__isnull=True).exclude(owner_session_key="")
        mapped = set(LegacyConversationLink.objects.filter(source_model="AgentSession").values_list("source_pk", flat=True))
        pending = sessions.exclude(pk__in=mapped)
        self.stdout.write(json.dumps({"eligible_sessions": pending.count(),
            "unowned_sessions_preserved": AgentSession.objects.count() - sessions.count(),
            "unlinked_chat_histories_preserved": ChatHistory.objects.filter(agent_sessions__isnull=True).count(),
            "apply": options["apply"]}))
        if not options["apply"]:
            return
        stats = {"sessions": 0, "conversations": 0, "messages": 0, "attachments": 0,
                 "owner_conflicts_preserved": 0, "missing_explicit_images_preserved": 0}
        for session in pending.order_by("id").iterator():
            with transaction.atomic():
                if LegacyConversationLink.objects.filter(source_model="AgentSession", source_pk=session.id).exists():
                    continue
                history_link = (LegacyConversationLink.objects.select_related("conversation").filter(
                    source_model="ChatHistory", source_pk=session.history_id).first() if session.history_id else None)
                linked = history_link if history_link and history_link.conversation.owner_session_key == session.owner_session_key else None
                if history_link and not linked:
                    # A legacy history can belong to one owner only.  Preserve this session
                    # separately instead of merging records based on matching text or filename.
                    stats["owner_conflicts_preserved"] += 1
                conversation = linked.conversation if linked else Conversation.objects.create(owner_session_key=session.owner_session_key,
                    title=session.goal[:100], summary={"legacy": True, "execution_replay_available": False})
                stats["conversations"] += int(not linked)
                seq = conversation.messages.count()
                rows = []
                if session.history_id and not linked:
                    # Import a ChatHistory only when it has not already been assigned to
                    # another owner.  This is an explicit FK relationship, not a heuristic.
                    if not history_link:
                        rows.extend(session.history.messages or [])
                rows.extend(session.messages or [])
                for i, item in enumerate(rows):
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    content = item.get("content")
                    if role not in {"user", "assistant"} or not isinstance(content, str):
                        continue
                    seq += 1
                    ConversationMessage.objects.create(conversation=conversation, role=role, content=content,
                        status="completed", parts=[{"type": "legacy_record", "session_id": session.id, "index": i}],
                        sequence=seq, request_id=f"legacy:{session.id}:{i}", request_digest=digest(item))
                    stats["messages"] += 1
                LegacyConversationLink.objects.create(source_model="AgentSession", source_pk=session.id, conversation=conversation)
                if session.history_id and not history_link:
                    copied = self._copy_attachment(conversation, session.history)
                    if copied:
                        stats["attachments"] += 1
                    else:
                        stats["missing_explicit_images_preserved"] += 1
                    LegacyConversationLink.objects.create(source_model="ChatHistory", source_pk=session.history_id, conversation=conversation)
                stats["sessions"] += 1
        stats["eligible_sessions_after"] = pending.exclude(
            pk__in=LegacyConversationLink.objects.filter(source_model="AgentSession").values_list("source_pk", flat=True)
        ).count()
        self.stdout.write(json.dumps({"import": stats, "original_records_and_files": "unchanged"}, ensure_ascii=False))
