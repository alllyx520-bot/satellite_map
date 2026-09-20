"""Regression tests for conversation semantics and the real V3 execution loop."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TransactionTestCase
from django.utils import timezone

from .models import AgentRun, AgentTurn, Conversation, ConversationMessage, RunToolCall
from .v3 import harness
from .v3.conversations import control, submit
from .v3.harness import claim_run, execute_run
from .v3.tools import Tool, schema


def call(name, args):
    return {"id": uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class HarnessTests(TransactionTestCase):
    def setUp(self):
        self.c = Conversation.objects.create(owner_session_key="owner")

    def message(self, content="你好", **extra):
        return submit(self.c.id, "owner", {"content": content, "attachment_ids": [], "request_id": uuid.uuid4().hex, **extra})

    def test_dynamic_tool_loop_persists_plan_and_publishes_answer(self):
        _, run = self.message()
        replies = iter([
            {"tool_calls": [call("update_plan", {"steps": [{"label": "理解当前问题", "status": "completed"}]})]},
            {"tool_calls": [call("finish_answer", {"answer": "可以上传影像或在地图框选区域。", "observation_ids": [], "evidence_ids": [], "limitations": []})]},
        ])
        self.assertTrue(execute_run(run.id, provider=lambda *a, **kw: next(replies)))
        run.refresh_from_db(); self.c.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.tool_calls.count(), 2)
        self.assertEqual(run.usage["controller_calls"], 2)
        self.assertIsNone(self.c.active_run_id)
        self.assertIn("上传影像", self.c.messages.get(role="assistant").content)
        self.assertTrue(run.checkpoints.exists())

    def test_steer_adopted_before_final_and_fifo_queue_starts_after(self):
        _, run = self.message("原问题")
        steering, same = self.message("补充要求")
        queued, _ = self.message("接着问另一个", delivery="queue")
        self.assertEqual(same.id, run.id)
        self.assertEqual(steering.status, "pending")
        self.assertEqual(queued.status, "queued")
        def provider(messages, tools, **kw):
            self.assertIn("补充要求", json.dumps(messages, ensure_ascii=False))
            self.assertNotIn("接着问另一个", json.dumps(messages, ensure_ascii=False))
            return {"content": "回答补充后的问题"}
        execute_run(run.id, provider=provider)
        steering.refresh_from_db(); queued.refresh_from_db(); self.c.refresh_from_db()
        self.assertEqual(steering.status, "adopted")
        self.assertNotEqual(self.c.active_run_id, run.id)
        self.assertEqual(queued.run_id, self.c.active_run_id)

    def test_pending_steer_during_model_call_prevents_obsolete_publication(self):
        _, run = self.message()
        n = 0
        def provider(*a, **kw):
            nonlocal n
            n += 1
            if n == 1:
                self.message("改为只解释上传方法")
                return {"content": "旧答案"}
            return {"content": "新答案"}
        execute_run(run.id, provider=provider)
        self.assertEqual(self.c.messages.get(role="assistant").content, "新答案")

    def test_worker_recovery_reuses_committed_decision(self):
        _, run = self.message()
        token = claim_run(run.id)
        AgentTurn.objects.create(run=run, number=1, context_version=run.context_version,
                                 status="decided", decision={"content": "已保存答案"})
        AgentRun.objects.filter(pk=run.id).update(lease_until=timezone.now() - timedelta(seconds=1))
        def forbidden(*a, **kw):
            self.fail("recovery must not repeat a committed model decision")
        execute_run(run.id, provider=forbidden)
        self.assertEqual(self.c.messages.get(role="assistant").content, "已保存答案")

    def test_idempotency_conflict_and_cancel_replay(self):
        key = uuid.uuid4().hex
        m, run = self.message(request_id=key)
        again, _ = self.message(request_id=key)
        self.assertEqual(m.id, again.id)
        with self.assertRaisesMessage(ValueError, "同一消息标识"):
            self.message("不同内容", request_id=key)
        action = {"action": "stop", "request_id": "stop-once"}
        control(run.id, "owner", action)
        control(run.id, "owner", action)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.cancellation_epoch, 1)

    def test_repeated_tool_error_is_returned_to_model_for_strategy_change(self):
        _, run = self.message()
        n = 0
        def provider(messages, tools, **kwargs):
            nonlocal n
            n += 1
            if n <= 4:
                return {"tool_calls": [call('read_history', {})]}
            self.assertIn('no_progress', json.dumps(messages, ensure_ascii=False))
            return {"content": '已读取历史，可以继续提问。'}
        execute_run(run.pk, provider=provider)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.usage['controller_calls'], 5)

    def test_ingest_interrupted_resume_does_not_trigger_no_progress(self):
        def handler(args, ctx):
            return {"error": {"code": "ingest_interrupted", "message": "下载已在时限边界暂停并保留进度（已完成 2/9 块）", "retryable": True}}
        extra = {"retrieve_imagery": Tool("retrieve_imagery", "下载影像", schema({"bbox": {"type": "string"}}), handler)}
        real_registry = harness.registry
        def patched():
            tools = real_registry()
            tools.update(extra)
            return tools
        _, run = self.message()
        calls = 0
        def provider(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 4:
                return {"tool_calls": [call("retrieve_imagery", {"bbox": "same"})]}
            text = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn("no_progress", text)
            self.assertIn("ingest_interrupted", text)
            return {"content": "续传完成后作答"}
        with patch.object(harness, "registry", patched):
            self.assertTrue(execute_run(run.id, provider=provider))
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.tool_calls.filter(name="retrieve_imagery").count(), 4)
        failures = harness._context(run)["recent_failures"]["retrieve_imagery"]
        self.assertIn("续传", failures["next_step"])
        self.assertNotIn("同类错误持续", failures["next_step"])

    def test_next_turn_keeps_native_tool_result_pair(self):
        _, run = self.message('读取已有历史')
        invocation = call('read_history', {})
        calls = 0
        def provider(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'content': '正在检查历史', 'tool_calls': [invocation]}
            paired = [m for m in messages if m['role'] == 'tool']
            self.assertEqual(len(paired), 1)
            self.assertEqual(paired[0]['tool_call_id'], invocation['id'])
            self.assertIn('读取已有历史', paired[0]['content'])
            preceding = messages[messages.index(paired[0])-1]
            self.assertEqual(preceding['role'], 'assistant')
            self.assertEqual(preceding['tool_calls'][0]['id'], invocation['id'])
            return {'content': '已检查历史'}
        execute_run(run.id, provider=provider)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')

    def test_budget_saves_checkpoint_and_requires_explicit_extension(self):
        _, run = self.message()
        run.budget = {"controller_calls": 0}; run.save()
        execute_run(run.id, provider=lambda *a, **kw: self.fail("no budget"))
        run.refresh_from_db()
        self.assertEqual(run.status, "budget_exhausted")
        with self.assertRaisesMessage(ValueError, "预算"):
            control(run.id, "owner", {"action": "resume", "request_id": "resume"})
        control(run.id, "owner", {"action": "extend_budget", "request_id": "extend", "budget": {"controller_calls": 2}})
        execute_run(run.id, provider=lambda *a, **kw: {"content": "继续后的回答"})
        run.refresh_from_db(); self.assertEqual(run.status, "completed")


class ConversationAPITests(TransactionTestCase):
    def test_real_api_owner_scope_and_event_cursor(self):
        first, second = Client(), Client()
        response = first.post("/api/v3/conversations", {"title": "测试"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        id = response.json()["conversation"]["id"]
        response = first.post(f"/api/v3/conversations/{id}/messages", {"content": "查看区域", "attachment_ids": [], "request_id": "m1"}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        run = response.json()["run"]["id"]
        self.assertEqual(second.get(f"/api/v3/conversations/{id}").status_code, 404)
        self.assertEqual(second.get(f"/api/v3/runs/{run}").status_code, 404)
        self.assertEqual(second.get(f"/api/v3/conversations/{id}/events?format=json").status_code, 404)
        events = first.get(f"/api/v3/conversations/{id}/events?format=json").json()
        self.assertGreater(events["cursor"], 0)
        self.assertEqual(first.get(f"/api/v3/conversations/{id}/events?format=json&after={events['cursor']}").json()["items"], [])

    def test_same_origin_csrf_is_required(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post("/api/v3/conversations", {}, content_type="application/json").status_code, 403)


class ErrorMappingTests(TransactionTestCase):
    def setUp(self):
        self.c = Conversation.objects.create(owner_session_key="owner")

    def message(self, content="你好", **extra):
        return submit(self.c.id, "owner", {"content": content, "attachment_ids": [], "request_id": uuid.uuid4().hex, **extra})

    def patched_registry(self, extra_tools):
        real_registry = harness.registry
        def patched():
            tools = real_registry()
            tools.update(extra_tools)
            return tools
        return patched

    def test_schema_validation_error_maps_to_invalid_arguments_with_field_details(self):
        _, run = self.message()
        calls = 0
        def provider(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"tool_calls": [call("read_saved_result", {"tool_call_id": "不是整数"})]}
            text = json.dumps(messages, ensure_ascii=False)
            self.assertIn("invalid_arguments", text)
            self.assertIn("tool_call_id", text)
            self.assertIn("integer", text)
            return {"content": "明白，参数类型不符合契约"}
        self.assertTrue(execute_run(run.id, provider=provider))
        row = run.tool_calls.get(name="read_saved_result")
        self.assertEqual(row.result["error"]["code"], "invalid_arguments")
        self.assertIn("tool_call_id", row.result["error"]["message"])
        self.assertIn("type", row.result["error"]["message"])
        self.assertIn("integer", row.result["error"]["message"])
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")

    def test_key_error_maps_to_missing_field_with_field_name(self):
        def raise_key(key):
            def handler(args, ctx):
                raise KeyError(key)
            return handler
        extra = {
            "boom_tool": Tool("boom_tool", "测试工具", schema({"attempt": {"type": "integer"}}), raise_key("aoi")),
            "boom_unicode": Tool("boom_unicode", "测试工具", schema({}), raise_key("空间范围")),
        }
        _, run = self.message()
        calls = 0
        def provider(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"tool_calls": [call("boom_tool", {"attempt": 1}), call("boom_unicode", {})]}
            text = json.dumps(messages, ensure_ascii=False)
            self.assertIn("missing_field", text)
            self.assertIn("aoi", text)
            self.assertIn("必填字段", text)
            return {"content": "已核对缺失字段"}
        with patch.object(harness, "registry", self.patched_registry(extra)):
            self.assertTrue(execute_run(run.id, provider=provider))
        row = run.tool_calls.get(name="boom_tool")
        self.assertEqual(row.result["error"]["code"], "missing_field")
        self.assertIn("aoi", row.result["error"]["message"])
        unicode_row = run.tool_calls.get(name="boom_unicode")
        self.assertEqual(unicode_row.result["error"]["code"], "missing_field")
        self.assertIn("必填字段", unicode_row.result["error"]["message"])
        self.assertNotIn("空间范围", unicode_row.result["error"]["message"])

    def test_consecutive_failures_escalate_next_step_and_reach_model_context(self):
        def handler(args, ctx):
            raise ValueError("模拟数据源失败")
        extra = {"flaky_tool": Tool("flaky_tool", "测试工具", schema({"attempt": {"type": "integer"}}), handler)}
        _, run = self.message()
        calls = 0
        def provider(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 3:
                return {"tool_calls": [call("flaky_tool", {"attempt": calls})]}
            text = json.dumps(messages, ensure_ascii=False)
            self.assertIn("recent_failures", text)
            self.assertIn("flaky_tool", text)
            self.assertIn("同类错误持续", text)
            return {"content": "更换数据路径后作答"}
        with patch.object(harness, "registry", self.patched_registry(extra)):
            self.assertTrue(execute_run(run.id, provider=provider))
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.usage["tool_errors"], 3)
        failures = harness._context(run)["recent_failures"]["flaky_tool"]
        self.assertEqual(failures["consecutive_failures"], 3)
        self.assertEqual(failures["last_code"], "ValueError")
        self.assertIn("同类错误持续", failures["next_step"])
