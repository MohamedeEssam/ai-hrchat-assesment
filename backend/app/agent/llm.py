from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import settings


class LLM(Protocol):
    async def structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        ...

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        ...


class OpenAICompatibleLLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model

    def _base_payload(self, prompt: str, *, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if stream:
            payload["stream"] = True
        if settings.openai_reasoning_enabled:
            payload["reasoning"] = {"enabled": True}
        elif "openrouter.ai" in self.base_url:
            # Reasoning models on OpenRouter may think by default, which made free models take minutes.
            # Other OpenAI-compatible servers may reject the unknown field, so only send it here.
            payload["reasoning"] = {"enabled": False}
        return payload

    async def structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = self._base_payload(prompt)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "payload", "schema": schema, "strict": True},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=settings.llm_plan_timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return json.loads(text)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        payload = self._base_payload(prompt, stream=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=settings.llm_answer_timeout) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    item = json.loads(chunk)
                    delta = item["choices"][0].get("delta", {}).get("content")
                    if delta:
                        yield delta


@dataclass
class ScriptedLLM:
    structured_responses: list[dict[str, Any]] = field(default_factory=list)
    stream_responses: list[list[str]] = field(default_factory=list)
    structured_prompts: list[str] = field(default_factory=list)
    stream_prompts: list[str] = field(default_factory=list)

    async def structured(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.structured_prompts.append(prompt)
        if not self.structured_responses:
            return {}
        return self.structured_responses.pop(0)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.stream_prompts.append(prompt)
        chunks: list[str] = []
        if self.stream_responses:
            chunks = self.stream_responses.pop(0)
        for token in chunks:
            await asyncio.sleep(0)
            yield token
