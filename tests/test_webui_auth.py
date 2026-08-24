"""Tests for the web UI's optional HTTP Basic Auth (WEBUI_PASSWORD env var).

Auth must be off when WEBUI_PASSWORD is unset (trusted-LAN default) and must
gate every route — API and static alike — when set.
"""
import base64
import importlib
import sys
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def _client(tmp_path, monkeypatch, password: str | None = None):
    monkeypatch.chdir(WEBUI_DIR)  # so StaticFiles("static") resolves
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("THUMBS_DIR", str(tmp_path / "thumbs"))
    if password is None:
        monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("WEBUI_PASSWORD", password)
    if str(WEBUI_DIR) not in sys.path:
        sys.path.insert(0, str(WEBUI_DIR))
    sys.modules.pop("app", None)
    import app as webui_app
    importlib.reload(webui_app)
    from starlette.testclient import TestClient
    return TestClient(webui_app.app)


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_no_password_configured_leaves_ui_open(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/channels").status_code == 200


def test_password_set_rejects_unauthenticated_with_challenge(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    r = client.get("/api/channels")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_password_set_gates_static_pages_too(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    assert client.get("/").status_code == 401


def test_correct_credentials_accepted(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    r = client.get("/api/channels", headers=_basic("tg", "s3cret"))
    assert r.status_code == 200


def test_wrong_password_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    assert client.get("/api/channels", headers=_basic("tg", "wrong")).status_code == 401


def test_wrong_username_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    assert client.get("/api/channels", headers=_basic("admin", "s3cret")).status_code == 401


def test_malformed_authorization_header_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, password="s3cret")
    r = client.get("/api/channels", headers={"Authorization": "Basic not-base64!!"})
    assert r.status_code == 401


def test_discard_endpoint_requires_auth(tmp_path, monkeypatch):
    """The destructive endpoint is the reason auth exists — pin it explicitly."""
    client = _client(tmp_path, monkeypatch, password="s3cret")
    assert client.post("/api/discard", json={"ids": [1]}).status_code == 401
