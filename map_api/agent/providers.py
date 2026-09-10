"""Configured main provider only; structured parsing, budgets and real telemetry."""
import json
import os
import time
from dataclasses import dataclass

import requests
from jsonschema import Draft202012Validator, ValidationError

from .decision import DECISION_SCHEMA, validate_decision
from ..utils.agent_tools import AGENT_MODEL, GLM_CHAT_URL, _parse_json_response, request_proxies


@dataclass
class ProviderError(Exception):
    code: str
    message: str
    retryable: bool = False
    http_status: int | None = None

    def __str__(self):
        return self.message

    def as_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "http_status": self.http_status}


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, *, model, endpoint, headers, transport=None, max_retries=1,
                 timeout=60, max_context_chars=48000, telemetry=None, before_request=None):
        self.model = model
        self.endpoint = endpoint
        self.headers = headers
        self.transport = transport or requests.post
        self.max_retries = max(0, min(int(max_retries), 2))
        self.timeout = timeout
        self.max_context_chars = max_context_chars
        self.telemetry = telemetry or (lambda event: None)
        self.before_request = before_request or (lambda: None)

    def normalize_error(self, exc):
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, (TimeoutError, requests.Timeout)):
            return ProviderError("timeout", "模型服务请求超时", True)
        if isinstance(exc, (ConnectionError, requests.ConnectionError)):
            return ProviderError("connection", "模型服务连接失败", True)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status:
            return ProviderError("rate_limit" if status == 429 else "http_error", f"模型服务返回 HTTP {status}", status == 429 or status >= 500, status)
        if isinstance(exc, (ValueError, TypeError, KeyError, IndexError, ValidationError)):
            return ProviderError("invalid_output", "模型结构化输出不满足协议", True)
        return ProviderError("provider_error", "模型服务调用失败，请查看错误类型后重试")

    def _messages(self, operation, context, schema):
        # Keep the explicit context layers; large raster arrays and raw tool
        # responses belong in artifacts, never in a model prompt.
        encoded = json.dumps(context, ensure_ascii=False, allow_nan=False)
        if len(encoded) > self.max_context_chars:
            raise ProviderError("context_budget", "模型上下文超过预算，请先压缩历史内容")
        return [{"role": "system", "content": (
            f"你是遥感调查服务，当前操作是 {operation}。只输出严格 JSON，遵循下面的 Schema。"
            "只使用给定证据，不编造日期、指标或引用。无法满足时明确请求用户处理，不能换源或降低要求。"
            + json.dumps(schema, ensure_ascii=False))}, {"role": "user", "content": encoded}]

    def _payload(self, messages, *, stream=False):
        payload = {"model": self.model, "messages": messages, "response_format": {"type": "json_object"},
                   "stream": stream, "max_tokens": 4096}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _call(self, operation, context, schema, *, definitions=None, images=None):
        messages = self._messages(operation, context, schema)
        images = images or []
        if len(images) > 3 or any(not isinstance(item, str) or len(item) > 3_000_000 for item in images):
            raise ProviderError("image_budget", "图像数量或尺寸超过模型输入预算")
        if images:
            messages[-1]["content"] = [{"type": "text", "text": messages[-1]["content"]}] + [
                {"type": "image_url", "image_url": {"url": item}} for item in images]
        for attempt in range(self.max_retries + 1):
            self.before_request()
            started = time.monotonic()
            usage = None
            error = None
            try:
                response = self.transport(self.endpoint, headers=self.headers(), json=self._payload(messages),
                                          timeout=self.timeout, proxies=request_proxies())
                response.raise_for_status()
                body = response.json()
                usage = body.get("usage")
                message = body["choices"][0]["message"]
                if message.get("refusal"):
                    raise ProviderError("refusal", "模型拒绝了本次请求")
                raw = message.get("content")
                if not isinstance(raw, str):
                    raise ValueError("missing public content")
                output = _parse_json_response(raw)
                Draft202012Validator(schema).validate(output)
                if schema is DECISION_SCHEMA:
                    validate_decision(output, definitions)
                return output
            except Exception as exc:
                error = self.normalize_error(exc)
                if not error.retryable or attempt >= self.max_retries:
                    raise error from exc
            finally:
                self.telemetry({"operation": operation, "provider": self.name, "model": self.model,
                                "latency_ms": round((time.monotonic() - started) * 1000), "attempt": attempt + 1,
                                "usage": usage, "image_count": len(images), "error": error.as_dict() if error else None})

    def plan(self, context, schema):
        return self._call("plan", context, schema)

    def decide(self, context, definitions):
        return self._call("decide", context, DECISION_SCHEMA, definitions=definitions)

    def review(self, context, *, images=None):
        return self._call("review", context, DECISION_SCHEMA, images=images)

    def summarize_context(self, context):
        schema = {"type": "object", "required": ["summary", "evidence_refs"], "additionalProperties": False,
                  "properties": {"summary": {"type": "string", "maxLength": 8000}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}}
        output = self._call("summarize_context", context, schema)
        if not set(output["evidence_refs"]).issubset(context.get("evidence_refs", [])):
            raise ProviderError("invalid_summary_refs", "历史摘要包含不存在的证据引用")
        return output

    def stream_decision(self, context, definitions):
        """Yield only a validated complete decision, never partial business facts.

        A broken stream produces an error; replay is an explicit caller action.
        Usage may be unknown if the provider omits its final usage chunk.
        """
        messages = self._messages("decide", context, DECISION_SCHEMA)
        self.before_request()
        started = time.monotonic()
        usage, error, response = None, None, None
        try:
            response = self.transport(self.endpoint, headers=self.headers(), json=self._payload(messages, stream=True),
                                      stream=True, timeout=self.timeout, proxies=request_proxies())
            response.raise_for_status()
            fragments = []
            length = 0
            ended = False
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                text = line[5:].strip()
                if text == "[DONE]":
                    ended = True
                    break
                chunk = _parse_json_response(text)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices", []):
                    part = (choice.get("delta") or {}).get("content")
                    if part:
                        if not isinstance(part, str):
                            raise ValueError("invalid streamed content")
                        length += len(part)
                        if length > 64000:
                            raise ProviderError("output_budget", "模型输出超过预算")
                        fragments.append(part)
            if not ended:
                raise ProviderError("stream_interrupted", "模型流式响应中断，请重试", True)
            yield validate_decision(_parse_json_response("".join(fragments)), definitions)
        except Exception as exc:
            error = self.normalize_error(exc)
            raise error from exc
        finally:
            if response is not None:
                response.close()
            self.telemetry({"operation": "stream_decision", "provider": self.name, "model": self.model,
                            "latency_ms": round((time.monotonic() - started) * 1000), "attempt": 1,
                            "usage": usage, "image_count": 0, "error": error.as_dict() if error else None})


