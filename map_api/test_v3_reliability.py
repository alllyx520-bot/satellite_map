"""Recovery, scope, sampling and transport regressions for the V3 harness."""
import json
import uuid
import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .middleware import RateLimitMiddleware
from .models import AgentRun, AgentTurn, Conversation, SpatialAttachment, SpatialObservation
from .test_v3_harness import call
from .v3.conversations import submit
from .v3.harness import (_prepare_call, _save_call, claim_run, execute_run, invocation_context)
from .v3.methods import coverage
from .v3.places import search
from .v3.spatial_tools import annotate, finish, relation, vision
from .v3.tools import registry


class HarnessRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(owner_session_key="reliability-owner")
        _, self.run = submit(self.conversation.id, "reliability-owner", {"content": "检查影像",
            "request_id": uuid.uuid4().hex, "attachment_ids": []})

    def test_unknown_tool_has_durable_native_response(self):
        n = 0
        def provider(messages, tools, **kwargs):
            nonlocal n
            n += 1
            if n == 1:
                return {"tool_calls": [call("does_not_exist", {"x": 1})]}
            paired = [m for m in messages if m["role"] == "tool"]
            self.assertEqual(json.loads(paired[-1]["content"])["error"]["code"], "unknown_tool")
            return {"content": "已处理无效调用"}
        execute_run(self.run.pk, provider=provider)
        row = self.run.tool_calls.get()
        self.assertEqual(row.status, "completed")
        self.assertEqual(row.result["error"]["code"], "unknown_tool")

    def test_annotation_rejects_attachment_or_malformed_id_with_recovery_instruction(self):
        ctx = {"run": self.run, "attachment_ids": [], "version": 1, "call_key": "annotation"}
        for invalid in (str(uuid.uuid4()), "attachment-1"):
            with self.assertRaisesMessage(ValueError, "view_overview 或 read_image_window"):
                annotate({"observation_id": invalid, "label": "位置", "summary": "观察"}, ctx)

    def test_successful_tool_is_not_reexecuted_after_worker_loss(self):
        token = claim_run(self.run.pk)
        invocation = call("read_history", {})
        turn = AgentTurn.objects.create(run=self.run, number=1, status="decided",
            context_version=self.run.context_version,
            decision={"tool_calls": [invocation]})
        row, _ = _prepare_call(self.run.pk, token, turn, 0, invocation, registry()["read_history"])
        _save_call(self.run.pk, token, row.pk, {"items": [{"content": "committed fact"}]})
        AgentRun.objects.filter(pk=self.run.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        with patch("map_api.v3.spatial_tools.history", side_effect=AssertionError("must reuse")):
            execute_run(self.run.pk, provider=lambda *args, **kwargs: {"content": "完成恢复"})
        row.refresh_from_db()
        self.assertEqual(row.result["items"][0]["content"], "committed fact")

    def test_uncertain_external_result_is_disclosed_after_takeover(self):
        token = claim_run(self.run.pk)
        invocation = call("review_visual", {"observation_ids": [str(uuid.uuid4())], "question": "检查"})
        turn = AgentTurn.objects.create(run=self.run, number=1, status="decided",
            context_version=self.run.context_version, decision={"tool_calls": [invocation]})
        _prepare_call(self.run.pk, token, turn, 0, invocation, registry()["review_visual"])
        AgentRun.objects.filter(pk=self.run.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        def provider(messages, tools, **kwargs):
            self.assertIn("result_unknown", json.dumps(messages))
            return {"content": "先前复核结果未知"}
        with patch("map_api.v3.spatial_tools.vision", side_effect=AssertionError("must not retry implicitly")):
            execute_run(self.run.pk, provider=provider)
        self.assertEqual(self.run.tool_calls.get().status, "uncertain")

    def test_tools_load_only_after_explicit_discovery(self):
        n = 0
        def provider(messages, tools, **kwargs):
            nonlocal n
            n += 1
            names = {t["function"]["name"] for t in tools}
            if n == 1:
                self.assertNotIn("compute_product", names)
                return {"tool_calls": [call("load_capabilities", {"groups": ["imagery"]})]}
            self.assertIn("compute_product", names)
            return {"content": "已加载原始影像工具"}
        execute_run(self.run.pk, provider=provider)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")

    def test_legacy_explicit_attachment_is_in_followup_scope(self):
        attachment = SpatialAttachment.objects.create(conversation=self.conversation,
            owner_session_key="reliability-owner", name="legacy.tif", status="pending",
            metadata={"legacy": {"source_model": "ChatHistory", "source_pk": 1}})
        self.assertIn(str(attachment.pk), invocation_context(self.run, {})["attachment_ids"])

    def test_spatial_answer_cannot_bypass_finish_contract(self):
        attachment = SpatialAttachment.objects.create(conversation=self.conversation,
            owner_session_key="reliability-owner", name="image.tif", status="ready")
        self.run.trigger_message.attachments.add(attachment)
        n = 0
        def provider(messages, tools, **kwargs):
            nonlocal n
            n += 1
            if n == 1:
                return {"content": "未校验的影像结论"}
            self.assertIn("answer_validation", json.dumps(messages))
            return {"tool_calls": [call("finish_answer", {"answer": "影像缺少可读取预览。",
                "observation_ids": [], "evidence_ids": [], "limitations": ["原始预览不可用"]})]}
        execute_run(self.run.pk, provider=provider)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed", self.run.error)
        self.assertEqual(n, 2)
        self.assertEqual(self.conversation.messages.get(role="assistant").content, "影像缺少可读取预览。")

    def test_adopted_steer_updates_public_context_version(self):
        message, _ = submit(self.conversation.id, "reliability-owner", {"content": "改为查看新范围",
            "request_id": uuid.uuid4().hex, "attachment_ids": []})
        execute_run(self.run.pk, provider=lambda *a, **kw: {"content": "已采纳"})
        self.run.refresh_from_db()
        from .v3.common import run_json
        self.assertEqual(run_json(self.run)["context_version"], message.context_version)

    def test_parallel_windows_all_persist_without_lock_errors(self):
        from PIL import Image
        from .v3 import assets
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            path = assets._root() / "windows.png"
            Image.new("RGB", (512, 512), (35, 74, 93)).save(path)
            attachment = SpatialAttachment.objects.create(conversation=self.conversation,
                owner_session_key="reliability-owner", name="windows.png", status="pending", file_path=str(path))
            attachment = assets.process_attachment(attachment.pk)
            self.run.trigger_message.attachments.add(attachment)
            n = 0
            def provider(messages, tools, **kwargs):
                nonlocal n
                n += 1
                if n <= 3:
                    return {"tool_calls": [call("read_image_window", {"attachment_id": str(attachment.pk),
                        "x": x, "y": y, "width": 256, "height": 256, "label": f"局部 {n} {x} {y}"})
                        for x, y in ((0, 0), (256, 0), (0, 256), (256, 256))]}
                refs = list(self.conversation.observations.values_list("id", flat=True))
                return {"tool_calls": [call("finish_answer", {"answer": "已观察原始局部。", "observation_ids": [str(refs[0])], "evidence_ids": [], "limitations": []})]}
            execute_run(self.run.pk, provider=provider)
            self.run.refresh_from_db()
            self.assertEqual(self.run.status, "completed", self.run.error)
            errors = [r.result.get("error") for r in self.run.tool_calls.all() if r.result.get("error")]
            self.assertEqual(self.conversation.observations.count(), 12,
                f"errors={json.dumps(errors, ensure_ascii=False, default=str)}")
            self.assertFalse(errors, json.dumps(errors, ensure_ascii=False, default=str))


class SpatialIntegrityTests(TransactionTestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(owner_session_key="spatial-owner")
        self.attachment = SpatialAttachment.objects.create(conversation=self.conversation,
            owner_session_key="spatial-owner", name="image.tif", width=2000, height=1000, status="ready")
        _, run = submit(self.conversation.id, "spatial-owner", {"content": "看看", "request_id": "spatial",
            "attachment_ids": [str(self.attachment.pk)]})
        self.ctx = {"run": run, "conversation": self.conversation,
            "attachment_ids": [str(self.attachment.pk)], "version": 2}

    def observation(self, window, output):
        return SpatialObservation.objects.create(conversation=self.conversation, attachment=self.attachment,
            label="观察", window=window, metadata={"output_size": output})

    def test_coarse_overview_does_not_inflate_native_coverage(self):
        self.observation([0, 0, 2000, 1000], [1000, 500])
        self.observation([0, 0, 1000, 1000], [1000, 1000])
        self.observation([500, 0, 1000, 1000], [1000, 1000])
        result = coverage({"attachment_id": str(self.attachment.pk)}, self.ctx)
        self.assertEqual(result["observed_fraction"], .75)
        self.assertEqual(result["overview_fraction"], 1)

    def test_pixel_only_direction_requires_correction(self):
        observation = self.observation([0, 0, 1000, 1000], [1000, 1000])
        args = {"answer": "建筑位于水面的北侧", "observation_ids": [str(observation.pk)], "evidence_ids": []}
        with self.assertRaisesRegex(ValueError, "地理方向"):
            finish(args, self.ctx)
        args["answer"] = "建筑位于水面上方"
        self.assertIn("final", finish(args, self.ctx))

    def test_draft_observations_cannot_be_reviewed_or_related(self):
        observation = self.observation([0, 0, 100, 100], [100, 100])
        draft = SpatialAttachment.objects.create(conversation=self.conversation,
            owner_session_key="spatial-owner", name="draft.tif", status="ready")
        hidden = SpatialObservation.objects.create(conversation=self.conversation, attachment=draft,
            label="草稿", window=[0, 0, 100, 100])
        with self.assertRaises(ValueError):
            relation({"first": str(observation.pk), "second": str(hidden.pk)}, self.ctx)
        with self.assertRaises(ValueError):
            vision({"observation_ids": [str(hidden.pk)], "question": "查看"}, self.ctx)


class TransportTests(SimpleTestCase):
    def test_raster_and_upload_do_not_exhaust_chat_read_quota(self):
        factory = RequestFactory()
        middleware = RateLimitMiddleware(lambda request: HttpResponse())
        cases = [("get", "/api/v3/attachments/a/tiles/0/0/0.png", "v3_raster"),
            ("put", "/api/v3/uploads/u/chunks/0", "v3_upload"),
            ("get", "/api/v3/conversations/c", "v3_read"),
            ("post", "/api/v3/conversations/c/messages", "ai")]
        with patch("map_api.middleware.consume_rate_limit", return_value=(True, 0)) as limiter:
            for method, path, scope in cases:
                middleware(getattr(factory, method)(path))
                self.assertEqual(limiter.call_args.args[0], scope)

    def test_coordinates_do_not_call_geocoding_service(self):
        with patch("map_api.v3.places.requests.get") as request:
            self.assertEqual(search("4.312, 51.904")["items"][0]["lon"], 4.312)
            with self.assertRaises(ValueError):
                search("190, 22")
            request.assert_not_called()
