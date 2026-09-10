from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase

from .models import AgentRun, AgentSession, RunStep
from .run_journal import RunCommandConflict, load_checkpoint
from .run_kernel import InvalidRunTransition, sync_session, transition


class RunJournalTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(goal="调查", run_key="journal")

    def test_every_transition_has_a_versioned_event_and_recovery_boundary(self):
        for target in ["planning", "running", "waiting_user", "running", "failed"]:
            transition(self.run.id, target)
        self.assertEqual(list(self.run.events.values_list("sequence", flat=True)), [1, 2, 3, 4, 5])
        self.assertEqual(list(self.run.checkpoints.order_by("sequence").values_list("last_event_sequence", flat=True)), [1, 2, 3, 4, 5])
        saved = load_checkpoint(self.run.id)
        self.assertEqual(saved["state"]["status"], "failed")
        self.assertEqual(saved["state"]["schema_version"], 1)
        self.assertEqual(saved["last_event_sequence"], 5)

    def test_command_replay_is_noop_even_after_later_state_changes(self):
        transition(self.run.id, "planning", command_key="start")
        transition(self.run.id, "running", command_key="execute")
        transition(self.run.id, "planning", command_key="start")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertEqual(self.run.event_sequence, 2)
        self.assertEqual(self.run.checkpoints.count(), 2)
        with self.assertRaises(RunCommandConflict):
            transition(self.run.id, "failed", command_key="start")
        self.assertEqual(self.run.events.count(), 2)

    def test_identical_status_and_context_do_not_duplicate_checkpoint(self):
        for _ in range(3):
            transition(self.run.id, "planning", snapshot={"aoi": {"id": "aoi-1"}})
        self.assertEqual(self.run.events.count(), 1)
        transition(self.run.id, "planning", snapshot={"aoi": {"id": "aoi-2"}})
        self.assertEqual(self.run.events.count(), 2)
        self.run.refresh_from_db()
        self.assertEqual(self.run.context_version, 2)

    def test_checkpoint_failure_rolls_back_event_and_run(self):
        with patch("map_api.run_journal.RunCheckpoint.objects.create", side_effect=RuntimeError("storage failed")):
            with self.assertRaises(RuntimeError):
                transition(self.run.id, "planning")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "queued")
        self.assertEqual(self.run.event_sequence, 0)
        self.assertFalse(self.run.events.exists())

    def test_provider_failure_can_resume_without_stale_terminal_error(self):
        transition(self.run.id, "planning")
        transition(self.run.id, "external_service_unavailable", error="HTTP 429")
        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.completed_at)
        transition(self.run.id, "retrying")
        transition(self.run.id, "running")
        saved = load_checkpoint(self.run.id)["state"]
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["error"], "")
        self.assertIsNone(saved["completed_at"])

    def test_cancelled_and_completed_runs_cannot_be_reopened(self):
        for terminal in ["cancelled", "completed"]:
            run = AgentRun.objects.create(goal="terminal", run_key=terminal, status=terminal)
            for target in ["planning", "running", "retrying", "cancelling"]:
                with self.subTest(terminal=terminal, target=target), self.assertRaises(InvalidRunTransition):
                    transition(run.id, target)
            self.assertFalse(run.events.exists())

    def test_invalid_transition_preserves_structured_diagnostic_without_mutation(self):
        with self.assertRaises(InvalidRunTransition) as caught:
            transition(self.run.id, "completed")
        self.assertEqual(caught.exception.details["current"], "queued")
        self.assertEqual(caught.exception.details["target"], "completed")
        self.assertFalse(self.run.events.exists())

    def test_adapter_checkpoints_after_step_evidence_and_artifact_writes(self):
        transition(self.run.id, "planning")
        RunStep.objects.create(run=self.run, step_id="resolve_aoi", kind="locate", label="AOI", depends_on=[])
        session = AgentSession.objects.create(goal="调查", status="running", slots={"place_name": "AOI"}, artifacts={
            "run_id": self.run.id, "observer": {"current_step": "locate", "current_status": "done"},
            "file_name": "a.jpg", "tool_history": [{"role": "tool_result", "content": "AOI resolved"}],
        })
        sync_session(session.id, snapshot={"evidence": [{"evidence_id": "aoi", "kind": "geometry", "value": [1, 2]}]})
        checkpoint = load_checkpoint(self.run.id)
        self.assertEqual(checkpoint["state"]["steps"][0]["status"], "completed")
        self.assertEqual(checkpoint["state"]["evidence_refs"], ["aoi"])
        self.assertEqual(checkpoint["state"]["artifact_refs"], [f"file_name:{self.run.id}"])
        self.assertEqual(checkpoint["context"]["slots"], {"place_name": "AOI"})
        self.assertEqual(checkpoint["plan"]["steps"][0]["step_id"], "resolve_aoi")
        # Later work must not mutate the immutable recovery boundary.
        session.slots = {"place_name": "another AOI"}
        session.save()
        sync_session(session.id)
        original = self.run.checkpoints.get(sequence=checkpoint["sequence"])
        self.assertEqual(original.context_snapshot["slots"], {"place_name": "AOI"})


