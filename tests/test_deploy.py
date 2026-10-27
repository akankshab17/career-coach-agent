"""Production settings: the access code is mandatory, briefs are private, a daily cap on new briefs, and the image files
keep secrets and the API out of reach."""
import os
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import api
import httpx
from jobs import JobStore
from test_api import REQ, T0, Clock, fake_runner

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = JobStore(str(tmp_path / "jobs.db"), now=clock)
    app = api.create_app(store=store, runner=fake_runner(), start_worker=False)
    with TestClient(app) as client:
        yield {"client": client, "store": store, "worker": app.state.worker, "clock": clock}


def post(env, **kw):
    return env["client"].post("/briefs", json={**REQ, **kw})


# ---- the access code is mandatory in production ----

@pytest.mark.parametrize("code, ok", [(None, False), ("short", False), ("x" * 15, False), ("x" * 16, True)])
def test_the_server_refuses_to_start_without_a_long_enough_code_when_required(monkeypatch, code, ok):
    monkeypatch.setattr(api, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_app", lambda: "app")  # no database, no worker thread
    monkeypatch.setenv("REQUIRE_ACCESS_CODE", "1")
    if code is not None:
        monkeypatch.setenv("APP_ACCESS_CODE", code)
    if ok:
        assert api.serve() == "app"
    else:
        with pytest.raises(RuntimeError, match="APP_ACCESS_CODE"):
            api.serve()


@pytest.mark.parametrize("key, ok", [(None, False), ("x" * 31, False), ("x" * 32, True)])
def test_the_server_refuses_to_start_without_a_long_enough_internal_key_when_required(monkeypatch, key, ok):
    monkeypatch.setattr(api, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_app", lambda: "app")
    monkeypatch.setenv("REQUIRE_INTERNAL_KEY", "1")
    if key is not None:
        monkeypatch.setenv("INTERNAL_API_KEY", key)
    if ok:
        assert api.serve() == "app"
    else:
        with pytest.raises(RuntimeError, match="INTERNAL_API_KEY"):
            api.serve()


def test_without_the_requirement_a_missing_code_is_allowed_for_local_use(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_app", lambda: "app")
    assert api.serve() == "app"


# ---- a daily cap on new briefs ----

def test_new_briefs_are_capped_per_utc_day_with_a_clear_message(env, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "2")
    env["clock"].t = T0.replace(hour=21, minute=30)
    assert post(env, company="A").status_code == 202
    assert post(env, company="B").status_code == 202
    r = post(env, company="C")
    assert r.status_code == 429
    assert r.json()["detail"].startswith("Daily limit reached: 2 new briefs have been started today (limit 2).")
    assert "in 2h 30m" in r.json()["detail"]
    assert env["store"].count("queued") == 2

    assert post(env, company="A").status_code == 202 and post(env, company="A").json()["reused"] is True  # reuse is free
    env["clock"].t = T0.replace(hour=0, minute=1) + timedelta(days=1)  # a new UTC day
    assert post(env, company="C").status_code == 202


def test_failed_briefs_count_too_and_zero_means_no_limit(env, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "1")
    env["worker"].runner = fake_runner(fail_with=RuntimeError("boom"))
    post(env, company="A")
    env["worker"].process_next()  # it failed, but it used searches and tokens
    assert post(env, company="B").status_code == 429
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "0")
    assert post(env, company="B").status_code == 202


def test_the_default_is_5_and_a_bad_value_fails_at_startup(tmp_path, monkeypatch):
    assert api.daily_limit() == 5  # Tavily's free plan: 1,000 credits a month
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "thirty")
    with pytest.raises(ValueError, match="MAX_BRIEFS_PER_DAY"):
        api.create_app(db_path=str(tmp_path / "jobs.db"), start_worker=False)


# ---- private briefs ----

def test_private_mode_turns_off_the_shared_list(env, monkeypatch):
    job_id = post(env).json()["job_id"]
    monkeypatch.setenv("PRIVATE_BRIEFS", "1")
    assert env["client"].get("/health").json()["private_briefs"] is True
    r = env["client"].get("/briefs")
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    assert env["client"].get(f"/briefs/{job_id}").status_code == 200  # a brief's own (unguessable) id still opens it


def test_the_app_lists_only_this_sessions_briefs_in_private_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_BRIEFS", "1")
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    app = api.create_app(store=JobStore(str(tmp_path / "jobs.db")), runner=fake_runner(), start_worker=True)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx, "request", lambda method, url, headers=None, timeout=None, **kw:
                            client.request(method, url.removeprefix("http://api.test"), headers=headers, **kw))
        client.post("/briefs", json={**REQ, "company": "SomeoneElse"})  # another coach's brief
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
        assert not at.exception and not at.sidebar.warning
        assert at.sidebar.header[0].value == "Your briefs"
        at.text_input[0].input("Acme")
        at.text_input[1].input("Software Engineer")
        at.button[0].click()
        at = at.run(timeout=20)
        labels = [b.label for b in at.sidebar.button]
        assert any(l.startswith("Acme · Software Engineer") for l in labels)
        assert not any("SomeoneElse" in l for l in labels)


