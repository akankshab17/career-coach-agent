"""The only file that knows about LLM providers.

Everything else calls generate(system_prompt, user_prompt) -> text.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass

DEFAULT_PROVIDER = "groq"
# Two jobs, two models: many small extraction calls on a cheaper model, one writing call on the best one.
TASKS = ("extract", "write")
TASK_ENV = {"extract": "EXTRACT_MODEL", "write": "WRITE_MODEL"}
DEFAULT_MODELS = {
    "groq": {"extract": "openai/gpt-oss-20b", "write": "openai/gpt-oss-120b"},
    "openai": {"extract": "gpt-4o-mini", "write": "gpt-4o-mini"},
}

# USD per 1M tokens: (input, output). Models not listed show cost as n/a.
PRICES = {
    ("groq", "openai/gpt-oss-120b"): (0.0, 0.0),  # free tier
    ("groq", "openai/gpt-oss-20b"): (0.0, 0.0),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
}

# Reasoning effort for Groq's gpt-oss models. Extraction is many small, simple calls: "low" keeps the hidden
# reasoning (billed as output tokens) short. Writing is one call that must follow many rules: "medium".
EFFORT_ENV = {"extract": "EXTRACT_REASONING_EFFORT", "write": "WRITE_REASONING_EFFORT"}
DEFAULT_EFFORT = {"extract": "low", "write": "medium"}
EFFORTS = ("low", "medium", "high")

MAX_OUTPUT_TOKENS = 16384  # reasoning models (gpt-oss) spend part of this on hidden reasoning
RETRIES = 2  # retries after the first attempt -> 3 attempts total
BACKOFF_SECONDS = 2  # waits 2s, then 4s
_sleep = time.sleep  # replaced in tests


class LLMError(Exception):
    """Raised with a user-readable message when the LLM call cannot succeed."""


class FatalLLMError(LLMError):
    """Retrying, or trying other calls, is pointless (daily quota, bad key). Callers must stop the run."""


class QuotaExceededError(FatalLLMError):
    """A daily limit (tokens/requests per day) or account quota is used up."""


@dataclass
class CallRecord:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency: float
    cost: float | None
    reasoning_tokens: int | None = None  # part of output_tokens, when the provider reports it
    effort: str | None = None


_calls: list[CallRecord] = []


def get_settings(task: str = "write") -> tuple[str, str]:
    """(provider, model) for a task. Model precedence: EXTRACT_MODEL/WRITE_MODEL, then LLM_MODEL, then the default."""
    provider = (os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in DEFAULT_MODELS:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}.")
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; use one of {TASKS}")
    model = (os.getenv(TASK_ENV[task]) or "").strip() or (os.getenv("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider][task]
    return provider, model


def get_reasoning_effort(task: str, provider: str, model: str) -> str | None:
    """EXTRACT_/WRITE_REASONING_EFFORT for Groq's gpt-oss models; None (= don't send it) for anything else."""
    if provider != "groq" or "gpt-oss" not in model.lower():
        return None
    raw = (os.getenv(EFFORT_ENV[task]) or "").strip().lower()
    effort = raw or DEFAULT_EFFORT[task]
    if effort not in EFFORTS:
        raise FatalLLMError(f"{EFFORT_ENV[task]} must be one of {', '.join(EFFORTS)} (got '{raw}'). Fix it in your .env file.")
    return effort


def _require_key(name: str) -> str:
    key = os.getenv(name)
    if not key:
        raise FatalLLMError(f"{name} is not set. Add it to your .env file.")
    return key


_truncated = 0  # how many responses so far were cut off by the output-token limit


def truncation_count() -> int:
    return _truncated


def _chat(client, model: str, system_prompt: str, user_prompt: str, reasoning_effort: str | None = None):
    """One chat-completions call (Groq and OpenAI share the shape).
    Returns (text, input tokens, output tokens, reasoning tokens or None)."""
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        **extra,
    )
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        global _truncated
        _truncated += 1
        print(f"[llm] WARNING: output hit the {MAX_OUTPUT_TOKENS}-token limit and was cut off", file=sys.stderr)
    details = getattr(resp.usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    return choice.message.content or "", resp.usage.prompt_tokens, resp.usage.completion_tokens, reasoning


def _call_groq(model, system_prompt, user_prompt, reasoning_effort=None):
    from groq import Groq

    return _chat(Groq(api_key=_require_key("GROQ_API_KEY")), model, system_prompt, user_prompt, reasoning_effort)


def _call_openai(model, system_prompt, user_prompt, reasoning_effort=None):
    from openai import OpenAI

    return _chat(OpenAI(api_key=_require_key("OPENAI_API_KEY")), model, system_prompt, user_prompt, reasoning_effort)


_PROVIDERS = {"groq": _call_groq, "openai": _call_openai}
_RETRYABLE = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")
_AUTH_ERRORS = ("AuthenticationError", "PermissionDeniedError")
DAILY_LIMIT_RE = re.compile(r"per day|\(TPD\)|\(RPD\)|insufficient_quota|exceeded your current quota", re.I)
MAX_RETRY_AFTER = 30  # per-minute limits: wait what the provider asks, but never longer than this


def _error_text(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return f"{error['message']} {error.get('code', '')}".strip()
    return str(exc)


def parse_wait_seconds(text: str, headers=None) -> float | None:
    """Seconds the provider asks us to wait: 'try again in 8m34.56s' / '1h2m3s' / '250ms', else a Retry-After header."""
    m = re.search(r"try again in\s+((?:\d+(?:\.\d+)?\s*(?:h|m(?!s)|ms|s)\s*)+)", text, re.I)
    if m:
        parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", m.group(1), re.I)
        unit = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
        return sum(float(n) * unit[u.lower()] for n, u in parts)
    try:
        return float((headers or {}).get("retry-after"))
    except (TypeError, ValueError):
        return None


def format_wait(seconds: float | None) -> str:
    if seconds is None:
        return "an unknown time (Groq did not say)"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    text = " ".join(part for part in (f"{h}h" if h else "", f"{m}m" if m else "", f"{sec}s" if sec or not (h or m) else "") if part)
    return f"{text} (around {time.strftime('%H:%M', time.localtime(time.time() + seconds))} local time)"


def _classify(exc: Exception, provider: str, model: str) -> LLMError | float | None:
    """A FatalLLMError for quota/auth problems; a wait in seconds for a retryable per-minute 429; None = plain retry."""
    name = type(exc).__name__
    text = _error_text(exc)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if name in _AUTH_ERRORS:
        return FatalLLMError(f"{provider} rejected the API key ({name}). Check it in your .env file.")
    if name == "RateLimitError":
        wait = parse_wait_seconds(text, headers)
        if DAILY_LIMIT_RE.search(text):
            return QuotaExceededError(
                f"{provider} daily limit reached on {model}. Retrying won't help: try again in {format_wait(wait)}.\n"
                f"{provider} said: {text[:300]}\n"
                "Fact extractions finished so far are cached in .cache/extract, so a re-run continues where this stopped. "
                "You can also switch model (EXTRACT_MODEL / WRITE_MODEL) or LLM_PROVIDER in .env."
            )
        return wait
    return None


def generate(system_prompt: str, user_prompt: str, task: str = "write") -> str:
    """Call the configured model for `task` ("extract" or "write") and record tokens, latency and cost.
    For Groq gpt-oss models the task's reasoning effort is sent along; other providers/models ignore it."""
    provider, model = get_settings(task)
    effort = get_reasoning_effort(task, provider, model)
    call = _PROVIDERS[provider]

    for attempt in range(RETRIES + 1):
        start = time.perf_counter()
        try:
            text, in_tok, out_tok, *rest = call(model, system_prompt, user_prompt, effort)
            reasoning = rest[0] if rest else None
            break
        except LLMError:
            raise
        except Exception as exc:
            verdict = _classify(exc, provider, model)
            if isinstance(verdict, LLMError):
                raise verdict from exc
            if type(exc).__name__ not in _RETRYABLE:
                raise LLMError(f"{provider} call failed: {type(exc).__name__}: {exc}") from exc
            if attempt == RETRIES:
                raise LLMError(
                    f"{provider} call failed after {RETRIES + 1} attempts "
                    f"({type(exc).__name__}: {exc}). Wait a bit and try again."
                ) from exc
            wait = BACKOFF_SECONDS * 2**attempt
            if verdict is not None:  # the provider told us how long: honour it (up to a cap)
                wait = max(wait, min(verdict + 0.5, MAX_RETRY_AFTER))
            print(f"[llm] {type(exc).__name__} on {model}; retrying in {wait:.1f}s ({attempt + 1}/{RETRIES})", file=sys.stderr)
            _sleep(wait)
    latency = time.perf_counter() - start

    price = PRICES.get((provider, model))
    cost = (in_tok * price[0] + out_tok * price[1]) / 1_000_000 if price else None
    _calls.append(CallRecord(provider, model, in_tok, out_tok, latency, cost, reasoning, effort))
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


