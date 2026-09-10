"""Versioned run journal and immutable checkpoints, committed in the same transaction."""
import hashlib
import json
import uuid
import time
from functools import wraps

from django.db import OperationalError, connection, transaction
from django.db.models import F

from .models import AgentRun, RunCheckpoint, RunEvent


class RunCommandConflict(ValueError):
    def __init__(self, key):
        self.details = {"code": "idempotency_conflict", "command_key": key}
        super().__init__("同一操作标识已用于不同的运行变更")


def retry_sqlite_write(function):
    """Retry only an entire top-level transaction after SQLite lock contention."""
    @wraps(function)
    def call(*args, **kwargs):
        nested = connection.in_atomic_block
        for attempt in range(8):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if nested or connection.vendor != "sqlite" or "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(min(0.005 * 2**attempt, 0.1))
    return call


def digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def lock_run(run_id):
    """Acquire SQLite's write lock before reading; row locks alone are ineffective there.

    Caller must own an atomic transaction. Other databases also serialize updates
    to this row. The no-op UPDATE does not change timestamps or the event cursor.
    """
    if not AgentRun.objects.filter(pk=run_id).update(event_sequence=F("event_sequence")):
        raise AgentRun.DoesNotExist
    return AgentRun.objects.get(pk=run_id)


def repeated_command(run, key, command_digest):
    if not key:
        return None
    if not isinstance(key, str) or len(key) > 160:
        raise ValueError("command_key must be a nonempty string of at most 160 characters")
    event = RunEvent.objects.filter(run=run, command_key=key).first()
    if event and event.command_digest != command_digest:
        raise RunCommandConflict(key)
    return event


def _rows(manager, fields):
    return list(manager.order_by("id").values(*fields))


def snapshots(run, context=None):
    latest = run.checkpoints.order_by("-sequence").first()
    if context is None:
        context = latest.context_snapshot if latest else {}
    state = {
        "schema_version": 1, "run_id": run.id, "status": run.status,
        "goal": run.goal, "mode": run.mode, "provider": run.provider, "model": run.model,
        "execution_engine": run.execution_engine, "cancellation_epoch": run.cancellation_epoch,
        "current_step_id": run.current_step_id, "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "plan_version": run.plan_version, "context_version": run.context_version,
        "steps": _rows(run.steps, ["step_id", "status", "attempt", "error", "input_refs", "output_refs"]),
        "evidence_refs": list(run.evidence_v2.order_by("evidence_id").values_list("evidence_id", flat=True)),
        "artifact_refs": list(run.artifacts_v2.order_by("artifact_id").values_list("artifact_id", flat=True)),
        "tool_calls": _rows(run.tool_calls, ["id", "call_key", "name", "step_id", "status", "attempt"]),
    }
    plan = {
        "schema_version": 1, "version": run.plan_version,
        "steps": _rows(run.steps, ["step_id", "kind", "label", "depends_on", "required_capabilities",
                                  "max_attempts", "retry_policy", "approval_policy", "purpose", "completion_schema",
                                  "failure_conditions", "optional", "allow_replan"]),
    }
    return state, context, plan


def append_locked(run, event_type, payload, *, context=None, command_key=None, command_digest=None, refs=None):
    """Append after reducer mutations, with the caller holding the run write lock."""
    latest = run.checkpoints.order_by("-sequence").first()
    if latest and context is not None and latest.context_snapshot != context:
        run.context_version += 1
        run.save(update_fields=["context_version"])
    state, context, plan = snapshots(run, context)
    if not command_key and latest and (
        latest.state_snapshot == state and latest.context_snapshot == context and latest.plan_snapshot == plan
    ):
        return run.events.filter(sequence=latest.last_event_sequence).first()
    key = command_key or uuid.uuid4().hex
    fingerprint = command_digest or digest({"type": event_type, "payload": payload, "context": context})
    previous = repeated_command(run, key, fingerprint)
    if previous:
        return previous
    sequence = run.event_sequence + 1
    event = RunEvent.objects.create(
        run=run, sequence=sequence, type=event_type, step_id=run.current_step_id,
        payload=payload, refs=refs or [], command_key=key, command_digest=fingerprint,
    )
    RunCheckpoint.objects.create(
        run=run, sequence=(latest.sequence + 1 if latest else 1),
        state_snapshot=state, context_snapshot=context, plan_snapshot=plan,
        last_event_sequence=sequence,
    )
    run.event_sequence = sequence
    run.save(update_fields=["event_sequence", "updated_at"])
    return event


@transaction.atomic
def load_checkpoint(run_id):
    """Read one committed recovery boundary; never reconstruct from public model text."""
    checkpoint = RunCheckpoint.objects.filter(run_id=run_id).order_by("-sequence").first()
    if not checkpoint or checkpoint.state_snapshot.get("schema_version") != 1:
        raise ValueError("没有可独立恢复的版本化 checkpoint")
    if not RunEvent.objects.filter(run_id=run_id, sequence=checkpoint.last_event_sequence).exists():
        raise ValueError("checkpoint 的事件游标不完整")
    return {
        "sequence": checkpoint.sequence, "last_event_sequence": checkpoint.last_event_sequence,
        "state": checkpoint.state_snapshot, "context": checkpoint.context_snapshot,
        "plan": checkpoint.plan_snapshot,
    }


def event_payload(event):
    return {
        "schema_version": event.schema_version, "sequence": event.sequence,
        "run_id": event.run_id, "type": event.type, "timestamp": event.timestamp.isoformat(),
        "step_id": event.step_id, "payload": event.payload, "refs": event.refs,
    }
