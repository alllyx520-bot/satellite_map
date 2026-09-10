import json
from unittest.mock import Mock

import requests
from django.test import SimpleTestCase

from .agent.decision import validate_decision
from .agent.providers import GLMProvider, OpenAICompatibleProvider, ProviderError, QwenProvider
from .agent.registry import ToolDefinition


class ProviderTests(SimpleTestCase):
    final = {"type": "final", "decision_id": "review-1", "content": "复核结论",
             "evidence_refs": ["metric-1"], "limitations": ["分辨率限制"]}

    def provider(self, transport, **kwargs):
        self.events = []
        return OpenAICompatibleProvider(model="configured-main", endpoint="https://example.test/chat/completions",
                                         headers=lambda: {}, transport=transport, telemetry=self.events.append, **kwargs)

    def response(self, output=None, *, status=200):
        response = Mock(status_code=status)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(output or self.final)}}],
                                      "usage": {"prompt_tokens": 45, "completion_tokens": 20}}
        if status != 200:
            response.raise_for_status.side_effect = requests.HTTPError(response=response)
        return response

    def test_schema_usage_and_same_provider_retry_after_429(self):
        transport = Mock(side_effect=[self.response(status=429), self.response()])
        provider = self.provider(transport)
        self.assertEqual(provider.review({"evidence_refs": ["metric-1"]}), self.final)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[0]["error"]["code"], "rate_limit")
        self.assertEqual(self.events[1]["usage"]["prompt_tokens"], 45)
        self.assertTrue(all(call.kwargs["json"]["model"] == "configured-main" for call in transport.call_args_list))

    def test_401_is_not_retried_and_failed_models_never_return_a_final(self):
        for failure in [self.response(status=401), self.response(status=503), requests.Timeout()]:
            transport = Mock(side_effect=failure if isinstance(failure, Exception) else None, return_value=failure)
            provider = self.provider(transport)
            with self.assertRaises(ProviderError):
                provider.review({})
            self.assertEqual(transport.call_count, 1 if getattr(failure, "status_code", None) == 401 else 2)
            self.assertTrue(self.events[-1]["error"])

    def test_invalid_json_cannot_be_repaired_from_prose(self):
        response = self.response()
        response.json.return_value["choices"][0]["message"]["content"] = 'prefix {"type":"final"}'
        with self.assertRaises(ProviderError) as caught:
            self.provider(Mock(return_value=response), max_retries=0).review({})
        self.assertEqual(caught.exception.code, "invalid_output")

    def test_six_decision_actions_and_single_tool_boundary(self):
        tool = ToolDefinition("read", "", {"type": "object", "required": ["asset"], "properties": {"asset": {"type": "string"}}}, lambda ctx, args: {})
        variants = [self.final,
                    {"type": "tool_call", "tool_call": {"name": "read", "arguments": {"asset": "green"}}},
                    {"type": "request_user", "user_request": {"question": "修改日期", "options": ["重试"]}},
                    {"type": "plan_update", "plan_update": {}}, {"type": "retry", "retry": {"reason": "HTTP 429"}},
                    {"type": "error", "error": {"code": "invalid_input", "retryable": False}}]
        for value in variants:
            decision = {"decision_id": "one", "content": "操作摘要", **value}
            self.assertEqual(validate_decision(decision, {"read": tool}), decision)
        with self.assertRaises(ValueError):
            validate_decision({**self.final, "tool_call": {"name": "read", "arguments": {"asset": "green"}}}, {"read": tool})

    def test_stream_yields_only_after_complete_validated_decision(self):
        response = self.response()
        encoded = json.dumps(self.final)
        response.iter_lines.return_value = ["data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) for part in [encoded[:40], encoded[40:]]]
        response.iter_lines.return_value += ["data: " + json.dumps({"choices": [], "usage": {"total_tokens": 20}}), "data: [DONE]"]
        provider = self.provider(Mock(return_value=response))
        self.assertEqual(list(provider.stream_decision({}, {})), [self.final])
        self.assertEqual(self.events[0]["usage"], {"total_tokens": 20})
        response.close.assert_called_once()
        response.iter_lines.return_value = response.iter_lines.return_value[:-1]
        with self.assertRaisesRegex(ProviderError, "中断"):
            list(provider.stream_decision({}, {}))

    def test_context_and_image_limits_fail_before_request(self):
        transport = Mock()
        provider = self.provider(transport, max_context_chars=100)
        with self.assertRaises(ProviderError):
            provider.review({"large": "x" * 200})
        with self.assertRaises(ProviderError):
            provider.review({}, images=["x"] * 4)
        transport.assert_not_called()

    def test_all_adapters_implement_same_contract_without_switching(self):
        for cls in [GLMProvider, QwenProvider, OpenAICompatibleProvider]:
            response = self.response()
            transport = Mock(return_value=response)
            provider = cls(model="selected", endpoint="https://example.test", headers=lambda: {}, transport=transport)
            self.assertEqual(provider.review({}), self.final)
            self.assertEqual(transport.call_args.kwargs["json"]["model"], "selected")
