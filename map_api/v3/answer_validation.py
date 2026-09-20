"""Check numeric claims against persisted measurements, units and scopes."""
import math

from ..models import RunEvidence
from .context_store import field_at

NUMERIC_CLAIMS_SCHEMA = {
    "type": "array", "maxItems": 30,
    "description": "定量派生结论的核算。operands.path 指向证据 value 内实际保存的 {value,unit,scope_id} 记录。比例/差值只允许同单位同范围。",
    "items": {"type": "object", "additionalProperties": False,
        "required": ["label", "operation", "operands", "value", "decimals"],
        "properties": {
            "label": {"type": "string", "maxLength": 200},
            "operation": {"enum": ["value", "percentage", "difference"]},
            "operands": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                "type": "object", "additionalProperties": False, "required": ["evidence_id", "path"],
                "properties": {"evidence_id": {"type": "string"}, "path": {"type": "array", "maxItems": 12,
                    "items": {"type": ["string", "integer"]}}}}},
            "value": {"type": "number"}, "decimals": {"type": "integer", "minimum": 0, "maximum": 6}}}}


def validate_answer(args, ctx, observations):
    citations = set(args.get("evidence_ids", []))
    for claim in args.get("numeric_claims", []):
        values, units, scopes = [], [], []
        for operand in claim["operands"]:
            row = RunEvidence.objects.filter(run__conversation=ctx["run"].conversation,
                evidence_id=operand["evidence_id"]).first()
            if row is None or row.evidence_id not in citations:
                raise ValueError("数值核算使用的证据必须真实存在且列入 evidence_ids")
            item = field_at(row.value, operand["path"])
            if isinstance(item, dict):
                value, unit, scope = item.get("value"), item.get("unit"), item.get("scope_id")
            else:
                value, unit, scope = item, None, None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("数值核算 path 必须指向实际保存的有限数值或测量记录")
            values.append(value); units.append(unit); scopes.append(scope)
        op = claim["operation"]
        if len(values) != (1 if op == "value" else 2):
            raise ValueError("value 需要一个操作数，percentage/difference 需要两个操作数")
        if op != "value" and (not all(units) or units[0] != units[1] or not all(scopes) or scopes[0] != scopes[1]):
            raise ValueError("统计口径不一致或缺失：比例/差值必须引用同 unit、同 scope_id 的测量记录。请按同一范围重新计算并保存，不能混用整窗、缓冲走廊和未缓冲水体")
        if op == "percentage" and values[1] == 0:
            raise ValueError("比例分母为零，不能输出有效百分比")
        expected = values[0] if op == "value" else (values[0] / values[1] * 100 if op == "percentage" else values[1] - values[0])
        tolerance = .5 * 10 ** -claim["decimals"] + 1e-9
        if not math.isfinite(claim["value"]) or abs(expected - claim["value"]) > tolerance:
            raise ValueError(f"数值核算不符：{claim['label']} 按已保存证据应为 {expected:.6f}；请纠正正文与 numeric_claims 后重新交付")
