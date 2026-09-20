"""Tests for context_store compaction and paginated saved-result reads."""
import json

from django.test import TestCase
from django.utils import timezone

from .models import AgentRun, Conversation, RunEvidence, RunToolCall
from .v3.context_store import compact, read_saved_result


class CompactTests(TestCase):
    def test_short_value_returned_unchanged(self):
        value = {"a": [1, 2, "短文本"], "b": {"c": True}}
        self.assertEqual(compact(value), value)

    def test_long_string_shortened_with_total_characters(self):
        value = {"text": "水" * 5000}
        result = compact(value)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_characters"], len(json.dumps(value, ensure_ascii=False)))
        summary = result["summary"]["text"]
        self.assertIn("[…完整内容已保存…]", summary)
        self.assertTrue(summary.startswith("水" * 600))
        self.assertTrue(summary.endswith("水" * 200))
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), len(json.dumps(value, ensure_ascii=False)))

    def test_nested_lists_and_dicts_are_bounded(self):
        value = {"items": list(range(100)), "fields": {f"k{i}": i for i in range(80)}}
        result = compact(value, characters=800)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_characters"], len(json.dumps(value, ensure_ascii=False)))
        summary = result["summary"]
        self.assertEqual(summary["items"][:16], list(range(16)))
        self.assertEqual(summary["items"][-1], {"more_saved_items": 84})
        self.assertEqual(summary["fields"]["more_saved_fields"], 30)

    def test_deep_nesting_collapses_to_saved_counts(self):
        value = [list(range(40))]
        for _ in range(5):
            value = [value]
        result = compact(value, characters=100)
        self.assertTrue(result["truncated"])
        self.assertIn('"saved_items": 40', json.dumps(result, ensure_ascii=False))
        deep_dict = {"a": "x" * 200}
        for _ in range(8):
            deep_dict = {"w": deep_dict}
        result = compact(deep_dict, characters=200)
        self.assertTrue(result["truncated"])
        self.assertIn("saved_fields", json.dumps(result, ensure_ascii=False))

    def test_oversized_summary_falls_back_to_excerpt(self):
        value = {"text": "水" * 5000}
        encoded = json.dumps(value, ensure_ascii=False)
        result = compact(value, characters=300)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_characters"], len(encoded))
        self.assertEqual(result["excerpt"], encoded[:120])
        self.assertIn("read_saved_result", result["note"])


