"""Strict decision boundary. Provider prose is never used to repair business fields."""
from jsonschema import Draft202012Validator, ValidationError


DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["type", "decision_id", "content"],
    "properties": {
        "type": {"enum": ["tool_call", "final", "request_user", "plan_update", "retry", "error"]},
        "decision_id": {"type": "string", "minLength": 1, "maxLength": 160},
        "content": {"type": "string", "maxLength": 16000},
        "tool_call": {"type": "object", "required": ["name", "arguments"], "additionalProperties": False,
                      "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}},
        "evidence_refs": {"type": "array", "uniqueItems": True, "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "limitations": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "\\S"}},
        "plan_update": {"type": "object"},
        "user_request": {"type": "object", "required": ["question", "options"],
                         "properties": {"question": {"type": "string", "pattern": "\\S"}, "options": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False},
        "retry": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string"}}, "additionalProperties": False},
        "error": {"type": "object", "required": ["code", "retryable"],
                  "properties": {"code": {"type": "string"}, "retryable": {"type": "boolean"}}, "additionalProperties": False},
    },
}


def validate_decision(value, definitions=None):
    import json
    try:
        json.dumps(value, allow_nan=False)
        Draft202012Validator(DECISION_SCHEMA).validate(value)
        required = {"tool_call": {"tool_call"}, "final": {"evidence_refs", "limitations"},
                    "request_user": {"user_request"}, "plan_update": {"plan_update"}, "retry": {"retry"}, "error": {"error"}}[value["type"]]
        action_fields = set(value) - {"type", "decision_id", "content"}
        if action_fields != required or not value["content"].strip():
            raise ValueError("each decision must contain exactly one action")
    except (ValidationError, TypeError, ValueError) as exc:
        raise ValueError("模型决策不满足内部协议") from exc
    if value["type"] == "tool_call":
        call = value["tool_call"]
        definition = (definitions or {}).get(call["name"])
        if definition is None:
            raise ValueError("模型选择了未注册的工具")
        definition.validate_args(call["arguments"])
    return value


LEGACY_DECISION_SCHEMA = {
    "type": "object",
    "required": ["thought", "current_step", "plan", "tool_call", "final_answer"],
    "properties": {
        "thought": {"type": "string", "maxLength": 500},
        "current_step": {"enum": ["understand", "locate", "select_source", "retrieve_imagery", "quality_check", "ndwi", "compute_metric", "vl_analysis", "review", "complete"]},
        "plan": {"type": "array", "maxItems": 30, "items": {
            "type": "object", "required": ["id", "label"],
            "properties": {"id": {"type": "string", "minLength": 1}, "label": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        }},
        "tool_call": {"oneOf": [{"type": "null"}, {
            "type": "object", "required": ["name", "args"],
            "properties": {"name": {"type": "string", "minLength": 1}, "args": {"type": "object"}},
            "additionalProperties": False,
        }]},
        "final_answer": {"type": ["string", "null"], "minLength": 1},
        "visual_observation": {"type": ["string", "null"]},
        "decision_confidence": {"enum": ["high", "medium", "low", None]},
        "evidence_basis": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": ["string", "null"]},
        "evidence_refs": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
        "limitations": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
    "additionalProperties": False,
    "oneOf": [
        {"properties": {"tool_call": {"type": "object"}, "final_answer": {"type": "null"}}},
        {"required": ["evidence_refs", "limitations"], "properties": {"tool_call": {"type": "null"}, "final_answer": {"type": "string", "pattern": "\\S"}, "evidence_refs": {"minItems": 1}, "limitations": {"minItems": 1}}},
    ],
}


def validate_legacy_decision(value, definitions):
    if not value:
        raise ValueError("GLM 返回空决策")
    try:
        Draft202012Validator(LEGACY_DECISION_SCHEMA).validate(value)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"模型决策 {path} 不满足协议，请重试") from exc
    tool_call = value["tool_call"]
    if tool_call:
        definition = definitions.get(tool_call["name"])
        if definition is None:
            raise ValueError("模型选择了未注册的工具")
        definition.validate_args(tool_call["args"])
    return value
