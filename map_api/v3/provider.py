"""OpenAI-compatible native tool-calling client for controller and vision roles."""
import os
import time
import requests


class ProviderFailure(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _headers(role):
    key = os.environ.get("DEEPSEEK_API_KEY" if role == "controller" else "DASHSCOPE_API_KEY")
    if not key:
        raise ProviderFailure("credentials_missing", "模型凭据未配置")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _endpoint(role):
    if role == "controller":
        return os.environ.get("DEEPSEEK_CHAT_URL", "https://api.deepseek.com/chat/completions")
    return os.environ.get("QWEN_CHAT_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")


def _model(role):
    return "deepseek-flash" if role == "controller" else os.environ.get("V3_VISION_MODEL", "qwen3-vl-plus")


def tool_call(messages, tools, role="controller", images=None, timeout=90, model=None):
    wire = list(messages)
    if images:
        content = [{"type": "text", "text": wire[-1]["content"]}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": image, "detail": "high"}})
        wire[-1] = {**wire[-1], "content": content}
    chosen = model or _model(role)
    payload = {"model": chosen, "messages": wire, "max_tokens": 8192, "stream": False}
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})
    started = time.monotonic()
    try:
        from ..utils.agent_tools import request_proxies
        response = requests.post(_endpoint(role), headers=_headers(role), json=payload, timeout=(15, timeout), proxies=request_proxies())
        response.raise_for_status()
        result = response.json()
        if result["choices"][0].get("finish_reason") == "length":
            raise ProviderFailure("output_truncated", "模型输出未完成，请缩小本轮输出范围", True)
        item = result["choices"][0]["message"]
        return {"content": item.get("content") or "", "tool_calls": item.get("tool_calls") or [],
                # Private continuation state; never returned by public run APIs.
                "reasoning_content": item.get("reasoning_content") or "",
                "usage": response.json().get("usage") or {}, "latency_ms": round((time.monotonic() - started) * 1000),
                "model": chosen}
    except requests.Timeout as exc:
        raise ProviderFailure("timeout", "模型请求超时", True) from exc
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise ProviderFailure("http_error" if status else "network", f"模型服务返回 HTTP {status}" if status else "模型服务不可用", not status or status == 429 or status >= 500) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderFailure("invalid_response", "模型返回格式无效", True) from exc


def model_capabilities():
    return {"controller": {"configured": bool(os.environ.get("DEEPSEEK_API_KEY")), "model": _model("controller"), "vision": True, "tool_calling": True},
            "vision": {"configured": bool(os.environ.get("DASHSCOPE_API_KEY")), "baseline": "qwen3-vl-plus",
                       "candidate": "qwen3.8-max-0902", "promotion": "requires_blind_evaluation"}}
