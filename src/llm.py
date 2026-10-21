"""The only file that knows about LLM providers.

Everything else calls generate(system_prompt, user_prompt) -> text.

Fallback: when Groq raises QuotaExceededError (a daily limit), that task's calls go to FALLBACK_PROVIDER /
FALLBACK_EXTRACT_MODEL / FALLBACK_WRITE_MODEL for the rest of the run (reset_fallback() starts a new run). Groq's daily limits
are per model, so the switch is per task. OpenAI spend is tracked in .cache/spend.json; past OPENAI_BUDGET_USD the fallback
stops with BudgetExceededError.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
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
    ("openai", "gpt-5.6-luna"): (0.20, 1.20),
    ("openai", "gpt-5.4-mini"): (0.75, 4.50),
}

FALLBACK_ENV = {"provider": "FALLBACK_PROVIDER", "extract": "FALLBACK_EXTRACT_MODEL", "write": "FALLBACK_WRITE_MODEL"}
SPEND_PATH = os.path.join(".cache", "spend.json")  # running total of OpenAI spend, across runs (delete it to reset)
BUDGET_ENV = "OPENAI_BUDGET_USD"
DEFAULT_BUDGET_USD = 3.00

# Reasoning effort for Groq's gpt-oss models and OpenAI's reasoning models (gpt-5*, o1/o3/o4...). Extraction is many small, simple calls: "low" keeps the hidden
# reasoning (billed as output tokens) short. Writing is one call that must follow many rules: "medium".
EFFORT_ENV = {"extract": "EXTRACT_REASONING_EFFORT", "write": "WRITE_REASONING_EFFORT"}
DEFAULT_EFFORT = {"extract": "low", "write": "medium"}
EFFORTS = ("low", "medium", "high")

# Sampling per task. The writer runs at temperature 0 with a fixed seed so the same facts give (nearly) the same draft:
# writer metrics (sentences dropped) swung 8 -> 0 on identical input at 0.2. Groq and OpenAI treat the seed as best effort.
# Extraction keeps a little temperature; its results are cached, so it doesn't add run-to-run noise there.
SAMPLING = {"extract": {"temperature": 0.2}, "write": {"temperature": 0.0, "seed": 7}}
# OpenAI reasoning models reject any temperature but the default, so they get no sampling settings (and no determinism).
OPENAI_REASONING_RE = re.compile(r"^(?:gpt-5|o[1-9])", re.I)

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


class BudgetExceededError(QuotaExceededError):
    """The OpenAI fallback has spent OPENAI_BUDGET_USD. A QuotaExceededError, so runs and evals stop the same way."""


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
    task: str = ""
    fallback: bool = False


_calls: list[CallRecord] = []
_fallback: dict[str, tuple[str, str]] = {}  # task -> (provider, model) it switched to in this run
# Extraction calls run in parallel threads (graph.extract_node): every read-modify-write of the module state above, the
# truncation counter and the spend file happens under this lock.
_lock = threading.RLock()
_local = threading.local()  # per thread: which (provider, model) served this thread's latest call


def reset_fallback() -> None:
    """Start a new run on the primary provider."""
    with _lock:
        _fallback.clear()


def fallback_tasks() -> list[str]:
    with _lock:
        return sorted(_fallback)


def clear_served() -> None:
    _local.served = None


def served_by() -> tuple[str, str] | None:
    """(provider, model) that answered this thread's latest generate() call, or None (e.g. generate was replaced in a test).
    Other threads may switch the run to the fallback at any moment, so get_settings() after a call can't tell this."""
    return getattr(_local, "served", None)


