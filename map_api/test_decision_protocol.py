from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from .agent.decision import validate_legacy_decision
from .agent.loop import _loop_set_observer
from .agent.registry import ToolDefinition
from .models import AgentSession
from .utils.agent_tools import _parse_json_response


class DecisionProtocolTests(SimpleTestCase):
    def test_prose_fences_duplicate_keys_and_nonfinite_values_are_rejected(self):
        for text in ['prefix {"ok":true}', '```json\n{"ok":true}\n```', '{"ok":true,"ok":false}', '{"v":NaN}', '[]']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                _parse_json_response(text)
        self.assertEqual(_parse_json_response(' {"ok": true} '), {"ok": True})

    def test_schema_rejects_multiple_actions_missing_fields_and_wrong_nested_types(self):
        tool = ToolDefinition("demo", "", {"type": "object", "properties": {}}, lambda ctx, args: {})
        valid = {"thought": "读取数据", "current_step": "retrieve_imagery", "plan": [], "tool_call": {"name": "demo", "args": {}}, "final_answer": None}
        self.assertEqual(validate_legacy_decision(valid, {"demo": tool}), valid)
        for change in [{"final_answer": "同时给结论"}, {"tool_call": None}, {"tool_call": {"name": "demo", "args": []}}, {"current_step": "imagined_step"}, {"thought": 4}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_legacy_decision({**valid, **change}, {"demo": tool})
        missing = deepcopy(valid)
        del missing["plan"]
        with self.assertRaises(ValueError):
            validate_legacy_decision(missing, {"demo": tool})

    def test_nested_tool_schema_bounds_required_and_enums(self):
        tool = ToolDefinition("demo", "", {
            "type": "object", "required": ["bands"], "additionalProperties": False,
            "properties": {"bands": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["kind", "scale"], "additionalProperties": False,
                "properties": {"kind": {"enum": ["green", "nir"]}, "scale": {"type": "number", "exclusiveMinimum": 0}},
            }}},
        }, lambda ctx, args: {})
        tool.validate_args({"bands": [{"kind": "green", "scale": 0.0001}]})
        for value in [{}, {"bands": []}, {"bands": None}, {"bands": [{"kind": "rgb", "scale": 1}]}, {"bands": [{"kind": "nir", "scale": 0}]}, {"bands": [{"kind": "nir", "scale": float("nan")}] }]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tool.validate_args(value)


class ObserverFactTests(TestCase):
    def test_later_step_does_not_imply_earlier_steps_completed(self):
        session = AgentSession.objects.create(goal="调查", status="running")
        _loop_set_observer(session, "review", "准备复核", [
            {"id": "locate", "label": "定位"}, {"id": "ndwi", "label": "计算"}, {"id": "review", "label": "复核"},
        ], completed_steps={"locate"})
        session.refresh_from_db()
        states = {step["id"]: step["status"] for step in session.artifacts["observer"]["plan_steps"]}
        self.assertEqual(states, {"locate": "done", "ndwi": "pending", "review": "running"})
