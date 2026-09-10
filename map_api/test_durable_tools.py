from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from .agent.durable_tools import ToolClaimLost, authorize_retry, invoke
from .agent.registry import ToolDefinition
from .agent.waiting import WaitingForUser
from .models import AgentRun, AgentSession
from .run_journal import load_checkpoint


class DurableToolTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(goal="vegetation", run_key="tools", status="running")
        self.session = AgentSession.objects.create(goal=self.run.goal, status="running", artifacts={"run_id": self.run.id, "worker_claim": "worker-1"})
        self.ctx = {"goal": self.run.goal, "slots": {}, "scene_id": 1}

    def definition(self, fn):
        return ToolDefinition("compute_ndwi", "index", {"type": "object", "additionalProperties": False}, fn)

    def call(self, definition, ctx=None):
        return invoke(self.session, definition, ctx if ctx is not None else self.ctx, {}, "compute_metric", "worker-1")

    def test_success_is_replayed_after_memory_loss_without_executing_tool(self):
        def compute(ctx, args):
            ctx["ndwi"] = {"value": 0.3}
            ctx["slots"]["computed"] = True
            return {"status": "ok", "result": ctx["ndwi"]}
        fn = Mock(side_effect=compute)
        definition = self.definition(fn)
        first = self.call(definition)
        restored = {"goal": self.run.goal, "slots": {"user_note": "retained"}, "scene_id": 1}
        second = self.call(definition, restored)
        self.assertEqual(first, second)
        fn.assert_called_once()
        self.assertEqual(restored["ndwi"], {"value": 0.3})
        self.assertEqual(restored["slots"]["user_note"], "retained")
        self.assertTrue(restored["slots"]["computed"])
        self.assertEqual(self.run.tool_calls.count(), 1)
        self.assertEqual(load_checkpoint(self.run.id)["state"]["tool_calls"][0]["status"], "completed")

    def test_scene_change_does_not_reuse_old_result(self):
        fn = Mock(return_value={"status": "ok", "result": {"value": 0.1}})
        self.call(self.definition(fn))
        self.ctx["scene_id"] = 2
        self.call(self.definition(fn))
        self.assertEqual(fn.call_count, 2)
        self.assertEqual(self.run.tool_calls.count(), 2)

    def test_failed_and_waiting_calls_are_not_success_receipts(self):
        fn = Mock(side_effect=[WaitingForUser("cloud", []), {"status": "error", "message": "unavailable"}, {"status": "ok", "result": {}}])
        definition = self.definition(fn)
        with self.assertRaises(WaitingForUser):
            self.call(definition)
        self.assertEqual(self.run.tool_calls.get().status, "waiting")
        self.call(definition)
        self.assertEqual(self.run.tool_calls.get().status, "failed")
        self.call(definition)
        self.assertEqual(self.run.tool_calls.get().status, "completed")
        self.assertEqual(self.run.tool_calls.get().attempt, 3)
        self.assertEqual(fn.call_count, 3)

    def test_cancel_before_call_never_invokes_tool(self):
        self.session.cancel_requested = True
        self.session.save()
        fn = Mock()
        with self.assertRaises(ToolClaimLost):
            self.call(self.definition(fn))
        fn.assert_not_called()
        self.assertFalse(self.run.tool_calls.exists())

    def test_worker_takeover_during_call_rejects_old_completion(self):
        def compute(ctx, args):
            self.session.artifacts["worker_claim"] = "worker-2"
            self.session.save()
            return {"status": "ok", "result": {"value": 1}}
        with self.assertRaises(ToolClaimLost):
            self.call(self.definition(compute))
        self.assertEqual(self.run.tool_calls.get().status, "running")
        self.assertFalse(self.run.events.filter(type="tool.completed").exists())

    def test_interrupted_call_requires_expiry_and_explicit_retry(self):
        def interrupt(ctx, args):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.call(self.definition(interrupt))
        call = self.run.tool_calls.get()
        fn = Mock(return_value={"status": "ok", "result": {}})
        with self.assertRaises(WaitingForUser):
            self.call(self.definition(fn))
        self.assertFalse(authorize_retry(self.session, call.call_key))
        call.lease_until = timezone.now() - timedelta(seconds=1)
        call.save(update_fields=["lease_until"])
        with self.assertRaises(WaitingForUser):
            self.call(self.definition(fn))
        fn.assert_not_called()
        self.assertTrue(authorize_retry(self.session, call.call_key))
        self.call(self.definition(fn))
        fn.assert_called_once()

    def test_argument_validation_happens_before_any_call_record_or_tool(self):
        fn = Mock()
        with self.assertRaises(ValueError):
            invoke(self.session, self.definition(fn), self.ctx, {"unexpected": 1}, "compute_metric", "worker-1")
        self.assertFalse(self.run.tool_calls.exists())
        fn.assert_not_called()

    def test_unavailable_metric_is_failed_and_not_replayed_as_success(self):
        fn = Mock(side_effect=[
            {"status": "ok", "result": {"available": False, "reason": "缺少 SCL"}},
            {"status": "ok", "result": {"available": True, "value": 0.1}},
        ])
        first = self.call(self.definition(fn))
        self.assertEqual(first["status"], "error")
        self.assertEqual(self.run.tool_calls.get().status, "failed")
        self.assertEqual(self.call(self.definition(fn))["status"], "ok")
        self.assertEqual(fn.call_count, 2)

    def test_failed_tool_cannot_leave_success_facts_in_memory(self):
        def failed(ctx, args):
            ctx["ndwi"] = {"available": True, "value": 0.9}
            return {"status": "failed", "message": "storage error"}
        self.call(self.definition(failed))
        self.assertNotIn("ndwi", self.ctx)
        self.assertEqual(self.run.tool_calls.get().context_patch, {})

    def test_deleted_output_requires_explicit_retry_instead_of_stale_success(self):
        def fetch(ctx, args):
            ctx["file_name"] = "result.jpg"
            return {"status": "ok", "result": {}}
        fn = Mock(side_effect=fetch)
        definition = ToolDefinition("search_sentinel_imagery", "", {"type": "object"}, fn)
        self.call(definition)
        with patch("map_api.agent.durable_tools._missing_output", return_value=True):
            with self.assertRaises(WaitingForUser):
                self.call(definition)
            self.assertEqual(fn.call_count, 1)
            self.assertTrue(authorize_retry(self.session, self.run.tool_calls.get().call_key))
        self.call(definition)
        self.assertEqual(fn.call_count, 2)

    def test_pause_keeps_inflight_result_but_prevents_next_tool(self):
        def compute(ctx, args):
            self.session.artifacts["pause_requested"] = True
            self.session.save()
            return {"status": "ok", "result": {"value": 1}}
        self.call(self.definition(compute))
        self.assertEqual(self.run.tool_calls.get().status, "completed")
        self.ctx["scene_id"] = 2
        fn = Mock()
        with self.assertRaises(WaitingForUser):
            self.call(self.definition(fn))
        fn.assert_not_called()
        self.assertEqual(self.run.tool_calls.count(), 1)