class RunEventApiTests(TestCase):
    def setUp(self):
        self.run = AgentRun.objects.create(goal="events", run_key="events")
        owner = self.client.session
        owner.save()
        AgentSession.objects.create(goal="events", owner_session_key=owner.session_key, artifacts={"run_id": self.run.id})
        for target in ["planning", "running", "failed"]:
            transition(self.run.id, target)
        self.url = f"/api/v2/agent/runs/{self.run.id}/events/"

    def test_pages_replay_without_skips_or_duplicates(self):
        first = self.client.get(self.url, {"limit": 2}).json()["data"]
        self.assertEqual([e["sequence"] for e in first["events"]], [1, 2])
        self.assertTrue(first["has_more"])
        second = self.client.get(self.url, {"after": first["next_sequence"]}).json()["data"]
        self.assertEqual([e["sequence"] for e in second["events"]], [3])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["events"][0]["type"], "run.failed")
        self.assertEqual(second["events"][0]["schema_version"], 1)
        self.assertEqual(second, self.client.get(self.url, {"after": 2}).json()["data"])

    def test_sse_last_event_id_resumes_after_committed_cursor(self):
        response = self.client.get(self.url + "stream/", HTTP_LAST_EVENT_ID="2")
        body = b"".join(response.streaming_content).decode()
        self.assertIn("id: 3\n", body)
        self.assertNotIn("id: 2\n", body)
        self.assertIn('"type": "run.failed"', body)
        response = self.client.get(self.url + "stream/", HTTP_LAST_EVENT_ID="3")
        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_both_transports_enforce_ownership_and_validate_cursor(self):
        for suffix in ["", "stream/"]:
            self.assertEqual(Client().get(self.url + suffix).status_code, 404)
            self.assertEqual(self.client.get(self.url + suffix, {"after": "bad"}).status_code, 400)
            self.assertEqual(self.client.get(self.url + suffix, {"after": -1}).status_code, 400)
            self.assertEqual(self.client.get(self.url + suffix, {"after": 20}).status_code, 409)
            self.assertEqual(self.client.post(self.url + suffix).status_code, 405)

    def test_no_event_is_visible_when_checkpoint_transaction_fails(self):
        run = AgentRun.objects.create(goal="rollback", run_key="rollback")
        with patch("map_api.run_journal.RunCheckpoint.objects.create", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                transition(run.id, "planning")
        self.assertFalse(run.events.exists())


class ConcurrentRunJournalTests(TransactionTestCase):
    def test_same_command_from_two_workers_commits_once(self):
        run = AgentRun.objects.create(goal="concurrent", run_key="concurrent")
        ready = Barrier(2)

        def submit():
            close_old_connections()
            try:
                ready.wait(timeout=5)
                transition(run.id, "planning", command_key="same-command", snapshot={"goal": "concurrent"})
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        run.refresh_from_db()
        self.assertEqual(run.status, "planning")
        self.assertEqual(run.event_sequence, 1)
        self.assertEqual(run.events.count(), 1)
        self.assertEqual(run.checkpoints.count(), 1)
