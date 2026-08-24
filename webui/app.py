import base64
import io
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rich.logging import RichHandler

from db import Database

# Match the main app's logging style (rich, message-focused) so listener and
# web UI logs read the same when viewed side by side via `docker compose logs`.
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(show_path=False)],
)
log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "/app/data/tg_downloader.db"))
THUMBS_DIR = Path(os.environ.get("THUMBS_DIR", "/app/data/thumbs"))
SEARCH_TOP_K = int(os.environ.get("SEARCH_TOP_K", "8"))
# HTTP Basic Auth, enabled by setting WEBUI_PASSWORD. Basic (not a bearer token)
# because the frontend loads thumbnails via <img src> and PDFs via pdf.js Range
# requests — neither can attach a custom header, but the browser attaches cached
# Basic credentials to all same-origin requests automatically, so the vanilla-JS
# frontend needs no changes. Unset = no auth (trusted-LAN default).
WEBUI_USERNAME = os.environ.get("WEBUI_USERNAME", "tg")
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")

app = FastAPI()
db = Database(str(DB_PATH))


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if not WEBUI_PASSWORD:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            username, _, password = base64.b64decode(header[6:]).decode().partition(":")
            # compare_digest resists timing attacks; check both fields unconditionally.
            user_ok = secrets.compare_digest(username, WEBUI_USERNAME)
            pass_ok = secrets.compare_digest(password, WEBUI_PASSWORD)
            if user_ok and pass_ok:
                return await call_next(request)
        except Exception:
            pass  # malformed base64/utf-8 → fall through to 401
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="tg-downloader"'},
    )


@app.get("/api/languages")
def list_languages():
    return db.language_counts()


@app.get("/api/files")
def list_files(page: int = 1, per_page: int = 60, channel: str = "", language: str = "", hide_dupes: bool = True, ids: str = ""):
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()] if ids else None
    all_items = db.list_downloaded_files(channel=channel, language=language, ids=id_list)

    if hide_dupes:
        all_items = _deduplicate_with_counts(all_items)
    else:
        for item in all_items:
            item["copy_count"] = 1

    offset = (page - 1) * per_page
    total = len(all_items)
    return {"total": total, "page": page, "per_page": per_page, "items": all_items[offset:offset + per_page]}


def _deduplicate_with_counts(items: list[dict]) -> list[dict]:
    """Keep the first-downloaded copy per unique hash (or filename+size fallback).

    Items arrive sorted newest-first; we scan once to find group members, then
    filter to keep only the representative (lowest id = oldest download) while
    annotating each with copy_count.
    """
    groups: dict[str, list[int]] = {}
    id_to_key: dict[int, str] = {}
    for item in items:
        # Prefer hash when available; fall back to filename|size for unhashed files.
        key = item.get("file_hash") or f"\x00{item['filename']}\x00{item['size']}"
        groups.setdefault(key, []).append(item["id"])
        id_to_key[item["id"]] = key

    rep_ids = {min(ids) for ids in groups.values()}
    key_counts = {k: len(v) for k, v in groups.items()}

    result = []
    for item in items:
        if item["id"] in rep_ids:
            item["copy_count"] = key_counts[id_to_key[item["id"]]]
            result.append(item)
    return result


@app.get("/api/channels")
def list_channels():
    return db.channel_counts()


@app.get("/api/thumb/{file_id}")
def get_thumb(file_id: int):
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMBS_DIR / f"{file_id}.jpg"

    if not thumb_path.exists():
        row = db.get_media(file_id)
        if not row or not row["local_path"]:
            raise HTTPException(status_code=404)

        img_bytes = _generate_thumb(Path(row["local_path"]), (row["ext"] or "").lower())
        if img_bytes is None:
            raise HTTPException(status_code=404)
        thumb_path.write_bytes(img_bytes)

    return FileResponse(thumb_path, media_type="image/jpeg")


def _generate_thumb(file_path: Path, ext: str) -> bytes | None:
    if not file_path.exists():
        log.warning(f"File not found: {file_path}")
        return None
    try:
        if ext == "pdf":
            import fitz
            fitz.TOOLS.mupdf_display_errors(False)  # suppress C-layer stderr noise
            doc = fitz.open(str(file_path))
            if not doc.page_count:
                return None
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.6, 0.6))
            return pix.tobytes("jpeg")

        if ext == "epub":
            import zipfile
            with zipfile.ZipFile(file_path) as zf:
                for name in zf.namelist():
                    n = name.lower()
                    if "cover" in n and n.endswith((".jpg", ".jpeg", ".png")):
                        return _resize_bytes(zf.read(name))
            return None

        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            return _resize_bytes(file_path.read_bytes())

    except Exception as exc:
        log.warning(f"Thumb generation failed for {file_path}: {exc}")

    return None


def _resize_bytes(data: bytes) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((300, 420))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class DiscardRequest(BaseModel):
    ids: list[int]


@app.post("/api/discard")
def discard_files(req: DiscardRequest):
    if not req.ids:
        return {"deleted": 0, "total": 0}

    # Read local_path before mark_discarded_many clears it in the DB.
    local_paths = {row["id"]: row["local_path"] for row in db.list_downloaded_files(ids=req.ids)}

    try:
        # One transaction for the whole batch -- see mark_discarded_many's
        # docstring for why per-id transactions caused intermittent 500s here.
        db.mark_discarded_many(req.ids)
    except sqlite3.OperationalError as exc:
        log.error(f"Discard failed for {len(req.ids)} files: {exc}")
        raise HTTPException(status_code=503, detail="Database busy, please try again") from exc

    deleted_files = 0
    for file_id in req.ids:
        local_path = local_paths.get(file_id)
        if local_path:
            p = Path(local_path)
            if p.exists():
                p.unlink()
                deleted_files += 1
        thumb = THUMBS_DIR / f"{file_id}.jpg"
        if thumb.exists():
            thumb.unlink()

    log.info(f"Discarded {deleted_files}/{len(req.ids)} files via web UI")
    return {"deleted": deleted_files, "total": len(req.ids)}


@app.get("/api/download/{file_id}")
def download_file(file_id: int):
    row = db.get_media(file_id)
    if not row or not row["local_path"]:
        raise HTTPException(status_code=404)

    path = Path(row["local_path"])
    if not path.exists():
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row["filename"],
    )


@app.get("/api/pdf/{file_id}")
def view_pdf(file_id: int):
    """Serve a PDF **inline** for the in-browser reader (reader.js / pdf.js).

    Unlike /api/download (which forces a save), this sets Content-Disposition:
    inline so the browser renders it. FileResponse honors HTTP Range requests, so
    pdf.js streams large magazines progressively instead of fetching the whole
    file. PDF-only by design — the reader is never opened on other formats."""
    row = db.get_media(file_id)
    if not row or not row["local_path"] or (row["ext"] or "").lower() != "pdf":
        raise HTTPException(status_code=404)

    path = Path(row["local_path"])
    if not path.exists():
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["filename"],
        content_disposition_type="inline",
    )


@app.get("/api/search")
def fts_search(q: str, channel: str = "", top_k: int = 0):
    if not q.strip():
        return {"chunks": []}
    try:
        chunks = db.search_fts_query(q, top_k=top_k or SEARCH_TOP_K, channel_identifier=channel)
        return {"chunks": chunks}
    except Exception as exc:
        log.warning(f"FTS5 search error for {q!r}: {exc}")
        return {"chunks": [], "error": str(exc)}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
