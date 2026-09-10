"""通用 Agent tool harness primitives.

领域工具仍可使用旧的 ``REGISTRY`` 字典；本模块提供统一的输入/输出边界、
副作用分类和调用上下文，供循环、测试和未来插件工具复用。
"""
from dataclasses import dataclass
import json
from jsonschema import Draft202012Validator, ValidationError


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    fn: object
    side_effect: str = "read"
    max_output_chars: int = 12000

    def validate_args(self, args):
        try:
            json.dumps(args, allow_nan=False)
            Draft202012Validator(self.parameters).validate(args)
        except (ValidationError, TypeError, ValueError) as exc:
            path = ".".join(str(item) for item in getattr(exc, "absolute_path", [])) or "$"
            raise ValueError(f"工具 {self.name} 参数 {path} 不满足 JSON Schema") from exc
        return args

    def invoke(self, ctx, args):
        result = self.fn(ctx, self.validate_args(args or {}))
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded) > self.max_output_chars:
            return {"status": "error", "message": f"工具 {self.name} 输出过大，已拒绝写入上下文"}
        return result


def definitions_from_registry(registry):
    return {
        name: ToolDefinition(
            name=name,
            description=spec.get("description", ""),
            parameters=spec.get("parameters") or {"type": "object"},
            fn=spec["fn"],
            side_effect=spec.get("side_effect", "read"),
            max_output_chars=int(spec.get("max_output_chars", 12000)),
        )
        for name, spec in registry.items()
    }
