"""Bound model context while retaining addressable, immutable tool records."""
import json

from ..models import RunEvidence, RunToolCall


def compact(value, *, characters=4000):
    """A display excerpt, never a replacement for the persisted source value."""
    def shorten(item, depth=0):
        if isinstance(item, str):
            if len(item) <= 900:
                return item
            return item[:600] + "\n[…完整内容已保存…]\n" + item[-200:]
        if isinstance(item, list):
            if depth >= 6:
                return {"saved_items": len(item)}
            result = [shorten(child, depth + 1) for child in item[:16]]
            if len(item) > 16:
                result.append({"more_saved_items": len(item) - 16})
            return result
        if isinstance(item, dict):
            if depth >= 7:
                return {"saved_fields": list(item)[:30]}
            result = {key: shorten(child, depth + 1) for key, child in list(item.items())[:50]}
            if len(item) > 50:
                result["more_saved_fields"] = len(item) - 50
            return result
        return item

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded) <= characters:
        return value
    summary = shorten(value)
    if len(json.dumps(summary, ensure_ascii=False)) <= characters:
        return {"summary": summary, "truncated": True, "total_characters": len(encoded)}
    return {"excerpt": encoded[:max(0, characters - 180)], "truncated": True,
            "total_characters": len(encoded), "note": "此处仅为节选；使用 read_saved_result 查看完整记录或指定字段。"}


def _describe_level(value):
    if isinstance(value, dict):
        keys = list(value)
        shown = ", ".join(repr(key) for key in keys[:20]) or "（无键）"
        overflow = f"；共 {len(keys)} 个键，仅列出前 20 个" if len(keys) > 20 else ""
        return f"dict，可用键: {shown}{overflow}"
    if isinstance(value, list):
        if not value:
            return "list，长度 0，无可用索引"
        return f"list，长度 {len(value)}，合法索引为 0–{len(value) - 1} 的整数"
    return f"{type(value).__name__} 类型标量，不能再用键或索引访问"


def field_at(value, path):
    for key in path:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(value):
            value = value[key]
        else:
            raise ValueError(
                f"保存结果中不存在指定 path {path!r}；在 {key!r} 处定位失败，"
                f"当前层为{_describe_level(value)}；请改用上述键名或索引")
    return value


def read_saved_result(args, ctx):
    """Retrieve paginated facts in this conversation, excluding model reasoning."""
    if bool(args.get("tool_call_id")) == bool(args.get("evidence_id")):
        raise ValueError("请指定 tool_call_id 或 evidence_id 中的一项")
    if args.get("tool_call_id"):
        row = RunToolCall.objects.filter(run__conversation=ctx["run"].conversation,
                                         pk=args["tool_call_id"]).first()
        if row is None:
            raise ValueError("工具记录不存在或不属于当前会话")
        value = row.result
        source = {"tool_call_id": row.pk, "name": row.name}
    else:
        row = RunEvidence.objects.filter(run__conversation=ctx["run"].conversation,
                                          evidence_id=args["evidence_id"]).first()
        if row is None:
            raise ValueError("数据证据不存在或不属于当前会话")
        value = row.value
        source = {"evidence_id": row.evidence_id, "metric": row.metric}
    path = args.get("path", [])
    value = field_at(value, path)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    offset, size = args.get("character_offset", 0), args.get("max_characters", 8000)
    end = min(len(encoded), offset + size)
    result = {**source, "path": path, "total_characters": len(encoded)}
    if offset == 0 and end == len(encoded):
        result["value"] = value
    else:
        result.update({"excerpt": encoded[offset:end], "character_offset": offset,
                       "next_character_offset": end if end < len(encoded) else None})
    return result


def tool_result_for_prompt(row, *, characters=6000):
    result = compact(row.result, characters=characters)
    return {**result, "stored_tool_call_id": row.pk} if isinstance(result, dict) else {
        "value": result, "stored_tool_call_id": row.pk}
