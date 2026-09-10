import os
from unittest.mock import patch

from django.test import Client, TestCase

from .models import AgentRun, AgentSession, RunArtifact, RunEvidence
from .run_kernel import sync_session


class RunControlApiTests(TestCase):
    def create(self, **extra):
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "thread"}), \
             patch("map_api.orchestrator.threading.Thread") as thread:
            response = self.client.post("/api/v2/agent/runs/", {"goal": "调查水体", **extra}, content_type="application/json")
        thread.assert_not_called()
        self.assertEqual(response.status_code, 202, response.content)
        return AgentRun.objects.get(pk=response.json()["data"]["id"])

    def test_create_is_persisted_queue_even_when_sync_is_requested(self):
        run = self.create(sync=True)
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.events.get().type, "run.created")
        session = AgentSession.objects.get(artifacts__run_id=run.id)
        self.assertEqual(session.artifacts["execution_mode"], "queue")
        self.assertNotIn("worker_claim", session.artifacts)
        self.assertEqual(len(self.client.get("/api/v2/agent/runs/").json()["data"]), 1)
        self.assertEqual(Client().get("/api/v2/agent/runs/").json()["data"], [])

    def test_creation_idempotency_conflict_and_replay(self):
        run = self.create(request_id="create-once")
        same = self.create(request_id="create-once")
        self.assertEqual(run.id, same.id)
        response = self.client.post("/api/v2/agent/runs/", {"goal": "不同任务", "request_id": "create-once"}, content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AgentRun.objects.count(), 1)

    def test_artifacts_evidence_and_controls_are_owner_isolated(self):
        run = self.create()
        RunEvidence.objects.create(run=run, evidence_id="e1", kind="metric", mask_statistics={"valid": 42}, data_contract={"version": 1})
        RunArtifact.objects.create(run=run, artifact_id="a1", kind="result", title="结果", uri="urn:result:1", evidence_refs=["e1"])
        for endpoint in ["artifacts", "evidence"]:
            url = f"/api/v2/agent/runs/{run.id}/{endpoint}/"
            self.assertEqual(Client().get(url).status_code, 404)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["data"]), 1)
        url = f"/api/v2/agent/runs/{run.id}/actions/"
        self.assertEqual(Client().post(url, {"action": "cancel"}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "continue"}, content_type="application/json").status_code, 400)

    def test_queued_cancel_and_replay_do_not_start_worker(self):
        run = self.create()
        url = f"/api/v2/agent/runs/{run.id}/actions/"
        body = {"action": "cancel", "message_id": "cancel-once"}
        first = self.client.post(url, body, content_type="application/json")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["data"]["status"], "cancelled")
        count = run.events.count()
        second = self.client.post(url, body, content_type="application/json")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(run.events.count(), count)

    def test_resume_preserves_worker_queue_in_thread_environment(self):
        run = self.create()
        session = AgentSession.objects.get(artifacts__run_id=run.id)
        sync_session(session.id)
        # New runs are controlled by the DAG reducer, not the legacy projection.
        from .run_scheduler import claim_step, fail_step
        claim = claim_step(run.id, "worker")
        fail_step(claim, "服务暂不可用", status="external_service_unavailable", retryable=True)
        session.status = "waiting_user"
        session.artifacts["waiting"] = {"message": "服务暂不可用", "options": [{"code": "retry_step", "label": "重试"}], "data": {"failed_phase": "planning"}}
        session.save()
        sync_session(session.id)
        with patch.dict(os.environ, {"AGENT_EXECUTION_MODE": "thread"}), patch("map_api.orchestrator.threading.Thread") as thread:
            response = self.client.post(f"/api/v2/agent/runs/{run.id}/actions/", {"action": "retry_step"}, content_type="application/json")
        thread.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        session.refresh_from_db()
        self.assertNotIn("worker_claim", session.artifacts)
        run.refresh_from_db()
        self.assertEqual(run.status, "retrying")
        self.assertEqual(run.steps.get(step_id="understand_goal").status, "queued")

    def test_replan_versions_and_clears_stale_context_with_idempotency(self):
        run = self.create()
        session = AgentSession.objects.get(artifacts__run_id=run.id)
        sync_session(session.id)
        from .run_scheduler import control_run
        control_run(run.id, "pause", command_key="pause-before-replan")
        session.status = "waiting_user"
        session.artifacts["ndwi"] = {"available": True, "water_percent": 99}
        session.save()
        sync_session(session.id)
        checkpoint_ids = list(run.checkpoints.values_list("id", flat=True))
        url = f"/api/v2/agent/runs/{run.id}/replan/"
        data = {"goal": "检查新区域植被", "conditions": {"source": "sentinel2"}, "request_id": "replan-once"}
        first = self.client.post(url, data, content_type="application/json")
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(first.json()["data"]["plan_version"], 2)
        second = self.client.post(url, data, content_type="application/json")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["data"]["plan_version"], 2)
        self.assertEqual(run.checkpoints.filter(pk__in=checkpoint_ids).count(), len(checkpoint_ids))
        session.refresh_from_db()
        self.assertNotIn("ndwi", session.artifacts)
        self.assertEqual(session.slots, {"source": "sentinel2"})
        self.assertTrue(session.artifacts["retry_planning"])
        conflict = self.client.post(url, {**data, "goal": "另一个目标"}, content_type="application/json")
        self.assertEqual(conflict.status_code, 409)

    def test_replan_does_not_overwrite_running_or_other_owners_runs(self):
        run = self.create()
        url = f"/api/v2/agent/runs/{run.id}/replan/"
        self.assertEqual(Client().post(url, {}, content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post(url, {}, content_type="application/json").status_code, 409)

    def test_queued_pause_is_immediate_and_idempotent(self):
        run = self.create()
        url = f"/api/v2/agent/runs/{run.id}/actions/"
        response = self.client.post(url, {"action": "pause"}, content_type="application/json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["status"], "waiting_user")
        count = run.events.count()
        self.client.post(url, {"action": "pause"}, content_type="application/json")
        self.assertEqual(run.events.count(), count)
        session = AgentSession.objects.get(artifacts__run_id=run.id)
        self.assertEqual(session.status, "waiting_user")
        self.assertTrue(session.artifacts["waiting"]["data"]["paused_by_user"])