class ReadSavedResultTests(TestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(owner_session_key="owner")
        self.run = AgentRun.objects.create(goal="调查水体", conversation=self.conversation, run_key="rk-context-store")
        self.call_row = RunToolCall.objects.create(
            run=self.run, call_key="ck-1", name="read_image_window", step_id="turn:1",
            claim="claim", lease_until=timezone.now(), status="completed",
            result={"stats": {"mean": 0.5, "values": [1, 2, 3]}, "note": "完成"})
        self.evidence = RunEvidence.objects.create(
            run=self.run, evidence_id="ev-1", kind="metric", metric="water_area",
            value={"area_km2": 12.5, "samples": ["a", "b"]})
        self.ctx = {"run": self.run}

    def test_read_by_tool_call_id(self):
        result = read_saved_result({"tool_call_id": self.call_row.pk}, self.ctx)
        self.assertEqual(result["tool_call_id"], self.call_row.pk)
        self.assertEqual(result["name"], "read_image_window")
        self.assertEqual(result["path"], [])
        self.assertEqual(result["value"], self.call_row.result)
        self.assertEqual(result["total_characters"], len(json.dumps(self.call_row.result, ensure_ascii=False)))

    def test_read_by_evidence_id(self):
        result = read_saved_result({"evidence_id": "ev-1"}, self.ctx)
        self.assertEqual(result["evidence_id"], "ev-1")
        self.assertEqual(result["metric"], "water_area")
        self.assertEqual(result["value"], self.evidence.value)

    def test_path_navigates_dict_keys_and_list_indexes(self):
        result = read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "mean"]}, self.ctx)
        self.assertEqual(result["value"], 0.5)
        self.assertEqual(result["path"], ["stats", "mean"])
        result = read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "values", 1]}, self.ctx)
        self.assertEqual(result["value"], 2)
        result = read_saved_result({"evidence_id": "ev-1", "path": ["samples", 0]}, self.ctx)
        self.assertEqual(result["value"], "a")

    def test_pagination_slices_encoded_value(self):
        row = RunToolCall.objects.create(
            run=self.run, call_key="ck-2", name="python_analysis", step_id="turn:2",
            claim="claim", lease_until=timezone.now(), status="completed",
            result={"blob": "x" * 1000})
        encoded = json.dumps(row.result, ensure_ascii=False)
        first = read_saved_result({"tool_call_id": row.pk, "max_characters": 300}, self.ctx)
        self.assertNotIn("value", first)
        self.assertEqual(first["excerpt"], encoded[:300])
        self.assertEqual(first["character_offset"], 0)
        self.assertEqual(first["next_character_offset"], 300)
        self.assertEqual(first["total_characters"], len(encoded))
        second = read_saved_result({"tool_call_id": row.pk, "character_offset": 300, "max_characters": 300}, self.ctx)
        self.assertEqual(second["excerpt"], encoded[300:600])
        self.assertEqual(second["next_character_offset"], 600)
        last = read_saved_result({"tool_call_id": row.pk, "character_offset": len(encoded) - 100, "max_characters": 300}, self.ctx)
        self.assertEqual(last["excerpt"], encoded[-100:])
        self.assertIsNone(last["next_character_offset"])

    def test_pagination_applies_after_path(self):
        row = RunToolCall.objects.create(
            run=self.run, call_key="ck-3", name="python_analysis", step_id="turn:3",
            claim="claim", lease_until=timezone.now(), status="completed",
            result={"nested": {"blob": "y" * 500}})
        encoded = json.dumps("y" * 500, ensure_ascii=False)
        result = read_saved_result({"tool_call_id": row.pk, "path": ["nested", "blob"], "max_characters": 200}, self.ctx)
        self.assertEqual(result["path"], ["nested", "blob"])
        self.assertEqual(result["excerpt"], encoded[:200])
        self.assertEqual(result["total_characters"], len(encoded))

    def test_exactly_one_id_is_required(self):
        with self.assertRaisesMessage(ValueError, "请指定 tool_call_id 或 evidence_id 中的一项"):
            read_saved_result({}, self.ctx)
        with self.assertRaisesMessage(ValueError, "请指定 tool_call_id 或 evidence_id 中的一项"):
            read_saved_result({"tool_call_id": self.call_row.pk, "evidence_id": "ev-1"}, self.ctx)

    def test_unknown_or_foreign_ids_are_rejected(self):
        with self.assertRaisesMessage(ValueError, "工具记录不存在或不属于当前会话"):
            read_saved_result({"tool_call_id": self.call_row.pk + 9999}, self.ctx)
        with self.assertRaisesMessage(ValueError, "数据证据不存在或不属于当前会话"):
            read_saved_result({"evidence_id": "ev-missing"}, self.ctx)
        other_conversation = Conversation.objects.create(owner_session_key="other")
        other_run = AgentRun.objects.create(goal="别的会话", conversation=other_conversation, run_key="rk-other")
        other_row = RunToolCall.objects.create(
            run=other_run, call_key="ck-x", name="read_image_window", step_id="turn:1",
            claim="claim", lease_until=timezone.now(), status="completed", result={})
        with self.assertRaisesMessage(ValueError, "工具记录不存在或不属于当前会话"):
            read_saved_result({"tool_call_id": other_row.pk}, self.ctx)

    def test_invalid_path_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "保存结果中不存在指定 path"):
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "missing"]}, self.ctx)
        with self.assertRaisesMessage(ValueError, "保存结果中不存在指定 path"):
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "values", 99]}, self.ctx)
        with self.assertRaisesMessage(ValueError, "保存结果中不存在指定 path"):
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "values", "0"]}, self.ctx)

    def test_unknown_dict_key_message_lists_available_keys(self):
        with self.assertRaises(ValueError) as caught:
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "missing"]}, self.ctx)
        message = str(caught.exception)
        self.assertIn("保存结果中不存在指定 path", message)
        self.assertIn("'mean'", message)
        self.assertIn("'values'", message)

    def test_dict_key_overflow_is_truncated_with_total_count(self):
        row = RunToolCall.objects.create(
            run=self.run, call_key="ck-many", name="python_analysis", step_id="turn:4",
            claim="claim", lease_until=timezone.now(), status="completed",
            result={f"k{i}": i for i in range(25)})
        with self.assertRaises(ValueError) as caught:
            read_saved_result({"tool_call_id": row.pk, "path": ["missing"]}, self.ctx)
        message = str(caught.exception)
        self.assertIn("共 25 个键，仅列出前 20 个", message)
        self.assertIn("'k19'", message)
        self.assertNotIn("'k20'", message)

    def test_out_of_range_index_message_gives_length(self):
        with self.assertRaises(ValueError) as caught:
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "values", 99]}, self.ctx)
        message = str(caught.exception)
        self.assertIn("保存结果中不存在指定 path", message)
        self.assertIn("长度 3", message)
        self.assertIn("0–2", message)

    def test_string_index_on_list_message_explains_type(self):
        with self.assertRaises(ValueError) as caught:
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "values", "0"]}, self.ctx)
        message = str(caught.exception)
        self.assertIn("list", message)
        self.assertIn("整数", message)

    def test_scalar_level_message_explains_type(self):
        with self.assertRaises(ValueError) as caught:
            read_saved_result({"tool_call_id": self.call_row.pk, "path": ["stats", "mean", "x"]}, self.ctx)
        message = str(caught.exception)
        self.assertIn("float", message)
        self.assertIn("标量", message)