def _reasoning_total(calls: list[CallRecord]) -> int | None:
    """Reasoning tokens over the calls that reported them (they are already counted inside the output tokens)."""
    reported = [c.reasoning_tokens for c in calls if c.reasoning_tokens is not None]
    return sum(reported) if reported else None


def summary_line() -> str:
    """One-line usage summary over every LLM call since reset_usage(), with tokens per model.
    Reasoning tokens are shown separately when the provider returns them."""
    if not _calls:
        return "[llm] no calls made"
    in_tok = sum(c.input_tokens for c in _calls)
    out_tok = sum(c.output_tokens for c in _calls)
    latency = sum(c.latency for c in _calls)
    costs = [c.cost for c in _calls]
    cost = "n/a (no price configured)" if None in costs else f"${sum(costs):.4f}"
    total_reasoning = _reasoning_total(_calls)
    by_model: dict[str, list[CallRecord]] = {}
    for c in _calls:
        by_model.setdefault(c.model, []).append(c)
    parts = []
    for model, calls in by_model.items():
        details = []
        if (r := _reasoning_total(calls)) is not None:
            details.append(f"reasoning={r}")
        if efforts := sorted({c.effort for c in calls if c.effort}):
            details.append("effort=" + "/".join(efforts))
        parts.append(f"{model} {len(calls)} calls in={sum(c.input_tokens for c in calls)} "
                     f"out={sum(c.output_tokens for c in calls)}" + (f" ({', '.join(details)})" if details else ""))
    reasoning_note = f" (of which reasoning={total_reasoning})" if total_reasoning is not None else ""
    return (
        f"[llm] provider={_calls[-1].provider} calls={len(_calls)} in={in_tok} tok out={out_tok} tok{reasoning_note} "
        f"latency={latency:.1f}s est_cost={cost} | by model: {'; '.join(parts)}"
    )


if __name__ == "__main__":
    # Usage: python src/llm.py models [groq|openai]
    from dotenv import load_dotenv

    load_dotenv()
    if sys.argv[1:2] != ["models"]:
        sys.exit("Usage: python src/llm.py models [groq|openai]")
    try:
        provider = sys.argv[2] if len(sys.argv) > 2 else get_settings()[0]
        print(f"# {provider} models (defaults: {DEFAULT_MODELS.get(provider)})")
        print("\n".join(list_models(provider)))
    except LLMError as exc:
        sys.exit(f"Error: {exc}")
