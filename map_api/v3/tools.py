"""Tool contracts enforce arguments, evidence and bounded execution."""
from dataclasses import dataclass
from . import assets
from jsonschema import Draft202012Validator

GROUPS = {
    "imagery": {"description": "检索原始影像、读取光学/SAR/DEM/热产品与变化计算",
                "tools": ["search_scenes", "retrieve_imagery", "compute_product", "compare_two_date_change"]},
    "context": {"description": "历史水体、地类、道路建筑、火点、气象等外部证据", "tools": ["external_evidence"]},
    "analysis": {"description": "独立视觉复核、Python 分析及结果影像接入", "tools": ["review_visual", "python_analysis", "import_python_output"]},
}


def unavailable_tools(names):
    """Return runtime-disabled optional tools without probing unrelated tools."""
    unavailable = {}
    if set(names) & {"python_analysis", "import_python_output"}:
        from .sandbox import sandbox_status
        state = sandbox_status()
        if not state.get("available"):
            detail = state.get("detail") or "隔离分析容器不可用"
            for name in ("python_analysis", "import_python_output"):
                if name in names:
                    unavailable[name] = detail
    return unavailable


def load_capabilities(args, context):
    names = list(dict.fromkeys(name for group in args["groups"] for name in GROUPS[group]["tools"]))
    definitions = registry()
    unavailable = unavailable_tools(names)
    enabled = [name for name in names if name not in unavailable]
    return {"enabled_tools": enabled, "tools": [definitions[name].public() for name in enabled],
            "unavailable_tools": unavailable}


def model_tools(definitions, context):
    deferred = {name for group in GROUPS.values() for name in group["tools"]}
    enabled = set(context.get("enabled_tools", []))
    unavailable = unavailable_tools(enabled)
    return [tool.openai() for name, tool in definitions.items()
            if (name not in deferred or name in enabled) and name not in unavailable]

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: object
    timeout: int = 120
    recovery: str = "replay_safe"
    parallel: bool = False
    def duration(self, args):
        if self.name == "python_analysis":
            seconds = args.get("timeout_seconds", 300) if isinstance(args, dict) else 300
            if isinstance(seconds, int) and not isinstance(seconds, bool):
                return min(600, max(30, seconds)) + 60
        return self.timeout
    def public(self): return {"name": self.name, "description": self.description, "input_schema": self.schema, "timeout_seconds": self.timeout, "timeout_boundary": "chunk_and_publication", "recovery": self.recovery, "parallel": self.parallel, "retry": {"transient_attempts": 2}}
    def openai(self): return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.schema}}
    def execute(self, args, ctx):
        Draft202012Validator(self.schema).validate(args)
        return self.handler(args, ctx)

def _window(args, context):
    from .spatial_tools import read_window
    return read_window(args, context)

def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

def registry():
    from .spatial_tools import specifications
    tools = specifications(Tool, schema)
    from .places import tool as place_lookup
    from .methods import consult, coverage, METHODS
    from .spatial_index import search as spatial_search
    from .context_store import read_saved_result
    tools.extend([
        Tool("read_saved_result", "按真实工具调用 ID 或证据 ID 读取当前会话完整结果；可用 path 定位字段，长内容按字符分页。",
             schema({"tool_call_id": {"type": "integer", "minimum": 1}, "evidence_id": {"type": "string"},
                     "path": {"type": "array", "maxItems": 16, "items": {"type": ["string", "integer"]}},
                     "character_offset": {"type": "integer", "minimum": 0},
                     "max_characters": {"type": "integer", "minimum": 200, "maximum": 16000}}), read_saved_result),
        Tool("find_observations", "按影像、像素窗口和名称查找已有空间观察，找回早期目标与局部证据。",
            schema({"attachment_id": {"type": "string"}, "window": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "label": {"type": "string", "maxLength": 200}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["attachment_id"]), spatial_search),
        Tool("load_capabilities", "按问题加载专用工具。imagery=影像检索/指数/变化，context=外部空间证据，analysis=专门视觉/Python。",
            schema({"groups": {"type": "array", "items": {"enum": list(GROUPS)}, "minItems": 1, "maxItems": 3}}, ["groups"]), load_capabilities),
        Tool("consult_method", "按需查找遥感问答方法与质量门禁；topic 省略时列出方法目录。",
            schema({"topic": {"enum": list(METHODS)}}), consult),
        Tool("observation_coverage", "统计某影像在指定尺度上已观察的真实窗口覆盖，去除重叠；不冒充计数召回率。",
            schema({"attachment_id": {"type": "string"}, "max_pixel_scale": {"type": "number", "minimum": 1, "maximum": 128}}, ["attachment_id"]), coverage),
    ])
    tools.append(Tool("find_place", "按地名或经度,纬度查找真实位置；返回 WGS84 坐标和匹配来源，不能当作精确行政边界。",
        schema({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"]), place_lookup,
        timeout=20, recovery="uncertain_external", parallel=True))
    try:
        from .data_adapter import registered_tools
    except ImportError as error:
        if error.name != "map_api.v3.data_adapter":
            raise
    else:
        tools.extend(registered_tools(Tool, schema))
    return {tool.name: tool for tool in tools}
