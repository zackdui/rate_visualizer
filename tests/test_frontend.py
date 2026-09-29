"""Frontend: the front/back boundary, and the real `site` command serving the page against the fixture site.duckdb."""
import os, pathlib, re, socket, subprocess, sys, time
import pytest
import requests
from test_site import site  # noqa: F401  (module fixture: builds a small site.duckdb from the real fixtures)

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "rate_visualizer"


def test_frontend_never_touches_the_database():
    for path in (SRC / "frontend").rglob("*.py"):
        text = path.read_text()
        assert "import duckdb" not in text, f"{path} imports duckdb"
        assert not re.search(r"\bSELECT\b|\bFROM rates\b", text), f"{path} contains SQL"


def test_backend_never_imports_ui_code():
    for path in (SRC / "backend").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"^\s*(from|import)\s+\S*(nicegui|frontend)", text, re.M), f"{path} imports UI code"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_site_command_serves_page_and_health(site, tmp_path):  # noqa: F811
    port = _free_port()
    # a clean environment: NiceGUI switches into its own test mode when it sees pytest's variables
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST") and not k.startswith("NICEGUI")}
    env.update(SITE_DB_PATH=site["path"], DUCKDB_THREADS="1")
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        env.pop(k, None)
    exe = pathlib.Path(sys.executable).with_name("rate-visualizer")
    log = open(tmp_path / "site.log", "w")
    proc = subprocess.Popen([str(exe), "site", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT,
                            cwd=str(SRC.parent.parent))
    try:
        status, body = None, {}
        for _ in range(120):
            try:
                r = requests.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
                status, body = r.status_code, r.json()
                if status == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        assert status == 200 and body["ok"] and body["snapshot_date"] == "2026-01-01", (tmp_path / "site.log").read_text()
        page = requests.get(f"http://127.0.0.1:{port}/", timeout=10)
        assert page.status_code == 200 and "BCBSTX Behavioral Health Rates" in page.text
        for anchor in ("code-summary", "benchmarks", "rate-explorer", "percentage-rates", "map",
                       "rate-type-composition", "provider-profile", "billing-entity-profile", "data-quality"):
            assert anchor in page.text
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        log.close()
