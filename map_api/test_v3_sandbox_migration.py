import io
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from .models import AgentSession, ChatHistory, Conversation, LegacyConversationLink, SpatialAttachment
from .v3 import sandbox


class SandboxBoundaryTests(SimpleTestCase):
    def _archive(self, members):
        value = io.BytesIO()
        with tarfile.open(fileobj=value, mode="w") as archive:
            for name, content in members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        value.seek(0)
        return value

    def test_extract_outputs_refuses_escape_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                sandbox.extract_outputs(self._archive([("../escape.txt", b"x")]), directory)
            with self.assertRaises(ValueError):
                sandbox.extract_outputs(self._archive([("same.txt", b"x"), ("same.txt", b"y")]), directory)

    def test_extract_outputs_refuses_links_and_capacity_overflow(self):
        link_archive = io.BytesIO()
        with tarfile.open(fileobj=link_archive, mode="w") as archive:
            link = tarfile.TarInfo("shortcut")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            archive.addfile(link)
        link_archive.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                sandbox.extract_outputs(link_archive, directory)
            with patch.object(sandbox, "MAX_OUTPUT", 2):
                with self.assertRaisesRegex(ValueError, "总大小"):
                    sandbox.extract_outputs(self._archive([("too-large.bin", b"123")]), directory)

    @override_settings(V3_PYTHON_RUNTIME="docker")
    def test_unavailable_docker_never_executes_host_code(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with patch.object(sandbox, "sandbox_status", return_value={"available": False}), \
                    patch.object(sandbox.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "不会退回宿主机"):
                    sandbox.run_python("raise SystemExit", [], "run-1-a")
            run.assert_not_called()

    def test_timeout_is_bounded_before_container_launch(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with patch.object(sandbox, "sandbox_status") as status:
                with self.assertRaisesRegex(ValueError, "1 到 600"):
                    sandbox.run_python("pass", [], "run-1-a", timeout=601)
            status.assert_not_called()


class V3HistoryMigrationTests(TestCase):
    def _apply(self):
        output = io.StringIO()
        call_command("migrate_v3_history", "--apply", stdout=output)
        return output.getvalue()

    def test_explicit_history_image_is_copied_hashed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            legacy_dir = Path(directory) / "satellite_imgs"
            legacy_dir.mkdir()
            original = legacy_dir / "linked.png"
            original.write_bytes(b"directly-linked-image-bytes")
            history = ChatHistory.objects.create(image_file="linked.png", messages=[{"role": "user", "content": "q"}])
            session = AgentSession.objects.create(owner_session_key="owner-a", goal="goal", history=history,
                messages=[{"role": "assistant", "content": "a"}])

            first = self._apply()
            conversation = LegacyConversationLink.objects.get(source_model="AgentSession", source_pk=session.pk).conversation
            attachment = SpatialAttachment.objects.get(conversation=conversation)
            self.assertEqual(attachment.owner_session_key, "owner-a")
            self.assertEqual(attachment.metadata["legacy"]["source_pk"], history.pk)
            self.assertEqual(attachment.sha256, sandbox.file_hash(original))
            self.assertTrue(Path(attachment.file_path).is_file())
            self.assertEqual(conversation.messages.count(), 2)
            self.assertIn('"attachments": 1', first)

            self._apply()
            self.assertEqual(Conversation.objects.count(), 1)
            self.assertEqual(SpatialAttachment.objects.count(), 1)
            self.assertEqual(conversation.messages.count(), 2)
            self.assertTrue(original.is_file())

    def test_same_history_for_another_owner_stays_separate(self):
        history = ChatHistory.objects.create(image_file="missing.png", messages=[{"role": "user", "content": "history"}])
        first = AgentSession.objects.create(owner_session_key="owner-a", goal="first", history=history,
            messages=[{"role": "assistant", "content": "one"}])
        second = AgentSession.objects.create(owner_session_key="owner-b", goal="second", history=history,
            messages=[{"role": "assistant", "content": "two"}])

        report = self._apply()
        first_conversation = LegacyConversationLink.objects.get(source_model="AgentSession", source_pk=first.pk).conversation
        second_conversation = LegacyConversationLink.objects.get(source_model="AgentSession", source_pk=second.pk).conversation
        self.assertNotEqual(first_conversation.pk, second_conversation.pk)
        self.assertEqual(first_conversation.owner_session_key, "owner-a")
        self.assertEqual(second_conversation.owner_session_key, "owner-b")
        self.assertEqual(second_conversation.messages.count(), 1)
        self.assertIn('"owner_conflicts_preserved": 1', report)
