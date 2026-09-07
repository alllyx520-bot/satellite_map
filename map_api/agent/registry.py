"""通用 Agent tool harness primitives.

领域工具仍可使用旧的 ``REGISTRY`` 字典；本模块提供统一的输入/输出边界、
副作用分类和调用上下文，供循环、测试和未来插件工具复用。
"""
from dataclasses import dataclass
import json


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    fn: object
    side_effect: str = "read"
    max_output_chars: int = 12000

    def validate_args(self, args):
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        required = self.parameters.get("required") or []
        missing = [key for key in required if args.get(key) in (None, "")]
        if missing:
            raise ValueError(f"工具 {self.name} 缺少参数：{', '.join(missing)}")
        properties = self.parameters.get("properties") or {}
        unknown = sorted(set(args) - set(properties))
        if unknown and self.parameters.get("additionalProperties") is False:
            raise ValueError(f"工具 {self.name} 包含未知参数：{', '.join(unknown)}")
        type_checks = {
            "string": lambda value: isinstance(value, str),
            "object": lambda value: isinstance(value, dict),
            "array": lambda value: isinstance(value, list),
            "boolean": lambda value: isinstance(value, bool),
            "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        }
        for key, schema in properties.items():
            if key not in args or args[key] is None:
                continue
            expected = schema.get("type") if isinstance(schema, dict) else None
            check = type_checks.get(expected)
            if check and not check(args[key]):
                raise ValueError(f"工具 {self.name} 参数 {key} 类型错误，应为 {expected}")
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
