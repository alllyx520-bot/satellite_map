from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .models import AgentRun, RunStep
from .run_journal import load_checkpoint
from .run_scheduler import (StepConflict, claim_step, control_run, fail_step, finish_step,
                            install_plan, renew_step, validate_plan)


def plan():
    return [{"step_id": sid, "kind": "pure", "label": sid, "purpose": "验证依赖执行",
             "depends_on": deps, "max_attempts": 3, "retry_policy": {"recoverable": True},
             "completion_schema": {"type": "object", "required": ["value"], "properties": {"value": {"type": "number"}}}}
            for sid, deps in [("a", []), ("b", ["a"]), ("c", ["b"])]]


class SchedulerTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(goal="DAG", run_key="dag")
        install_plan(self.run.id, plan(), context={"goal": "DAG"}, command_key="plan", mandatory=set())

    def test_dependency_order_and_restart_skip_completed_steps(self):
        first = claim_step(self.run.id, "worker-1")
        self.assertEqual(first.step_id, "a")
        self.assertIsNone(claim_step(self.run.id, "worker-2"))
        finish_step(first, {"value": 10}, context_patch={"aoi": {"id": "aoi-1"}})
        recovered = load_checkpoint(self.run.id)
        self.assertEqual(recovered["context"]["aoi"], {"id": "aoi-1"})
        self.assertEqual(recovered["context"]["outputs"]["a"], first.output_refs[0] if first.output_refs else "step:1:a")
        next_step = claim_step(self.run.id, "worker-2")
        self.assertEqual(next_step.step_id, "b")
        self.assertEqual(self.run.steps.get(step_id="a").attempt, 1)

    def test_expired_lease_can_be_taken_over_and_old_result_rejected(self):
        old = claim_step(self.run.id, "old")
        RunStep.objects.filter(pk=old.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        new = claim_step(self.run.id, "new")
        self.assertEqual(new.pk, old.pk)
        self.assertEqual(new.attempt, 2)
        with self.assertRaises(StepConflict):
            finish_step(old, {"value": 1})
        with self.assertRaises(StepConflict):
            renew_step(old)
        finish_step(new, {"value": 2})
        self.assertEqual(self.run.artifacts_v2.get().metadata, {"value": 2})

    def test_failed_output_schema_does_not_complete_or_create_artifact(self):
        step = claim_step(self.run.id, "worker")
        with self.assertRaises(StepConflict):
            finish_step(step, {"value": "invented"})
        self.assertFalse(self.run.artifacts_v2.exists())
        step.refresh_from_db()
        self.assertEqual(step.status, "running")

    def test_checkpoint_failure_rolls_back_step_and_artifact(self):
        step = claim_step(self.run.id, "worker")
        with patch("map_api.run_journal.RunCheckpoint.objects.create", side_effect=RuntimeError("disk")), self.assertRaises(RuntimeError):
            finish_step(step, {"value": 1})
        step.refresh_from_db()
        self.assertEqual(step.status, "running")
        self.assertFalse(self.run.artifacts_v2.exists())

    def test_cancel_fences_active_step_and_replay_is_idempotent(self):
        step = claim_step(self.run.id, "worker")
        control_run(self.run.id, "cancel", command_key="cancel")
        cursor = self.run.events.count()
        control_run(self.run.id, "cancel", command_key="cancel")
        self.assertEqual(self.run.events.count(), cursor)
        with self.assertRaises(StepConflict):
            finish_step(step, {"value": 1})
        self.assertIsNone(claim_step(self.run.id, "worker"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "cancelled")

    def test_retry_preserves_success_and_clears_only_failed_step(self):
        a = claim_step(self.run.id, "worker")
        finish_step(a, {"value": 1})
        b = claim_step(self.run.id, "worker")
        fail_step(b, "HTTP 429", status="external_service_unavailable", retryable=True)
        control_run(self.run.id, "retry_step", command_key="retry")
        retry = claim_step(self.run.id, "worker")
        self.assertEqual((retry.step_id, retry.attempt), ("b", 2))
        self.assertEqual(self.run.steps.get(step_id="a").status, "completed")
        self.run.refresh_from_db()
        self.assertEqual(self.run.error, "")

    def test_pause_accepts_inflight_result_but_prevents_next_claim(self):
        step = claim_step(self.run.id, "worker")
        control_run(self.run.id, "pause", command_key="pause")
        finish_step(step, {"value": 1})
        self.assertIsNone(claim_step(self.run.id, "worker"))
        control_run(self.run.id, "resume", command_key="resume")
        self.assertEqual(claim_step(self.run.id, "worker").step_id, "b")

    def test_completed_receipt_is_idempotent_and_plan_validation_is_atomic(self):
        step = claim_step(self.run.id, "worker")
        finish_step(step, {"value": 1})
        finish_step(step, {"value": 1})
        self.assertEqual(self.run.artifacts_v2.count(), 1)
        for variant in [plan() + [plan()[0]], [{**item, "depends_on": ["c"]} for item in plan()], plan()[1:]]:
            with self.assertRaises(StepConflict):
                validate_plan(variant, mandatory=set())
        with self.assertRaises(StepConflict):
            validate_plan(plan())


class SchedulerConcurrencyTests(TransactionTestCase):
    def test_two_workers_claim_one_step_once(self):
        run = AgentRun.objects.create(goal="concurrency", run_key="concurrent-dag")
        install_plan(run.id, plan(), context={}, command_key="plan", mandatory=set())
        barrier = Barrier(2)
        def claim(worker):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                result = claim_step(run.id, worker)
                return result.pk if result else None
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["one", "two"]))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(run.steps.get(step_id="a").attempt, 1)
        self.assertEqual(run.events.filter(type="step.started").count(), 1)
