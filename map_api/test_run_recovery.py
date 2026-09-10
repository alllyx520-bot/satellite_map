"""Regression tests for durable state and access boundaries, without live services."""
from unittest.mock import patch

from django.test import TestCase

from .models import AgentRun, AgentSession, RunStep
from .run_kernel import evidence_key, sync_session, transition


class RunRecoveryTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(goal="调查", run_key="recovery", status="running")
        self.session = AgentSession.objects.create(
            goal=self.run.goal, artifacts={"run_id": self.run.id}, status="running",
        )

    def test_cancel_waits_for_worker_then_acknowledges_and_preserves_timestamp(self):
        self.session.cancel_requested = True
        self.session.status = "failed"
        self.session.artifacts["worker_claim"] = "worker-one"
        self.session.save()
        sync_session(self.session.id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelling")
        self.assertIsNone(self.run.completed_at)
        from .run_kernel import acknowledge_cancel
        acknowledge_cancel(self.session.id, "stale-worker")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelling")
        acknowledge_cancel(self.session.id, "worker-one")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")
        completed_at = self.run.completed_at
        sync_session(self.session.id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.completed_at, completed_at)

    def test_late_cancel_does_not_rewrite_completed_run(self):
        transition(self.run.id, "completed")
        self.session.cancel_requested = True
        self.session.status = "failed"
        self.session.save()
        sync_session(self.session.id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")

    def test_evidence_identity_is_canonical_and_not_position_or_python_hash_based(self):
        item = {"tool": "compute_ndwi", "summary": {"mean": 0.4, "valid": 100}}
        reordered = {"summary": {"valid": 100, "mean": 0.4}, "tool": "compute_ndwi"}
        self.assertEqual(evidence_key(item), evidence_key(reordered))
        with patch("builtins.hash", side_effect=AssertionError("process-specific hash")):
            key = evidence_key(item)
        sync_session(self.session.id, snapshot={"evidence": [item]})
        sync_session(self.session.id, snapshot={"evidence": [{"tool": "geocode"}, reordered]})
        self.assertEqual(self.run.evidence_v2.count(), 2)
        evidence = self.run.evidence_v2.get(evidence_id=key)
        self.assertEqual(evidence.value, item["summary"])
        self.assertEqual(evidence.method, "compute_ndwi")

    def test_invalid_evidence_rolls_back_state_checkpoint_and_artifacts(self):
        self.session.status = "waiting_user"
        self.session.save()
        with self.assertRaises(ValueError):
            sync_session(self.session.id, snapshot={"evidence": [{"evidence_id": "x" * 161}]})
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertFalse(self.run.checkpoints.exists())
        self.assertFalse(self.run.artifacts_v2.exists())

    def test_legacy_step_name_and_explicit_status_are_mapped_without_invented_completion(self):
        step = RunStep.objects.create(run=self.run, step_id="resolve_aoi", kind="locate", label="AOI")
        final = RunStep.objects.create(run=self.run, step_id="final_review", kind="review", label="review")
        self.session.artifacts["observer"] = {"current_step": "locate", "current_status": "done"}
        self.session.save()
        sync_session(self.session.id)
        step.refresh_from_db()
        self.assertEqual(step.status, "completed")
        self.session.status = "failed"
        self.session.artifacts["observer"] = {"current_step": "failed", "current_status": "failed"}
        self.session.save()
        sync_session(self.session.id)
        final.refresh_from_db()
        self.assertEqual(final.status, "queued")
        self.run.refresh_from_db()
        self.assertEqual(self.run.current_step_id, "resolve_aoi")

    def test_final_answer_text_is_not_used_as_uri_or_truncated(self):
        answer = "调查结论" * 300
        self.session.artifacts["final_answer"] = answer
        self.session.save()
        sync_session(self.session.id)
        artifact = self.run.artifacts_v2.get(artifact_id=f"final_answer:{self.run.id}")
        self.assertTrue(artifact.uri.startswith("urn:satellitesense:"))
        self.assertEqual(artifact.metadata["value"], answer)

    def test_stale_worker_cannot_persist_evidence_after_takeover(self):
        self.session.artifacts["worker_claim"] = "new-worker"
        self.session.save()
        result = sync_session(self.session.id, expected_claim="old-worker", snapshot={"evidence": [{"value": 1}]})
        self.assertIsNone(result)
        self.assertFalse(self.run.evidence_v2.exists())
        self.assertFalse(self.run.checkpoints.exists())

    def test_planning_failure_stops_before_decision_or_tool_execution(self):
        from .agent.loop import run_agent_loop
        with patch("map_api.agent.loop.build_agent_plan", side_effect=TimeoutError("unavailable")), \
             patch("map_api.agent.loop.agent_step") as decision:
            run_agent_loop(self.session.id)
        decision.assert_not_called()
        self.session.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.session.status, "waiting_user")
        self.assertEqual(self.run.status, "waiting_user")
        self.assertFalse(self.session.artifacts.get("final_answer"))

    def test_decision_failure_never_uses_rule_tools_or_final_answer(self):
        from .agent.loop import _run_loop
        context = {"goal": "water", "slots": {}, "vision_calls": 0}
        with patch("map_api.agent.loop.refresh_ctx_scene"), \
             patch("map_api.agent.loop.agent_step", side_effect=TimeoutError("unavailable")), \
             patch("map_api.agent.loop._deterministic_next_call") as fallback:
            _run_loop(self.session, context, [], {})
        fallback.assert_not_called()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "waiting_user")
        self.assertFalse(self.session.artifacts.get("final_answer"))

    def test_planning_retry_reenters_planner_instead_of_skipping_to_decision(self):
        from .agent.loop import run_agent_loop, resume_waiting_agent_session
        with patch("map_api.agent.loop.build_agent_plan", side_effect=TimeoutError("offline")):
            run_agent_loop(self.session.id)
        self.session.refresh_from_db()
        with patch("map_api.views._run_agent_background") as background:
            resume_waiting_agent_session(self.session, "retry_step")
        context = background.call_args.args[1]
        with patch("map_api.agent.loop.build_agent_plan", return_value={"slots": {}, "steps": []}) as planner, \
             patch("map_api.agent.loop._run_loop"):
            run_agent_loop(self.session.id, context)
        planner.assert_called_once()
        self.session.refresh_from_db()
        self.assertNotIn("retry_planning", self.session.artifacts)

    def test_worker_restart_restores_metric_evidence_and_steps_from_checkpoint(self):
        from .agent.loop import _persist, run_agent_loop
        ctx = {
            "slots": {"task": "water"}, "ndwi": {"available": True, "water_percent": 12},
            "facts": {"compute_ndwi": {"water_percent": 12}},
            "evidence": [{"evidence_id": "metric-1", "metric": "NDWI", "value": 12}],
            "completed_steps": ["understand", "locate", "ndwi"],
        }
        history = [{"role": "tool_result", "content": "NDWI complete"}]
        _persist(self.session, ctx, history)
        with patch("map_api.agent.loop.build_agent_plan") as planner, \
             patch("map_api.agent.loop._run_loop") as loop:
            run_agent_loop(self.session.id, {"worker_claim": None})
        planner.assert_not_called()
        recovered = loop.call_args.args[1]
        self.assertEqual(recovered["ndwi"], ctx["ndwi"])
        self.assertEqual(recovered["completed_steps"], ctx["completed_steps"])
        self.assertEqual(recovered["evidence"], ctx["evidence"])
        self.assertEqual(loop.call_args.args[2], history)

    def test_explicit_resume_keeps_evidence_and_current_user_slots(self):
        from .agent.loop import _persist, run_agent_loop
        ctx = {"slots": {"task": "water"}, "ndwi": {"available": True},
               "evidence": [{"evidence_id": "metric-1"}], "completed_steps": ["ndwi"]}
        _persist(self.session, ctx, [])
        self.session.refresh_from_db()
        self.session.slots["date_end"] = "2026-09-08"
        self.session.save(update_fields=["slots"])
        history = [{"role": "tool_result", "content": "retry"}]
        with patch("map_api.agent.loop._run_loop") as loop:
            run_agent_loop(self.session.id, {"tool_history": history, "follow_up": "解释阈值"})
        recovered = loop.call_args.args[1]
        self.assertEqual(recovered["evidence"], ctx["evidence"])
        self.assertEqual(recovered["ndwi"], ctx["ndwi"])
        self.assertEqual(recovered["slots"]["date_end"], "2026-09-08")
        self.assertEqual(recovered["follow_up"], "解释阈值")

    def test_final_gate_rejects_invalid_refs_old_scene_missing_metric_and_limits(self):
        from .agent.loop import _complete
        base = {"slots": {"task": "water"}, "scene_id": 12, "file_name": "evidence.jpg",
                "vision_answer": "视觉复核", "evidence": [
                    {"evidence_id": "visual", "tool": "analyze_imagery", "scene_id": 12},
                    {"evidence_id": "metric", "metric": "ndwi", "scene_id": 12, "summary": {"available": True}},
                    {"evidence_id": "old", "metric": "ndwi", "scene_id": 11, "summary": {"available": True}},
                ]}
        cases = [([], ["限制"]), ([{}], ["限制"]), ("visual", ["限制"]),
                 (["forged"], ["限制"]), (["visual", "old"], ["限制"]),
                 (["visual"], ["限制"]), (["visual", "metric"], []),
                 (["visual", "metric"], [" "])]
        for refs, limits in cases:
            with self.subTest(refs=refs, limits=limits), \
                 patch("map_api.agent.loop._ctx_scene", return_value=None), \
                 patch("map_api.agent.loop._quality_warning", return_value=""), \
                 patch("map_api.agent.loop._quality_requires_safe_fallback", return_value=False), \
                 patch("map_api.agent.loop._views._agent_wait") as waiting:
                _complete(self.session, base, "复核结论", [], evidence_refs=refs, limitations=limits)
            waiting.assert_called_once()
            self.session.refresh_from_db()
            self.assertNotEqual(self.session.status, "completed")
            self.assertFalse(self.session.artifacts.get("final_answer"))
