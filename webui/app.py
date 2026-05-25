import io
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "/app/data/tg_downloader.db"))
THUMBS_DIR = Path(os.environ.get("THUMBS_DIR", "/app/data/thumbs"))

app = FastAPI()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/languages")
def list_languages():
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT COALESCE(m.language, '__unknown__') AS language, COUNT(*) AS count
            FROM media_messages m
            JOIN channels c ON m.channel_id = c.id
            WHERE m.status = 'downloaded'
            GROUP BY m.language
            HAVING count > 0
            ORDER BY count DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/files")
def list_files(page: int = 1, per_page: int = 60, channel: str = "", language: str = "", hide_dupes: bool = True):
    offset = (page - 1) * per_page
    conn = _db()
    try:
        params: list = []
        where = "m.status = 'downloaded'"
        if channel:
            where += " AND c.identifier = ?"
            params.append(channel)
        if language == "__unknown__":
            where += " AND m.language IS NULL"
        elif language:
            where += " AND m.language = ?"
            params.append(language)

        rows = conn.execute(
            f"""
            SELECT m.id, m.filename, m.size, m.ext, m.date, m.downloaded_at,
                   m.local_path, m.language, m.file_hash,
                   c.title AS channel_title, c.identifier AS channel_identifier
            FROM media_messages m
            JOIN channels c ON m.channel_id = c.id
            WHERE {where}
            ORDER BY m.downloaded_at DESC NULLS LAST, m.date DESC
            """,
            params,
        ).fetchall()
        all_items = [dict(r) for r in rows]

        if hide_dupes:
            all_items = _deduplicate_with_counts(all_items)
        else:
            for item in all_items:
                item["copy_count"] = 1

        total = len(all_items)
        return {"total": total, "page": page, "per_page": per_page, "items": all_items[offset:offset + per_page]}
    finally:
        conn.close()


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
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT c.identifier, c.title,
                   SUM(CASE WHEN m.status='downloaded' THEN 1 ELSE 0 END) AS count
            FROM channels c
            LEFT JOIN media_messages m ON m.channel_id = c.id
            GROUP BY c.id
            HAVING count > 0
            ORDER BY c.title
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/thumb/{file_id}")
def get_thumb(file_id: int):
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMBS_DIR / f"{file_id}.jpg"

    if not thumb_path.exists():
        conn = _db()
        try:
            row = conn.execute(
                "SELECT local_path, ext FROM media_messages WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            conn.close()

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
    deleted_files = 0
    conn = _db()
    try:
        for file_id in req.ids:
            row = conn.execute(
                "SELECT local_path FROM media_messages WHERE id = ?", (file_id,)
            ).fetchone()
            if row and row["local_path"]:
                p = Path(row["local_path"])
                if p.exists():
                    p.unlink()
                    deleted_files += 1
            conn.execute(
                "UPDATE media_messages SET status='discarded', local_path=NULL WHERE id=?",
                (file_id,),
            )
            thumb = THUMBS_DIR / f"{file_id}.jpg"
            if thumb.exists():
                thumb.unlink()
        conn.commit()
    finally:
        conn.close()

    log.info(f"Discarded {deleted_files}/{len(req.ids)} files via web UI")
    return {"deleted": deleted_files, "total": len(req.ids)}


@app.get("/api/download/{file_id}")
def download_file(file_id: int):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT local_path, filename FROM media_messages WHERE id = ?", (file_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["local_path"]:
        raise HTTPException(status_code=404)

    path = Path(row["local_path"])
    if not path.exists():
        raise HTTPException(status_code=404)

    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row["filename"]}"'},
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