def test_the_app_shows_the_daily_limit_message_as_is(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "1")
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    app = api.create_app(store=JobStore(str(tmp_path / "jobs.db")), runner=fake_runner(), start_worker=False)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx, "request", lambda method, url, headers=None, timeout=None, **kw:
                            client.request(method, url.removeprefix("http://api.test"), headers=headers, **kw))
        client.post("/briefs", json={**REQ, "company": "First"})
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
        at.text_input[0].input("Acme")
        at.text_input[1].input("Software Engineer")
        at.button[0].click()
        at = at.run(timeout=20)
        assert any(e.value.startswith("Daily limit reached") for e in at.error)


# ---- the image files ----

def test_the_image_keeps_secrets_out_and_production_settings_on():
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    for entry in (".env", ".cache/", "evals/", "tests/", ".venv/", "*.debug.md"):
        assert entry in ignore, entry
    docker = (ROOT / "Dockerfile").read_text()
    for setting in ("REQUIRE_ACCESS_CODE=1", "PRIVATE_BRIEFS=1", "MAX_BRIEFS_PER_DAY=5", "USER user", "--uid 1000"):
        assert setting in docker, setting
    assert ".env" not in [line.split()[1] for line in docker.splitlines() if line.startswith("COPY")]
    start = (ROOT / "start.sh").read_text()
    assert "--host 127.0.0.1 --port 8000" in start and "--server.address 0.0.0.0" in start  # only Streamlit is reachable
    assert 'PORT="${PORT:-7860}"' in start and 'STARTUP_WAIT="${STARTUP_WAIT:-180}"' in start  # Render's 0.1 CPU starts slowly


def test_the_render_blueprint_is_free_has_a_health_check_and_holds_no_secret_values():
    import yaml

    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    (service,) = blueprint["services"]
    assert (service["type"], service["runtime"], service["plan"]) == ("web", "docker", "free")
    assert service["healthCheckPath"] == "/_stcore/health" and service["dockerfilePath"] == "./Dockerfile"
    env = {e["key"]: e for e in service["envVars"]}
    for secret in ("APP_ACCESS_CODE", "TAVILY_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        assert env[secret] == {"key": secret, "sync": False}, secret  # asked for in Render's dashboard, never stored here
    assert env["PORT"]["value"] == "10000" and env["FALLBACK_PROVIDER"]["value"] == "openai"
    assert all("value" not in e or not str(e["value"]).startswith(("gsk_", "tvly-", "sk-")) for e in service["envVars"])
    assert not (ROOT / "README.md").read_text().startswith("---")  # the Hugging Face header is gone


# ---- the container can write its .cache ----

def test_the_dockerfile_hands_the_app_directory_and_cache_to_the_user_before_switching():
    """Local run failed: PermissionError on '.cache' because WORKDIR created /home/user/app owned by root."""
    lines = [l.strip() for l in (ROOT / "Dockerfile").read_text().splitlines()]
    instructions = " ".join(lines).replace("\\ ", " ")  # join continued lines
    user_at = next(i for i, l in enumerate(lines) if l == "USER user")
    before_user = " ".join(lines[:user_at])
    assert "mkdir -p /home/user/app/.cache" in before_user and "chown -R user:user /home/user/app" in before_user
    assert "WORKDIR /home/user/app" in instructions
    assert instructions.index("chown -R user:user /home/user/app") < instructions.index("WORKDIR /home/user/app")
    # every file the app writes lives under WORKDIR/.cache (read from the source: conftest points these at temp dirs)
    for module, name in (("facts", "CACHE_DIR"), ("jobs", "DB_PATH"), ("llm", "SPEND_PATH")):
        source = (ROOT / "src" / f"{module}.py").read_text()
        assert f'{name} = os.path.join(".cache", ' in source, (module, name)


def _docker_ready() -> bool:
    import shutil
    import subprocess

    if os.getenv("RUN_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="set RUN_DOCKER_TESTS=1 with Docker running to build the image (slow)")
def test_the_image_runs_as_uid_1000_and_can_write_its_cache():
    import subprocess

    tag = "interview-brief-test"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(ROOT)], check=True, capture_output=True, timeout=900)
    probe = ("import os, sys; sys.path.insert(0, 'src'); from jobs import JobStore; JobStore(); "
             "open('.cache/extract-probe', 'w').write('x'); print(os.getuid(), sorted(os.listdir('.cache')))")
    out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", probe],
                         check=True, capture_output=True, text=True, timeout=120).stdout
    assert out.startswith("1000 ") and "jobs.db" in out and "extract-probe" in out
    refused = subprocess.run(["docker", "run", "--rm", tag], capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0 and "APP_ACCESS_CODE" in refused.stdout + refused.stderr


def test_the_streamlit_toolbar_hides_deploy_and_developer_menu():
    config = (ROOT / ".streamlit" / "config.toml").read_text()
    assert 'toolbarMode = "viewer"' in config
    assert "COPY --chown=user .streamlit/ .streamlit/" in (ROOT / "Dockerfile").read_text()  # the image needs it too
