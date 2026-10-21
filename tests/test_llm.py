import pytest

import llm


def _fake_provider(name, calls):
    def fn(model, system_prompt, user_prompt, effort=None, **_):
        calls.append((name, model))
        return "text", 1000, 500
    return fn


def test_provider_switch(monkeypatch):
    calls = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", _fake_provider("groq", calls))
    monkeypatch.setitem(llm._PROVIDERS, "openai", _fake_provider("openai", calls))
    llm.reset_usage()

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm.generate("s", "u")  # default -> groq
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    llm.generate("s", "u")
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    llm.generate("s", "u")

    assert calls == [("groq", "openai/gpt-oss-120b"), ("openai", "gpt-4o-mini"), ("openai", "custom-model")]
    line = llm.summary_line()
    assert "provider=groq+openai calls=3 in=3000 tok out=1500 tok" in line
    assert ("by model: groq/openai/gpt-oss-120b 1 calls in=1000 out=500 (effort=medium); openai/gpt-4o-mini 1 calls in=1000 out=500; "
            "openai/custom-model 1 calls in=1000 out=500") in line


def test_retries_then_clear_error(monkeypatch):
    class RateLimitError(Exception):
        pass

    attempts = []

    def flaky(model, s, u, effort=None, **_):
        attempts.append(1)
        raise RateLimitError("429")

    monkeypatch.setitem(llm._PROVIDERS, "groq", flaky)
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    try:
        llm.generate("s", "u")
        assert False, "should have raised"
    except llm.LLMError as exc:
        assert "after 3 attempts" in str(exc)
    assert len(attempts) == 3


def test_truncated_output_is_flagged(capsys):
    from types import SimpleNamespace as NS

    resp = NS(choices=[NS(finish_reason="length", message=NS(content="## TL;DR\n- cut"))],
              usage=NS(prompt_tokens=10, completion_tokens=20))
    client = NS(chat=NS(completions=NS(create=lambda **kw: resp)))
    before = llm.truncation_count()
    text, *_ = llm._chat(client, "m", "s", "u")
    assert llm.truncation_count() == before + 1
    assert text.startswith("## TL;DR")
    assert "was cut off" in capsys.readouterr().err


# ---- model routing ----

def test_extract_and_write_use_different_models_by_default(monkeypatch):
    used = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, **_: used.append(model) or ("text", 100, 50))
    llm.reset_usage()
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u", task="write")
    llm.generate("s", "u")  # task defaults to write
    assert used == ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "openai/gpt-oss-120b"]
    line = llm.summary_line()
    assert "openai/gpt-oss-20b 1 calls in=100 out=50" in line and "openai/gpt-oss-120b 2 calls in=200 out=100" in line


