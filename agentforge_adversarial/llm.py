"""LLM client factories + provider-agnostic JSON chat helper.

The platform splits model families across the two LLM-using roles to
honour ARCHITECTURE.md §2 ("Judge and Red Team must be different model
families to avoid attacker/judge conflict of interest"). Concretely:

  - Judge uses OpenAI (``JUDGE_MODEL``, default ``gpt-4o-mini``).
  - Red Team mutator + class-probe use Anthropic (``MUTATOR_MODEL``,
    default ``claude-haiku-4-5-20251001``).

``chat_json`` abstracts the SDK shape difference so mutator.py and
class_probe.py stay provider-agnostic: pass any of {AsyncOpenAI,
AsyncAnthropic} + the model name + system/user prompts, get back a parsed
dict plus token counts for ``cost.record_usage``.
"""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI


JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
MUTATOR_MODEL = os.environ.get("MUTATOR_MODEL", "claude-haiku-4-5-20251001")


def make_openai(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key)


def make_anthropic(api_key: str) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=api_key)


def is_anthropic_model(model: str) -> bool:
    return model.startswith("claude")


async def chat_json(
    client: AsyncOpenAI | AsyncAnthropic,
    model: str,
    *,
    system: str,
    user: str,
    temperature: float = 0.9,
    max_tokens: int = 4096,
) -> tuple[dict[str, Any], int, int]:
    """Chat-completion-with-JSON-output across providers.

    Returns ``(parsed_dict, tokens_in, tokens_out)``. Raises
    ``json.JSONDecodeError`` if the provider returns non-JSON; callers
    typically catch this and degrade to empty output.
    """
    if is_anthropic_model(model):
        # Anthropic doesn't expose an explicit JSON-response mode; we
        # rely on the system prompt instructing JSON-only and strip
        # any markdown fences the model wraps it in.
        msg = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=temperature,
        )
        text = "".join(
            getattr(block, "text", "") for block in msg.content
        ).strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text or "{}")
        usage = msg.usage
        return parsed, int(usage.input_tokens), int(usage.output_tokens)

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    parsed = json.loads(resp.choices[0].message.content or "{}")
    usage = resp.usage
    pin = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    pout = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return parsed, pin, pout
