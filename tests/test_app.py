"""The Streamlit app, driven by Streamlit's AppTest against the real API (fake pipeline, temp database, no network):
app.py's HTTP calls are routed into FastAPI's TestClient."""
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import api
from jobs import JobStore
from test_api import fake_runner

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The API with a worker thread and the fake pipeline; httpx.request in app.py goes to it."""
    monkeypatch.delenv("APP_ACCESS_CODE", raising=False)
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    app = api.create_app(store=JobStore(str(tmp_path / "jobs.db")), runner=fake_runner(), start_worker=True)
    with TestClient(app) as client:
        def route(method, url, headers=None, timeout=None, **kw):
            return client.request(method, url.removeprefix("http://api.test"), headers=headers, **kw)
        monkeypatch.setattr(httpx, "request", route)
        yield client


def build(at: AppTest, company="Acme", role="Software Engineer") -> AppTest:
    at.text_input[0].input(company)
    at.text_input[1].input(role)
    at.button[0].click()  # "Build brief" (the form's submit button)
    return at.run(timeout=20)


def test_the_form_builds_a_brief_and_offers_a_download(served):
    at = AppTest.from_file(APP, default_timeout=20).run()
    assert not at.exception
    assert [t.label for t in at.text_input] == ["Company", "Role", "Location"] and at.text_input[2].value == "United States"
    assert at.selectbox[0].value == "new-grad"
    at = build(at)
    assert not at.exception and not at.error
    page = "\n".join(m.value for m in at.markdown)
    assert "# Interview brief: Acme" in page and "[About](https://acme.com/about)" in page  # sources render as links
    assert at.get("download_button")  # the brief as markdown
    assert any("Acme · Software Engineer" in b.label for b in at.sidebar.button)  # it shows up under recent briefs


def test_company_and_role_are_required(served):
    at = AppTest.from_file(APP, default_timeout=20).run()
    at.button[0].click()
    at = at.run()
    assert any("required" in e.value for e in at.error)
    assert served.get("/briefs").json() == []


def test_the_app_asks_for_the_access_code_once(served, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_CODE", "s3cret")
    at = AppTest.from_file(APP, default_timeout=20).run()
    assert [t.label for t in at.text_input] == ["Access code"]  # nothing else until the code is right
    at.text_input[0].input("wrong")
    at.button[0].click()
    at = at.run()
    assert any("not accepted" in e.value for e in at.error)
    at.text_input[0].input("s3cret")
    at.button[0].click()
    at = at.run()
    assert [t.label for t in at.text_input] == ["Company", "Role", "Location"]
    at = build(at)
    assert "# Interview brief: Acme" in "\n".join(m.value for m in at.markdown)


def test_an_unreachable_api_is_reported(monkeypatch):
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    at = AppTest.from_file(APP, default_timeout=20).run()
    assert any("Can't reach the API" in e.value for e in at.error)
