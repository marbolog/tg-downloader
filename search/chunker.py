# search/chunker.py
"""Text extraction and chunking for PDF and EPUB files.

Returns one chunk per PDF page; one chunk per EPUB chapter content file.
Each chunk dict: {chunk_idx, page, chapter, text}
page is 1-based for PDF, None for EPUB.
chapter is None for PDF, heading text for EPUB.
"""
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import fitz

SUPPORTED_EXTS = {"pdf", "epub"}
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|nbsp|quot|apos);")
_ENTITY_MAP = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"', "&apos;": "'"}


def chunk_file(path: Path, ext: str) -> list[dict]:
    if ext == "pdf":
        return _chunk_pdf(path)
    if ext == "epub":
        return _chunk_epub(path)
    return []


def _chunk_pdf(path: Path) -> list[dict]:
    try:
        doc = fitz.open(str(path))
    except Exception:
        return []
    chunks = []
    for page_num in range(doc.page_count):
        text = doc[page_num].get_text().strip()
        if not text:
            continue
        chunks.append({
            "chunk_idx": len(chunks),
            "page": page_num + 1,
            "chapter": None,
            "text": text,
        })
    return chunks


def _chunk_epub(path: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as zf:
            opf_path = _find_opf(zf)
            if not opf_path:
                return []
            spine_hrefs = _parse_spine(zf, opf_path)
            chunks = []
            for href in spine_hrefs:
                try:
                    html = zf.read(href).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                text = _html_to_text(html)
                if not text.strip():
                    continue
                heading = _extract_heading(html)
                chunks.append({
                    "chunk_idx": len(chunks),
                    "page": None,
                    "chapter": heading,
                    "text": text.strip(),
                })
            return chunks
    except Exception:
        return []


def _find_opf(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _parse_spine(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    try:
        root = ET.fromstring(zf.read(opf_path))
    except Exception:
        return []
    id_to_href: dict[str, str] = {}
    for item in root.findall(".//{http://www.idpf.org/2007/opf}item"):
        item_id = item.get("id", "")
        href = item.get("href", "")
        mt = item.get("media-type", "")
        if "xhtml" in mt or "html" in mt:
            id_to_href[item_id] = base + href
    hrefs = []
    for itemref in root.findall(".//{http://www.idpf.org/2007/opf}itemref"):
        idref = itemref.get("idref", "")
        if idref in id_to_href:
            hrefs.append(id_to_href[idref])
    return hrefs


def _extract_heading(html: str) -> str | None:
    m = _HEADING_RE.search(html)
    if m:
        return _TAG_RE.sub("", m.group(1)).strip() or None
    return None


def _html_to_text(html: str) -> str:
    # Strip tags, decode basic entities
    text = _TAG_RE.sub(" ", html)
    text = _ENTITY_RE.sub(lambda m: _ENTITY_MAP.get(m.group(0), m.group(0)), text)
    # Collapse whitespace
    return re.sub(r"\s+", " ", text).strip()
