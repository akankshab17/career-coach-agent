"""The only file that knows about LLM providers.

Everything else calls generate(system_prompt, user_prompt) -> text.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

DEFAULT_PROVIDER = "groq"
DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "openai": "gpt-4o-mini"}

# USD per 1M tokens: (input, output). Models not listed show cost as n/a.
PRICES = {
    ("groq", "openai/gpt-oss-120b"): (0.0, 0.0),  # free tier
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
}

RETRIES = 2  # retries after the first attempt -> 3 attempts total
BACKOFF_SECONDS = 2  # waits 2s, then 4s
_sleep = time.sleep  # replaced in tests


class LLMError(Exception):
    """Raised with a user-readable message when the LLM call cannot succeed."""


@dataclass
class CallRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency: float
    cost: float | None


_calls: list[CallRecord] = []


def get_settings() -> tuple[str, str]:
    provider = (os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in DEFAULT_MODELS:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}.")
    model = (os.getenv("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider]
    return provider, model


def _require_key(name: str) -> str:
    key = os.getenv(name)
    if not key:
        raise LLMError(f"{name} is not set. Add it to your .env file.")
    return key


def _chat(client, model: str, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
    # Groq and OpenAI share the same chat-completions shape.
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    )
    return resp.choices[0].message.content, resp.usage.prompt_tokens, resp.usage.completion_tokens


def _call_groq(model, system_prompt, user_prompt):
    from groq import Groq

    return _chat(Groq(api_key=_require_key("GROQ_API_KEY")), model, system_prompt, user_prompt)


def _call_openai(model, system_prompt, user_prompt):
    from openai import OpenAI

    return _chat(OpenAI(api_key=_require_key("OPENAI_API_KEY")), model, system_prompt, user_prompt)


_PROVIDERS = {"groq": _call_groq, "openai": _call_openai}
_RETRYABLE = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")


def _is_retryable(exc: Exception) -> bool:
    return type(exc).__name__ in _RETRYABLE


def generate(system_prompt: str, user_prompt: str) -> str:
    provider, model = get_settings()
    call = _PROVIDERS[provider]

    for attempt in range(RETRIES + 1):
        start = time.perf_counter()
        try:
            text, in_tok, out_tok = call(model, system_prompt, user_prompt)
            break
        except LLMError:
            raise
        except Exception as exc:
            if not _is_retryable(exc):
                raise LLMError(f"{provider} call failed: {type(exc).__name__}: {exc}") from exc
            if attempt == RETRIES:
                raise LLMError(
                    f"{provider} call failed after {RETRIES + 1} attempts "
                    f"({type(exc).__name__}: {exc}). Wait a bit and try again."
                ) from exc
            wait = BACKOFF_SECONDS * 2**attempt
            print(f"[llm] {type(exc).__name__}; retrying in {wait}s ({attempt + 1}/{RETRIES})", file=sys.stderr)
            _sleep(wait)
    latency = time.perf_counter() - start

    price = PRICES.get((provider, model))
    cost = (in_tok * price[0] + out_tok * price[1]) / 1_000_000 if price else None
    _calls.append(CallRecord(provider, model, in_tok, out_tok, latency, cost))
    return text


def list_models(provider: str | None = None) -> list[str]:
    """Ask the provider's /models endpoint which model ids are currently available."""
    provider = provider or get_settings()[0]
    if provider == "groq":
        from groq import Groq

        client = Groq(api_key=_require_key("GROQ_API_KEY"))
    elif provider == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=_require_key("OPENAI_API_KEY"))
    else:
        raise LLMError(f"Unknown provider '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}.")
    try:
        return sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        raise LLMError(f"Could not list {provider} models: {type(exc).__name__}: {exc}") from exc


def reset_usage() -> None:
    _calls.clear()


def summary_line() -> str:
    """One-line usage summary, totalled over every LLM call made since reset_usage()."""
    if not _calls:
        return "[llm] no calls made"
    last = _calls[-1]
    in_tok = sum(c.input_tokens for c in _calls)
    out_tok = sum(c.output_tokens for c in _calls)
    latency = sum(c.latency for c in _calls)
    costs = [c.cost for c in _calls]
    cost = "n/a (no price configured)" if None in costs else f"${sum(costs):.4f}"
    return (
        f"[llm] provider={last.provider} model={last.model} calls={len(_calls)} in={in_tok} tok "
        f"out={out_tok} tok latency={latency:.1f}s est_cost={cost}"
    )


if __name__ == "__main__":
    # Usage: python src/llm.py models [groq|openai]
    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1:2] != ["models"]:
        sys.exit("Usage: python src/llm.py models [groq|openai]")
    try:
        provider = sys.argv[2] if len(sys.argv) > 2 else get_settings()[0]
        print(f"# {provider} models (default: {DEFAULT_MODELS.get(provider)})")
        print("\n".join(list_models(provider)))
    except LLMError as exc:
        sys.exit(f"Error: {exc}")
