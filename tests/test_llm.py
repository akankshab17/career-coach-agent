import pytest

import llm


def _fake_provider(name, calls):
    def fn(model, system_prompt, user_prompt):
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
    assert "provider=openai calls=3 in=3000 tok out=1500 tok" in line
    assert "by model: openai/gpt-oss-120b 1 calls in=1000 out=500; gpt-4o-mini 1 calls in=1000 out=500; custom-model 1 calls in=1000 out=500" in line


def test_retries_then_clear_error(monkeypatch):
    class RateLimitError(Exception):
        pass

    attempts = []

    def flaky(model, s, u):
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
    text, _, _ = llm._chat(client, "m", "s", "u")
    assert llm.truncation_count() == before + 1
    assert text.startswith("## TL;DR")
    assert "was cut off" in capsys.readouterr().err


# ---- model routing ----

def test_extract_and_write_use_different_models_by_default(monkeypatch):
    used = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u: used.append(model) or ("text", 100, 50))
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

    def call(model, s, u):
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
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u: attempts.append(1) or (_ for _ in ()).throw(RateLimitError(message)))
    with pytest.raises(llm.QuotaExceededError):
        llm.generate("s", "u")
    assert len(attempts) == 1


def test_per_minute_limit_retries_and_honours_the_wait_it_is_given(monkeypatch):
    sleeps = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u: (_ for _ in ()).throw(RateLimitError(TPM)))
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    with pytest.raises(llm.LLMError) as err:
        llm.generate("s", "u")
    assert not isinstance(err.value, llm.FatalLLMError) and "after 3 attempts" in str(err.value)
    assert sleeps == [4.0, 4.0]  # 3.5s asked + 0.5s margin, never less than the normal 2s/4s backoff


def test_retry_wait_is_capped(monkeypatch):
    sleeps = []
    slow = TPM.replace("3.5s", "5m")
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u: (_ for _ in ()).throw(RateLimitError(slow)))
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    with pytest.raises(llm.LLMError):
        llm.generate("s", "u")
    assert sleeps == [llm.MAX_RETRY_AFTER] * 2


def test_bad_key_and_missing_key_are_fatal(monkeypatch):
    class AuthenticationError(Exception):
        pass

    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u: (_ for _ in ()).throw(AuthenticationError("401 invalid api key")))
    with pytest.raises(llm.FatalLLMError, match="rejected the API key"):
        llm.generate("s", "u")

    monkeypatch.undo()
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm.FatalLLMError, match="GROQ_API_KEY is not set"):
        llm._call_groq("openai/gpt-oss-120b", "s", "u")