def test_model_env_precedence(monkeypatch):
    assert llm.get_settings("extract") == ("groq", "openai/gpt-oss-20b")
    assert llm.get_settings("write") == ("groq", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_MODEL", "one-model")  # one model for both tasks
    assert llm.get_settings("extract")[1] == llm.get_settings("write")[1] == "one-model"
    monkeypatch.setenv("EXTRACT_MODEL", "small")  # the per-task variable wins
    monkeypatch.setenv("WRITE_MODEL", " big ")
    assert (llm.get_settings("extract")[1], llm.get_settings("write")[1]) == ("small", "big")
    monkeypatch.setenv("EXTRACT_MODEL", "")  # blank = not set
    assert llm.get_settings("extract")[1] == "one-model"
    with pytest.raises(ValueError):
        llm.get_settings("summarize")


def test_openai_provider_defaults(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert llm.get_settings("extract") == ("openai", "gpt-4o-mini") and llm.get_settings("write") == ("openai", "gpt-4o-mini")


# ---- rate limits ----

class RateLimitError(Exception):  # llm classifies SDK errors by class name, like the groq/openai ones
    def __init__(self, message, headers=None):
        super().__init__(f"Error code: 429 - {message}")
        self.body = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
        self.response = type("R", (), {"headers": headers or {}})()


TPD = ("Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` on tokens per day "
       "(TPD): Limit 200000, Used 199100, Requested 3400. Please try again in 8m34.56s. Need more tokens?")
TPM = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Used 7000, "
       "Requested 3000. Please try again in 3.5s.")


@pytest.mark.parametrize("text, seconds", [
    ("Please try again in 8m34.56s. Need more", 514.56),
    ("try again in 1h2m3s.", 3723),
    ("try again in 250ms.", 0.25),
    ("try again in 2.5s", 2.5),
    ("try again in 12m", 720),
    ("no hint here", None),
])
def test_parse_wait_seconds(text, seconds):
    got = llm.parse_wait_seconds(text)
    assert got == pytest.approx(seconds) if seconds is not None else got is None


def test_retry_after_header_is_the_fallback():
    assert llm.parse_wait_seconds("no hint", {"retry-after": "42"}) == 42
    assert llm.parse_wait_seconds("try again in 5s", {"retry-after": "42"}) == 5  # the message wins


def test_format_wait():
    assert llm.format_wait(514.56).startswith("8m 35s (around ")
    assert llm.format_wait(3723).startswith("1h 2m 3s (around ")
    assert llm.format_wait(2).startswith("2s (around ")
    assert "unknown" in llm.format_wait(None)


def test_daily_limit_is_not_retried_and_says_how_long_to_wait(monkeypatch):
    attempts = []

    def call(model, s, u, effort=None, **_):
        attempts.append(model)
        raise RateLimitError(TPD)

    monkeypatch.setitem(llm._PROVIDERS, "groq", call)
    monkeypatch.setattr(llm, "_sleep", lambda s: pytest.fail("must not wait/retry on a daily limit"))
    with pytest.raises(llm.QuotaExceededError) as err:
        llm.generate("s", "u", task="extract")
    msg = str(err.value)
    assert attempts == ["openai/gpt-oss-20b"]  # exactly one attempt
    assert isinstance(err.value, llm.FatalLLMError)
    assert "daily limit reached on openai/gpt-oss-20b" in msg and "try again in 8m 35s" in msg
    assert "Limit 200000, Used 199100" in msg and ".cache/extract" in msg and "EXTRACT_MODEL" in msg


@pytest.mark.parametrize("message", [
    "Rate limit reached ... on requests per day (RPD): Limit 1000, Used 1000. Please try again in 20m0s.",
    "You exceeded your current quota, please check your plan and billing details. insufficient_quota",
])
def test_other_daily_and_quota_limits_are_also_fatal(monkeypatch, message):
    attempts = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: attempts.append(1) or (_ for _ in ()).throw(RateLimitError(message)))
    with pytest.raises(llm.QuotaExceededError):
        llm.generate("s", "u")
    assert len(attempts) == 1


def test_per_minute_limit_retries_and_honours_the_wait_it_is_given(monkeypatch):
    sleeps = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: (_ for _ in ()).throw(RateLimitError(TPM)))
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    with pytest.raises(llm.LLMError) as err:
        llm.generate("s", "u")
    assert not isinstance(err.value, llm.FatalLLMError) and "after 3 attempts" in str(err.value)
    assert sleeps == [4.0, 4.0]  # 3.5s asked + 0.5s margin, never less than the normal 2s/4s backoff


def test_retry_wait_is_capped(monkeypatch):
    sleeps = []
    slow = TPM.replace("3.5s", "5m")
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: (_ for _ in ()).throw(RateLimitError(slow)))
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    with pytest.raises(llm.LLMError):
        llm.generate("s", "u")
    assert sleeps == [llm.MAX_RETRY_AFTER] * 2


def test_bad_key_and_missing_key_are_fatal(monkeypatch):
    class AuthenticationError(Exception):
        pass

    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: (_ for _ in ()).throw(AuthenticationError("401 invalid api key")))
    with pytest.raises(llm.FatalLLMError, match="rejected the API key"):
        llm.generate("s", "u")

    monkeypatch.undo()
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.FatalLLMError, match="GROQ_API_KEY is not set"):
        llm._call_groq("openai/gpt-oss-120b", "s", "u")


# ---- reasoning effort ----