def primary_settings(task: str = "write") -> tuple[str, str]:
    """(provider, model) from LLM_PROVIDER / EXTRACT_MODEL / WRITE_MODEL / LLM_MODEL, ignoring any fallback."""
    provider = (os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if provider not in DEFAULT_MODELS:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}.")
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}; use one of {TASKS}")
    model = (os.getenv(TASK_ENV[task]) or "").strip() or (os.getenv("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider][task]
    return provider, model


def get_settings(task: str = "write") -> tuple[str, str]:
    """(provider, model) that the next call for `task` will use: the fallback once this run switched to it, else the
    primary. Model precedence: EXTRACT_MODEL/WRITE_MODEL, then LLM_MODEL, then the default."""
    primary = primary_settings(task)
    with _lock:
        return _fallback.get(task, primary)


def fallback_settings(task: str) -> tuple[str, str] | None:
    """(provider, model) from FALLBACK_PROVIDER / FALLBACK_<TASK>_MODEL, or None when no fallback is configured."""
    provider = (os.getenv(FALLBACK_ENV["provider"]) or "").strip().lower()
    if not provider:
        return None
    if provider not in DEFAULT_MODELS:
        raise FatalLLMError(f"Unknown {FALLBACK_ENV['provider']} '{provider}'. Use one of: {', '.join(DEFAULT_MODELS)}.")
    model = (os.getenv(FALLBACK_ENV[task]) or "").strip() or DEFAULT_MODELS[provider][task]
    return provider, model


def is_reasoning_model(provider: str, model: str) -> bool:
    return (provider == "groq" and "gpt-oss" in model.lower()) or (provider == "openai" and bool(OPENAI_REASONING_RE.match(model)))


def sampling_for(task: str, provider: str, model: str) -> dict:
    return {} if provider == "openai" and OPENAI_REASONING_RE.match(model) else SAMPLING[task]


def get_reasoning_effort(task: str, provider: str, model: str) -> str | None:
    """EXTRACT_/WRITE_REASONING_EFFORT for Groq's gpt-oss models and OpenAI's reasoning models; None (= don't send it)
    for anything else."""
    if not is_reasoning_model(provider, model):
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
    with _lock:
        return _truncated


def _chat(client, model: str, system_prompt: str, user_prompt: str, reasoning_effort: str | None = None,
          sampling: dict | None = None):
    """One chat-completions call (Groq and OpenAI share the shape). `sampling`: temperature (default 0.2) and an optional seed.
    Returns (text, input tokens, output tokens, reasoning tokens or None)."""
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    sampling = {"temperature": 0.2} if sampling is None else sampling
    resp = client.chat.completions.create(
        model=model,
        **sampling,
        max_completion_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        **extra,
    )
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        global _truncated
        with _lock:
            _truncated += 1
        print(f"[llm] WARNING: output hit the {MAX_OUTPUT_TOKENS}-token limit and was cut off", file=sys.stderr)
    details = getattr(resp.usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    return choice.message.content or "", resp.usage.prompt_tokens, resp.usage.completion_tokens, reasoning


def _call_groq(model, system_prompt, user_prompt, reasoning_effort=None, sampling=None):
    from groq import Groq

    return _chat(Groq(api_key=_require_key("GROQ_API_KEY")), model, system_prompt, user_prompt, reasoning_effort, sampling)


def _call_openai(model, system_prompt, user_prompt, reasoning_effort=None, sampling=None):
    from openai import OpenAI

    return _chat(OpenAI(api_key=_require_key("OPENAI_API_KEY")), model, system_prompt, user_prompt, reasoning_effort, sampling)


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


def budget_usd() -> float:
    raw = (os.getenv(BUDGET_ENV) or "").strip()
    try:
        return float(raw) if raw else DEFAULT_BUDGET_USD
    except ValueError:
        raise FatalLLMError(f"{BUDGET_ENV} must be a number of dollars, e.g. 3.00 (got '{raw}'). Fix it in your .env file.")


def load_spend() -> dict:
    """{"openai_usd", "calls", "input_tokens", "output_tokens"} over every OpenAI call ever tracked (missing file = 0)."""
    try:
        with open(SPEND_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {"openai_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0, **data}


def _add_spend(cost: float, in_tok: int, out_tok: int) -> dict:
    with _lock:  # read-modify-write of the file: one thread at a time
        return _add_spend_locked(cost, in_tok, out_tok)


def _add_spend_locked(cost: float, in_tok: int, out_tok: int) -> dict:
    spend = load_spend()
    spend.update(openai_usd=round(spend["openai_usd"] + cost, 6), calls=spend["calls"] + 1,
                 input_tokens=spend["input_tokens"] + in_tok, output_tokens=spend["output_tokens"] + out_tok)
    os.makedirs(os.path.dirname(SPEND_PATH) or ".", exist_ok=True)
    with open(SPEND_PATH, "w") as f:
        json.dump(spend, f, indent=1)
    return spend


def _check_budget(provider: str, model: str) -> None:
    """Before a fallback call to OpenAI: refuse once the tracked spend has reached the budget."""
    if provider != "openai":
        return
    if (provider, model) not in PRICES:
        raise FatalLLMError(f"Fallback model openai/{model} has no price in PRICES (src/llm.py), so its spend can't be checked "
                            f"against {BUDGET_ENV}. Add its price or pick another FALLBACK_*_MODEL.")
    with _lock:
        spent = load_spend()["openai_usd"]
    budget = budget_usd()
    if spent >= budget:
        raise BudgetExceededError(
            f"OpenAI fallback budget used up: ${spent:.2f} spent of {BUDGET_ENV}=${budget:.2f} (tracked in {SPEND_PATH}). "
            f"Not calling openai/{model}. Wait for Groq's daily limit to reset, or raise {BUDGET_ENV} in .env.")


def generate(system_prompt: str, user_prompt: str, task: str = "write") -> str:
    """Call the model for `task` ("extract" or "write") and record tokens, latency and cost.

    If Groq raises QuotaExceededError and a fallback is configured, this task switches to the fallback for the rest of
    the run (the call is retried there). Reasoning effort goes to Groq gpt-oss and OpenAI reasoning models; the task's
    SAMPLING (temperature, seed) to every model that accepts it.

    Thread-safe: parallel calls may hit the daily limit together; one of them switches the task to the fallback (and logs
    it), the others retry there too. Budget checks are per call, so parallel calls can overshoot the budget by a few calls."""
    with _lock:
        provider, model = get_settings(task)
        on_fallback = _fallback.get(task) == (provider, model)
    try:
        return _generate_with(provider, model, task, system_prompt, user_prompt, fallback=on_fallback)
    except QuotaExceededError as exc:
        if on_fallback or provider != "groq" or isinstance(exc, BudgetExceededError):
            raise
        with _lock:
            target, switched = _fallback.get(task), False
            if target is None:  # first thread to hit the limit: switch the task for everyone
                target = fallback_settings(task)
                if target is None or target == (provider, model):
                    raise
                _check_budget(*target)  # a fallback we can't pay for: fail now, with Groq's message kept
                _fallback[task], switched = target, True
        if switched:
            print(f"[llm] FALLBACK: {provider}/{model} hit its daily limit; {task} calls now go to {target[0]}/{target[1]} "
                  f"for the rest of this run", file=sys.stderr)
        return _generate_with(*target, task, system_prompt, user_prompt, fallback=True)


def _generate_with(provider: str, model: str, task: str, system_prompt: str, user_prompt: str, fallback: bool) -> str:
    effort = get_reasoning_effort(task, provider, model)
    call = _PROVIDERS[provider]
    if fallback:
        _check_budget(provider, model)

    for attempt in range(RETRIES + 1):
        start = time.perf_counter()
        try:
            text, in_tok, out_tok, *rest = call(model, system_prompt, user_prompt, effort, sampling=sampling_for(task, provider, model))
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
    with _lock:
        _calls.append(CallRecord(provider, model, in_tok, out_tok, latency, cost, reasoning, effort, task, fallback))
    _local.served = (provider, model)
    spend_note = ""
    if provider == "openai" and cost is not None:
        spend = _add_spend(cost, in_tok, out_tok)
        spend_note = f" ${cost:.4f} (OpenAI total ${spend['openai_usd']:.2f} of ${budget_usd():.2f})" if fallback else f" ${cost:.4f}"
    print(f"[llm] {task} via {provider}/{model}{' [FALLBACK]' if fallback else ''}"
          f"{f' effort={effort}' if effort else ''} in={in_tok} out={out_tok}{spend_note}", file=sys.stderr)
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
    with _lock:
        _calls.clear()


def _snapshot() -> list[CallRecord]:
    with _lock:
        return list(_calls)


def models_used() -> dict[str, list[str]]:
    """{task: ["provider/model", ...]} over every call since reset_usage(), in first-use order."""
    calls = _snapshot()
    used: dict[str, list[str]] = {}
    for c in calls:
        name = f"{c.provider}/{c.model}"
        if name not in used.setdefault(c.task, []):
            used[c.task].append(name)
    return used


def usage_totals() -> dict:
    """Token and time totals over every call since reset_usage() (reasoning tokens counted when the provider reports them),
    plus which provider/model served each task and the dollar cost of the calls with a price."""
    calls = _snapshot()
    return {
        "models": models_used(),
        "fallback_calls": sum(c.fallback for c in calls),
        "cost": round(sum(c.cost or 0 for c in calls), 6),
        "calls": len(calls),
        "input": sum(c.input_tokens for c in calls),
        "output": sum(c.output_tokens for c in calls),
        "reasoning": sum(c.reasoning_tokens or 0 for c in calls),
        "latency": sum(c.latency for c in calls),
    }


def _reasoning_total(calls: list[CallRecord]) -> int | None:
    """Reasoning tokens over the calls that reported them (they are already counted inside the output tokens)."""
    reported = [c.reasoning_tokens for c in calls if c.reasoning_tokens is not None]
    return sum(reported) if reported else None


def summary_line() -> str:
    """One-line usage summary over every LLM call since reset_usage(), with tokens per model.
    Reasoning tokens are shown separately when the provider returns them."""
    all_calls = _snapshot()
    if not all_calls:
        return "[llm] no calls made"
    in_tok = sum(c.input_tokens for c in all_calls)
    out_tok = sum(c.output_tokens for c in all_calls)
    latency = sum(c.latency for c in all_calls)
    costs = [c.cost for c in all_calls]
    cost = "n/a (no price configured)" if None in costs else f"${sum(costs):.4f}"
    total_reasoning = _reasoning_total(all_calls)
    by_model: dict[str, list[CallRecord]] = {}
    for c in all_calls:
        by_model.setdefault(f"{c.provider}/{c.model}" + (" [FALLBACK]" if c.fallback else ""), []).append(c)
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
    providers = "+".join(dict.fromkeys(c.provider for c in all_calls))
    fallback_note = ""
    if any(c.fallback for c in all_calls):
        fallback_note = (f" | FALLBACK used for {', '.join(sorted({c.task for c in all_calls if c.fallback}))}; "
                         f"OpenAI spend so far ${load_spend()['openai_usd']:.2f} of ${budget_usd():.2f}")
    return (
        f"[llm] provider={providers} calls={len(all_calls)} in={in_tok} tok out={out_tok} tok{reasoning_note} "
        f"latency={latency:.1f}s est_cost={cost} | by model: {'; '.join(parts)}{fallback_note}"
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
