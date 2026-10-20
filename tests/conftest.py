import pytest

import facts
import helpers
import main


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never touch the real extraction cache, and start each test with no fake pages."""
    monkeypatch.setattr(facts, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)  # tests must not read the developer's real .env
    for var in ("LLM_PROVIDER", "LLM_MODEL", "EXTRACT_MODEL", "WRITE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    helpers.TEXTS.clear()
