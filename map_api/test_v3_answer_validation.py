"""finish_answer numeric_claims 核算：同口径比例、分母非零与可纠正重试。"""
import uuid

from django.test import SimpleTestCase, TransactionTestCase

from .models import Conversation, RunEvidence
from .v3.conversations import submit
from .v3.harness import invocation_context
from .v3.spatial_tools import finish
from .v3.tools import registry


class FinishAnswerSchemaTests(SimpleTestCase):
    def test_finish_answer_schema_accepts_optional_numeric_claims(self):
        parameters = registry()["finish_answer"].openai()["function"]["parameters"]
        self.assertIn("numeric_claims", parameters["properties"])
        self.assertNotIn("numeric_claims", parameters["required"])


class NumericClaimsTests(TransactionTestCase):
    def setUp(self):
        conversation = Conversation.objects.create(owner_session_key="answer-validation-test")
        _, self.run = submit(conversation.id, conversation.owner_session_key,
                             {"content": "占比核算测试", "attachment_ids": [], "request_id": uuid.uuid4().hex})
        self.ctx = invocation_context(self.run, {})

    def _evidence(self, evidence_id, value, scope="aoi"):
        return RunEvidence.objects.create(run=self.run, evidence_id=evidence_id, kind="code_execution",
            metric="area", value={"answer": {"value": value, "unit": "km2", "scope_id": scope}})

    def _operand(self, evidence_id):
        return {"evidence_id": evidence_id, "path": ["answer"]}

    def _finish(self, claims, evidence_ids):
        return finish({"answer": "水体面积占比 75.0%。", "observation_ids": [], "limitations": [],
                       "evidence_ids": evidence_ids, "numeric_claims": claims}, self.ctx)

    def test_valid_claims_pass(self):
        self._evidence("ev-water", 30.0)
        self._evidence("ev-total", 40.0)
        claims = [
            {"label": "水体面积", "operation": "value", "operands": [self._operand("ev-water")],
             "value": 30.0, "decimals": 1},
            {"label": "水体占比", "operation": "percentage",
             "operands": [self._operand("ev-water"), self._operand("ev-total")],
             "value": 75.0, "decimals": 1},
            {"label": "面积差", "operation": "difference",
             "operands": [self._operand("ev-water"), self._operand("ev-total")],
             "value": 10.0, "decimals": 1},
        ]
        result = self._finish(claims, ["ev-water", "ev-total"])
        self.assertIn("final", result)

    def test_mixed_scope_percentage_is_rejected_with_correctable_error(self):
        self._evidence("ev-water", 30.0)
        self._evidence("ev-buffer", 80.0, scope="buffer")
        claims = [{"label": "口径混用占比", "operation": "percentage",
                   "operands": [self._operand("ev-water"), self._operand("ev-buffer")],
                   "value": 37.5, "decimals": 1}]
        result = self._finish(claims, ["ev-water", "ev-buffer"])
        self.assertNotIn("final", result)
        error = result["error"]
        self.assertEqual(error["code"], "answer_validation_failed")
        self.assertTrue(error["retryable"])
        self.assertIn("口径", error["message"])

    def test_zero_denominator_is_rejected(self):
        self._evidence("ev-water", 30.0)
        self._evidence("ev-empty", 0.0)
        claims = [{"label": "零分母占比", "operation": "percentage",
                   "operands": [self._operand("ev-water"), self._operand("ev-empty")],
                   "value": 100.0, "decimals": 1}]
        result = self._finish(claims, ["ev-water", "ev-empty"])
        self.assertEqual(result["error"]["code"], "answer_validation_failed")
        self.assertIn("分母为零", result["error"]["message"])

    def test_model_can_retry_finish_with_corrected_claim(self):
        self._evidence("ev-water", 30.0)
        self._evidence("ev-total", 40.0)
        claims = [{"label": "水体占比", "operation": "percentage",
                   "operands": [self._operand("ev-water"), self._operand("ev-total")],
                   "value": 76.0, "decimals": 1}]
        first = self._finish(claims, ["ev-water", "ev-total"])
        self.assertEqual(first["error"]["code"], "answer_validation_failed")
        self.assertIn("75.000000", first["error"]["message"])
        claims[0]["value"] = 75.0
        second = self._finish(claims, ["ev-water", "ev-total"])
        self.assertIn("final", second)