def test_default_effort_is_low_for_extraction_and_medium_for_writing_on_groq_gpt_oss():
    assert llm.get_reasoning_effort("extract", "groq", "openai/gpt-oss-20b") == "low"
    assert llm.get_reasoning_effort("write", "groq", "openai/gpt-oss-120b") == "medium"


def test_effort_env_overrides_blank_means_default_and_junk_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "HIGH ")
    monkeypatch.setenv("WRITE_REASONING_EFFORT", "low")
    assert llm.get_reasoning_effort("extract", "groq", "openai/gpt-oss-20b") == "high"
    assert llm.get_reasoning_effort("write", "groq", "openai/gpt-oss-120b") == "low"
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "")
    assert llm.get_reasoning_effort("extract", "groq", "openai/gpt-oss-20b") == "low"
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "extreme")
    with pytest.raises(llm.FatalLLMError, match="EXTRACT_REASONING_EFFORT must be one of low, medium, high"):
        llm.get_reasoning_effort("extract", "groq", "openai/gpt-oss-20b")


@pytest.mark.parametrize("provider, model", [
    ("openai", "gpt-4o-mini"), ("openai", "gpt-oss-20b"), ("groq", "llama-3.3-70b-versatile"), ("groq", "qwen/qwen3-32b"),
])
def test_effort_is_ignored_for_other_providers_and_models(monkeypatch, provider, model):
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "high")
    assert llm.get_reasoning_effort("extract", provider, model) is None


def test_generate_passes_the_tasks_effort_to_the_provider(monkeypatch):
    sent = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, **_: sent.append((model, effort)) or ("t", 1, 1))
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u", task="write")
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "medium")
    llm.generate("s", "u", task="extract")
    monkeypatch.setenv("EXTRACT_MODEL", "llama-3.3-70b-versatile")  # not a gpt-oss model: nothing is sent
    llm.generate("s", "u", task="extract")
    assert sent == [("openai/gpt-oss-20b", "low"), ("openai/gpt-oss-120b", "medium"),
                    ("openai/gpt-oss-20b", "medium"), ("llama-3.3-70b-versatile", None)]


def test_chat_sends_reasoning_effort_only_when_asked_to():
    from types import SimpleNamespace as NS

    seen = []

    def create(**kw):
        seen.append(kw)
        return NS(choices=[NS(finish_reason="stop", message=NS(content="ok"))], usage=NS(prompt_tokens=10, completion_tokens=20))

    client = NS(chat=NS(completions=NS(create=create)))
    llm._chat(client, "m", "s", "u", "low")
    llm._chat(client, "m", "s", "u")
    assert seen[0]["reasoning_effort"] == "low" and "reasoning_effort" not in seen[1]


# ---- reasoning tokens in the log ----

def test_reasoning_tokens_are_read_from_usage_and_logged_separately(monkeypatch):
    from types import SimpleNamespace as NS

    def fake_resp(reasoning):
        usage = NS(prompt_tokens=100, completion_tokens=900, **({"completion_tokens_details": NS(reasoning_tokens=reasoning)} if reasoning is not None else {}))
        return NS(choices=[NS(finish_reason="stop", message=NS(content="ok"))], usage=usage)

    client = NS(chat=NS(completions=NS(create=lambda **kw: fake_resp(700))))
    assert llm._chat(client, "m", "s", "u") == ("ok", 100, 900, 700)
    client = NS(chat=NS(completions=NS(create=lambda **kw: fake_resp(None))))
    assert llm._chat(client, "m", "s", "u")[3] is None  # provider didn't say

    results = iter([("t", 1000, 3000, 2500), ("t", 1000, 3000, 2000), ("t", 500, 800, 100)])
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, **_: next(results))
    llm.reset_usage()
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u", task="write")
    line = llm.summary_line()
    assert "out=6800 tok (of which reasoning=4600)" in line
    assert "openai/gpt-oss-20b 2 calls in=2000 out=6000 (reasoning=4500, effort=low)" in line
    assert "openai/gpt-oss-120b 1 calls in=500 out=800 (reasoning=100, effort=medium)" in line


