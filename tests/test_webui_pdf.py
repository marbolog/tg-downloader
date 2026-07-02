"""Tests for the web UI's inline PDF endpoint (GET /api/pdf/{id}).

webui/app.py reads its config from the environment at import time and mounts
StaticFiles("static") relative to the cwd, so we point both at temp/webui before
importing the module.
"""
import importlib
import sys
from pathlib import Path

import pytest

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(WEBUI_DIR)                       # so StaticFiles("static") resolves
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("THUMBS_DIR", str(tmp_path / "thumbs"))
    if str(WEBUI_DIR) not in sys.path:
        sys.path.insert(0, str(WEBUI_DIR))
    sys.modules.pop("app", None)
    import app as webui_app
    importlib.reload(webui_app)
    from starlette.testclient import TestClient
    return webui_app, TestClient(webui_app.app)


def _make_pdf(path: Path) -> None:
    import fitz
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def _insert(db, *, ext: str, local_path: str, filename: str) -> int:
    with db._conn() as conn:
        ch = conn.execute(
            "INSERT INTO channels (telegram_id, identifier, title) VALUES (1, '@c', 'C')"
        ).lastrowid
    mid = db.save_media_message(
        channel_id=ch, message_id=1, filename=filename, size=1,
        mime_type="application/octet-stream", ext=ext,
        date="2026-01-01T00:00:00", caption="",
    )
    db.mark_downloaded(mid, local_path)
    return mid


def test_view_pdf_serves_inline(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf)
    webui_app, client = _client(tmp_path, monkeypatch)
    mid = _insert(webui_app.db, ext="pdf", local_path=str(pdf), filename="doc.pdf")

    r = client.get(f"/api/pdf/{mid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "inline" in r.headers["content-disposition"]
    assert r.content[:5] == b"%PDF-"


def test_view_pdf_supports_range_requests(tmp_path, monkeypatch):
    """pdf.js streams large files via HTTP Range; the endpoint must honor it."""
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf)
    webui_app, client = _client(tmp_path, monkeypatch)
    mid = _insert(webui_app.db, ext="pdf", local_path=str(pdf), filename="doc.pdf")

    r = client.get(f"/api/pdf/{mid}", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.content == b"%PDF"


def test_view_pdf_unknown_id_404(tmp_path, monkeypatch):
    webui_app, client = _client(tmp_path, monkeypatch)
    assert client.get("/api/pdf/9999").status_code == 404


def test_view_pdf_rejects_non_pdf(tmp_path, monkeypatch):
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"not really an epub")
    webui_app, client = _client(tmp_path, monkeypatch)
    mid = _insert(webui_app.db, ext="epub", local_path=str(epub), filename="book.epub")
    assert client.get(f"/api/pdf/{mid}").status_code == 404


def test_view_pdf_missing_file_404(tmp_path, monkeypatch):
    webui_app, client = _client(tmp_path, monkeypatch)
    mid = _insert(webui_app.db, ext="pdf", local_path=str(tmp_path / "gone.pdf"), filename="gone.pdf")
    assert client.get(f"/api/pdf/{mid}").status_code == 404


def test_view_pdf_filename_with_special_chars_is_safely_encoded(tmp_path, monkeypatch):
    """Filenames come from Telegram message metadata, not from us — a quote or
    CR/LF in one must not break out of the Content-Disposition header (HTTP
    response splitting / arbitrary header injection). FileResponse's `filename=`
    param percent-encodes via RFC 5987 instead of the old manual f-string, which
    embedded the raw filename straight into the header value."""
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf)
    webui_app, client = _client(tmp_path, monkeypatch)
    malicious = 'evil".pdf\r\nX-Injected: 1'
    mid = _insert(webui_app.db, ext="pdf", local_path=str(pdf), filename=malicious)

    r = client.get(f"/api/pdf/{mid}")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd and '"' not in cd
    assert "X-Injected" not in r.headers
    assert cd.startswith("inline; filename*=utf-8''")


def test_download_filename_with_special_chars_is_safely_encoded(tmp_path, monkeypatch):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf)
    webui_app, client = _client(tmp_path, monkeypatch)
    malicious = 'evil".pdf\r\nX-Injected: 1'
    mid = _insert(webui_app.db, ext="pdf", local_path=str(pdf), filename=malicious)

    r = client.get(f"/api/download/{mid}")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd and '"' not in cd
    assert "X-Injected" not in r.headers
    assert cd.startswith("attachment; filename*=utf-8''")
