"""Agent HITL 等待与恢复协议。

工具在领域门控触发时抛 WaitingForUser；循环捕获后持久化 waiting 状态。
恢复时 translate_action 把用户回送内容翻译成结构化 action code，
apply_resume_action 落 slot 变更并产出指令性 tool result，引导模型走确定的下一步。
"""
from django.utils import timezone


class WaitingForUser(Exception):
    """工具请求循环暂停并向用户征求确认。

    options: list[dict]，每项 {"code": str, "label": str}（前端显示 label、回送 code）。
    step_id: 当前所处固定 step 词汇表 id（retrieve_imagery/quality_check/vl_analysis/...）。
    data:    附加上下文（scene_brief 等）随 waiting 一起持久化，供恢复时重建历史。
    """

    def __init__(self, message, options, *, step_id="quality_check", label="等待用户确认", data=None):
        super().__init__(message)
        self.message = message
        self.options = options or []
        self.step_id = step_id
        self.label = label
        self.data = data or {}


# action code <-> 中文 label（label 保留旧契约，前端显示 + legacy 子串匹配兜底）
ACTION_LABELS = {
    "switch_source": "切换高清底图",
    "expand_dates": "扩大时间范围",
    "retry_fast": "快速模式重试",
    "continue": "继续分析",
    "cancel": "取消任务",
    "generate_report": "生成报告",
    "retry_step": "重试当前步骤",
    "continue_rule_mode": "继续使用规则流程",
}
LABEL_TO_CODE = {v: k for k, v in ACTION_LABELS.items()}


def option(code):
    """构造一个 waiting 选项 {code, label}。"""
    return {"code": code, "label": ACTION_LABELS.get(code, code)}


def translate_action(action_or_content):
    """把用户回送的内容翻译成结构化 action code。

    优先精确匹配 code；其次中文 label 子串匹配（legacy 兼容，前端旧版/手输仍生效）。
    返回 code 字符串；无匹配返回 ""。
    """
    text = (action_or_content or "").strip()
    if not text:
        return ""
    if text in ACTION_LABELS:
        return text
    for code, label in ACTION_LABELS.items():
        if label and label in text:
            return code
    return ""


def apply_resume_action(session, code):
    """把恢复动作落到 session.slots / session.mode，返回指令性 tool result 文本。

    指令文本作为闭合上一轮 waiting 的 tool result，明确告诉模型下一步调哪个工具，
    避免模型自由发散（例如 switch_source 后又去重检 Sentinel-2）。
    调用方在调用后负责 session.save()。
    """
    slots = dict(session.slots or {})
    if code == "switch_source":
        slots["source"] = "mapbox"
        slots["source_switched"] = True
        session.slots = slots
        return "已切换为高清底图（Mapbox）。请直接调用 fetch_mapbox_imagery，不要重新检索 Sentinel-2。"
    if code == "expand_dates":
        slots.pop("date_start", None)
        slots.pop("date_end", None)
        slots["replan_requested"] = True
        session.slots = slots
        return "已扩大时间范围（清除日期约束）。请重新调用 search_sentinel_imagery 检索 Sentinel-2 候选。"
    if code == "retry_fast":
        session.mode = "fast"
        session.slots = slots
        return "已切换为快速模式（qwen3-vl-flash）。请重新调用 analyze_imagery。"
    if code == "continue":
        session.slots = slots
        return "用户请求继续。必须重新验证未通过的质量门禁，只有满足要求后才能进入下一步。"
    if code == "continue_rule_mode":
        slots["decision_mode"] = "rule"
        session.slots = slots
        return "用户明确接受规则流程。后续仅使用已确认规则和工具结果，不把规则决策描述为模型决策。"
    if code == "retry_step":
        slots.pop("decision_mode", None)
        session.slots = slots
        return "用户要求重试当前步骤。请只重试上一次失败的工具，不重复已成功步骤。"
    session.slots = slots
    return "已收到用户回复，请继续推进调查。"


def resume_step_for(code):
    """恢复后 observer 起始 step（仅用于显示，不影响循环逻辑）。"""
    return {
        "switch_source": "select_source",
        "expand_dates": "retrieve_imagery",
        "retry_fast": "vl_analysis",
        "continue": "quality_check",
        "retry_step": "review",
        "continue_rule_mode": "review",
    }.get(code, "retrieve_imagery")