def test_no_reasoning_note_when_the_provider_does_not_return_it(monkeypatch):
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, **_: ("t", 10, 20))
    monkeypatch.setenv("EXTRACT_MODEL", "llama-3.3-70b-versatile")
    llm.reset_usage()
    llm.generate("s", "u", task="extract")
    line = llm.summary_line()
    assert "reasoning" not in line and "effort" not in line and "llama-3.3-70b-versatile 1 calls in=10 out=20" in line


# ---- sampling: the writer is deterministic (temperature 0 + seed) ----

def test_writer_runs_at_temperature_0_with_a_seed_and_extraction_keeps_its_temperature(monkeypatch):
    sent = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, sampling=None: sent.append(sampling) or ("t", 1, 1))
    llm.generate("s", "u", task="write")
    llm.generate("s", "u", task="extract")
    assert sent[0]["temperature"] == 0 and isinstance(sent[0]["seed"], int)
    assert sent[1] == {"temperature": 0.2}


def test_chat_sends_temperature_and_seed():
    from types import SimpleNamespace as NS

    seen = []

    def create(**kw):
        seen.append(kw)
        return NS(choices=[NS(finish_reason="stop", message=NS(content="ok"))], usage=NS(prompt_tokens=1, completion_tokens=1))

    client = NS(chat=NS(completions=NS(create=create)))
    llm._chat(client, "m", "s", "u", sampling={"temperature": 0.0, "seed": 7})
    llm._chat(client, "m", "s", "u")
    assert (seen[0]["temperature"], seen[0]["seed"]) == (0.0, 7)
    assert seen[1]["temperature"] == 0.2 and "seed" not in seen[1]


# ---- fallback to OpenAI when Groq hits a daily limit ----

@pytest.fixture
def fallback_env(monkeypatch):
    """Groq answers until `state['groq_daily_limit']` is True, then raises a TPD error; OpenAI always answers."""
    state = {"groq_daily_limit": False, "calls": []}

    def groq(model, s, u, effort=None, sampling=None):
        state["calls"].append(("groq", model, effort, sampling))
        if state["groq_daily_limit"]:
            raise RateLimitError(TPD)
        return "groq text", 100, 50

    def openai(model, s, u, effort=None, sampling=None):
        state["calls"].append(("openai", model, effort, sampling))
        return "openai text", 1_000_000, 100_000

    monkeypatch.setitem(llm._PROVIDERS, "groq", groq)
    monkeypatch.setitem(llm._PROVIDERS, "openai", openai)
    monkeypatch.setenv("FALLBACK_PROVIDER", "openai")
    monkeypatch.setenv("FALLBACK_EXTRACT_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("FALLBACK_WRITE_MODEL", "gpt-5.4-mini")
    llm.reset_usage()
    return state


def test_a_groq_daily_limit_switches_that_task_to_the_fallback_for_the_rest_of_the_run(fallback_env, capsys):
    assert llm.generate("s", "u", task="extract") == "groq text"
    fallback_env["groq_daily_limit"] = True
    assert llm.generate("s", "u", task="extract") == "openai text"  # the failed call is retried on the fallback
    assert llm.generate("s", "u", task="extract") == "openai text"  # sticky: Groq isn't tried again
    assert [c[0] for c in fallback_env["calls"]] == ["groq", "groq", "openai", "openai"]
    assert llm.get_settings("extract") == ("openai", "gpt-5.6-luna") and llm.primary_settings("extract")[0] == "groq"
    assert llm.fallback_tasks() == ["extract"]
    err = capsys.readouterr().err
    assert "FALLBACK: groq/openai/gpt-oss-20b hit its daily limit; extract calls now go to openai/gpt-5.6-luna" in err
    assert "[llm] extract via openai/gpt-5.6-luna [FALLBACK] effort=low" in err and "[llm] extract via groq/openai/gpt-oss-20b effort=low" in err

    fallback_env["groq_daily_limit"] = False  # the writer has its own Groq model and limit: it is not switched
    assert llm.generate("s", "u", task="write") == "groq text"
    assert llm.models_used() == {"extract": ["groq/openai/gpt-oss-20b", "openai/gpt-5.6-luna"], "write": ["groq/openai/gpt-oss-120b"]}
    assert "FALLBACK used for extract" in llm.summary_line()

    llm.reset_fallback()  # a new run starts on Groq again
    assert llm.get_settings("extract") == ("groq", "openai/gpt-oss-20b")


def test_openai_reasoning_models_get_effort_and_no_temperature(fallback_env):
    fallback_env["groq_daily_limit"] = True
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u", task="write")
    openai_calls = [c for c in fallback_env["calls"] if c[0] == "openai"]
    assert openai_calls == [("openai", "gpt-5.6-luna", "low", {}), ("openai", "gpt-5.4-mini", "medium", {})]
    assert llm.get_reasoning_effort("write", "openai", "o3-mini") == "medium"
    assert llm.get_reasoning_effort("write", "openai", "gpt-4o-mini") is None  # not a reasoning model
    assert llm.sampling_for("write", "openai", "gpt-4o-mini") == llm.SAMPLING["write"]


def test_no_fallback_configured_or_other_errors_do_not_switch(fallback_env, monkeypatch):
    monkeypatch.delenv("FALLBACK_PROVIDER")
    fallback_env["groq_daily_limit"] = True
    with pytest.raises(llm.QuotaExceededError):
        llm.generate("s", "u", task="extract")
    monkeypatch.setenv("FALLBACK_PROVIDER", "openai")

    class AuthenticationError(Exception):
        pass

    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: (_ for _ in ()).throw(AuthenticationError("401")))
    with pytest.raises(llm.FatalLLMError, match="rejected the API key"):
        llm.generate("s", "u", task="extract")  # a bad key is not a daily limit
    assert llm.fallback_tasks() == []


