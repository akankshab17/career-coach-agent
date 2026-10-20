import pytest

import facts
import helpers


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never touch the real extraction cache, and start each test with no fake pages."""
    monkeypatch.setattr(facts, "CACHE_DIR", str(tmp_path / "cache"))
    helpers.TEXTS.clear()