class GLMProvider(OpenAICompatibleProvider):
    name = "glm"

    def _payload(self, messages, *, stream=False):
        payload = super()._payload(messages, stream=stream)
        payload.update({"temperature": 1, "thinking": {"type": "enabled"}})
        return payload


class QwenProvider(OpenAICompatibleProvider):
    name = "qwen"


def configured_provider(*, name=None, model=None, **kwargs):
    provider = (name or os.environ.get("AGENT_PROVIDER", "glm")).lower()
    from ..utils.agent_tools import glm_headers
    if provider == "glm":
        return GLMProvider(model=model or os.environ.get("AGENT_MODEL", AGENT_MODEL),
                           endpoint=os.environ.get("GLM_CHAT_URL", GLM_CHAT_URL), headers=glm_headers, **kwargs)
    options = {
        "qwen": (QwenProvider, "DASHSCOPE_API_KEY", "QWEN_CHAT_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "QWEN_AGENT_MODEL"),
        "openai-compatible": (OpenAICompatibleProvider, "OPENAI_API_KEY", "OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions", "OPENAI_AGENT_MODEL"),
    }
    if provider not in options:
        raise ProviderError("provider_configuration", "未配置所选模型服务")
    cls, key_env, url_env, default_url, model_env = options[provider]
    chosen_model = model or os.environ.get(model_env)
    if not chosen_model:
        raise ProviderError("model_configuration", "请在服务配置中指定模型名称")
    def headers():
        key = os.environ.get(key_env)
        if not key:
            raise ProviderError("credentials_missing", "所选模型服务尚未配置凭据")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return cls(model=chosen_model, endpoint=os.environ.get(url_env, default_url), headers=headers, **kwargs)