def test_openai_spend_is_tracked_and_priced(fallback_env):
    fallback_env["groq_daily_limit"] = True
    llm.generate("s", "u", task="extract")  # 1M in x $0.20 + 100k out x $1.20 = $0.32
    llm.generate("s", "u", task="write")  # 1M in x $0.75 + 100k out x $4.50 = $1.20
    spend = llm.load_spend()
    assert spend["openai_usd"] == pytest.approx(1.52) and spend["calls"] == 2 and spend["input_tokens"] == 2_000_000
    assert llm.usage_totals()["cost"] == pytest.approx(1.52) and llm.usage_totals()["fallback_calls"] == 2


def test_the_fallback_stops_at_the_budget_with_a_clear_message(fallback_env, monkeypatch):
    monkeypatch.setenv("OPENAI_BUDGET_USD", "1.00")
    fallback_env["groq_daily_limit"] = True
    llm.generate("s", "u", task="write")  # $1.20: over budget after this call
    with pytest.raises(llm.BudgetExceededError, match=r"\$1.20 spent of OPENAI_BUDGET_USD=\$1.00"):
        llm.generate("s", "u", task="write")
    assert sum(c[0] == "openai" for c in fallback_env["calls"]) == 1  # refused before calling OpenAI again
    with pytest.raises(llm.BudgetExceededError):  # the other task can't switch to a spent fallback either
        llm.generate("s", "u", task="extract")
    assert issubclass(llm.BudgetExceededError, llm.QuotaExceededError)  # runs and evals stop the same way


def test_budget_default_and_bad_values(monkeypatch):
    assert llm.budget_usd() == 3.00
    monkeypatch.setenv("OPENAI_BUDGET_USD", "three")
    with pytest.raises(llm.FatalLLMError, match="OPENAI_BUDGET_USD"):
        llm.budget_usd()


def test_a_fallback_model_without_a_price_is_refused(fallback_env, monkeypatch):
    monkeypatch.setenv("FALLBACK_EXTRACT_MODEL", "gpt-9-unpriced")
    fallback_env["groq_daily_limit"] = True
    with pytest.raises(llm.FatalLLMError, match="no price"):
        llm.generate("s", "u", task="extract")
    assert not any(c[0] == "openai" for c in fallback_env["calls"])


def test_prices_for_the_fallback_models():
    assert llm.PRICES[("openai", "gpt-5.6-luna")] == (0.20, 1.20)
    assert llm.PRICES[("openai", "gpt-5.4-mini")] == (0.75, 4.50)
