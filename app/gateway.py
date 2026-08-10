"""Async LLM gateway: one call per model against opencode-go's API endpoints.

Protocol families (per https://opencode.ai/docs/zh-cn/go):
- chat:      POST {base}/chat/completions   (OpenAI-compatible)
- responses: POST {base}/responses          (OpenAI Responses)
- anthropic: POST {base}/messages           (Anthropic Messages)
"""
import asyncio
import json
import re
from datetime import datetime
from typing import Any

import httpx

from .config import config

DEFAULT_TIMEOUT = 180.0  # 秒；网页设置项未传时使用
TIMEOUT = httpx.Timeout(DEFAULT_TIMEOUT)
ANTHROPIC_VERSION = "2023-06-01"


_PROVIDER_PREFIXES = [
    ("deepseek", "DeepSeek"),
    ("glm", "智谱 GLM"),
    ("kimi", "月之暗面 Kimi"),
    ("grok", "xAI"),
    ("gpt", "OpenAI"),
    ("mimo", "阶跃星辰"),
    ("minimax", "MiniMax"),
    ("qwen", "阿里云通义"),
]


def provider_for(model_id: str) -> str:
    """Map a model id to its provider label via id prefix."""
    low = model_id.lower()
    for prefix, label in _PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return label
    return "其他"


def family_for(model_id: str) -> str:
    """Guess the API family for a model id (used for live-discovered models)."""
    if model_id == "gpt-5.6-luna":
        return "responses"
    if model_id.startswith("minimax-") or model_id.startswith("qwen3"):
        return "anthropic"
    return "chat"


def _now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def fill_prompt(template: str, question: str, answer_a: str, answer_b: str) -> str:
    """Replace {now}/{question}/{answer_a}/{answer_b} placeholders; append Q&A at the end if absent."""
    now = _now_text()
    has_now = "{now}" in template
    template = template.replace("{now}", now)
    if any(p in template for p in ("{question}", "{answer_a}", "{answer_b}")):
        return (
            template.replace("{question}", question)
            .replace("{answer_a}", answer_a)
            .replace("{answer_b}", answer_b)
        )
    suffix = f"\n\n问题：\n{question}\n\n答案A：\n{answer_a}\n\n答案B：\n{answer_b}"
    if not has_now:
        suffix = f"\n\n当前时间：{now}" + suffix
    return template + suffix


def parse_json(content: str) -> dict | None:
    """Extract the first JSON object from model output (tolerates fences)."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _anthropic_text(data: dict) -> str:
    """Extract concatenated text blocks from an Anthropic /messages response (skips thinking blocks)."""
    parts = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    if not parts:
        raise ValueError("响应中未找到文本内容")
    return "".join(parts)


def _responses_text(data: dict) -> str:
    """Extract text from an OpenAI Responses API response (skips reasoning blocks)."""
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                parts.append(block.get("text", ""))
    if not parts:
        raise ValueError("响应中未找到文本内容")
    return "".join(parts)


async def _chat_completions(client: httpx.AsyncClient, model_id: str, prompt: str, temperature: float | None) -> str:
    payload: dict[str, Any] = {"model": model_id, "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        payload["temperature"] = temperature
    resp = await client.post(
        f"{config.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {config.api_key}"},
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _anthropic_messages(client: httpx.AsyncClient, model_id: str, prompt: str) -> str:
    resp = await client.post(
        f"{config.base_url}/messages",
        headers={"x-api-key": config.api_key, "anthropic-version": ANTHROPIC_VERSION},
        json={"model": model_id, "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]},
    )
    resp.raise_for_status()
    return _anthropic_text(resp.json())


async def _responses_api(client: httpx.AsyncClient, model_id: str, prompt: str) -> str:
    resp = await client.post(
        f"{config.base_url}/responses",
        headers={"Authorization": f"Bearer {config.api_key}"},
        json={"model": model_id, "input": prompt},
    )
    resp.raise_for_status()
    return _responses_text(resp.json())


_HANDLERS = {"chat": _chat_completions, "anthropic": _anthropic_messages, "responses": _responses_api}


PROBE_TIMEOUT = httpx.Timeout(15.0)


async def probe_model(model_id: str, api: str | None = None, temperature: float | None = None) -> dict[str, Any]:
    """Health-check one model with a tiny request. Returns {model, ok, error?}."""
    api = api or family_for(model_id)
    handler = _HANDLERS.get(api, _chat_completions)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            if api == "chat":
                await handler(client, model_id, "ping", temperature)
            else:
                await handler(client, model_id, "ping")
        return {"model": model_id, "ok": True}
    except Exception as exc:
        return {"model": model_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def evaluate_one(
    model_id: str, prompt: str, api: str | None = None, temperature: float | None = None, timeout: float | None = None
) -> dict[str, Any]:
    """Call one model. Returns {model, api, ok, result|raw|error}. Retries once on 5xx."""
    api = api or family_for(model_id)
    handler = _HANDLERS.get(api, _chat_completions)
    client_timeout = httpx.Timeout(timeout) if timeout else TIMEOUT
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=client_timeout) as client:
                content = await handler(client, model_id, prompt, temperature) if api == "chat" else await handler(client, model_id, prompt)
            parsed = parse_json(content)
            return {"model": model_id, "api": api, "ok": True, "raw": content, "result": parsed}
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(1.0)
                continue
            break
        except Exception as exc:  # network, timeout, malformed body
            last_exc = exc
            break
    return {"model": model_id, "api": api, "ok": False, "error": f"{type(last_exc).__name__}: {last_exc}"}


async def fetch_live_models() -> list[dict] | None:
    """Fetch available models from {base}/models. Returns None on any failure."""
    if not config.api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(
                f"{config.base_url}/models",
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
        return [{"id": item["id"], "name": item["id"]} for item in data if item.get("id")]
    except Exception:
        return None
