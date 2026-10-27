import pytest

import facts
import helpers
import llm
import main


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never touch the real extraction cache, and start each test with no fake pages."""
    monkeypatch.setattr(facts, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(llm, "SPEND_PATH", str(tmp_path / "spend.json"))  # never the real OpenAI spend record
    llm.reset_fallback()
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)  # tests must not read the developer's real .env
    for var in ("LLM_PROVIDER", "LLM_MODEL", "EXTRACT_MODEL", "WRITE_MODEL", "FALLBACK_PROVIDER", "FALLBACK_EXTRACT_MODEL",
                "FALLBACK_WRITE_MODEL", "OPENAI_BUDGET_USD", "EXTRACT_REASONING_EFFORT", "WRITE_REASONING_EFFORT", "EXTRACT_CONCURRENCY",
                "MAX_BRIEFS_PER_DAY", "PRIVATE_BRIEFS", "REQUIRE_ACCESS_CODE", "APP_ACCESS_CODE",
                "INTERNAL_API_KEY", "REQUIRE_INTERNAL_KEY"):
        monkeypatch.delenv(var, raising=False)
    helpers.TEXTS.clear()
