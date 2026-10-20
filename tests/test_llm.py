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
    assert "provider=openai model=custom-model calls=3 in=3000" in llm.summary_line()


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
