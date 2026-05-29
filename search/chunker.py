# search/chunker.py
"""Text extraction and chunking for PDF and EPUB files.

Returns one chunk per PDF page; one chunk per EPUB chapter content file.
Each chunk dict: {chunk_idx, page, chapter, text}
page is 1-based for PDF, None for EPUB.
chapter is None for PDF, heading text for EPUB.
"""
import html.parser as _html_parser
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import fitz

# Silence MuPDF's C-layer stderr spew (e.g. "format error: non-page object in
# page tree") on malformed PDFs — these bypass Python try/except. We already
# handle the failures per page; the raw library noise is not actionable.
fitz.TOOLS.mupdf_display_errors(False)

SUPPORTED_EXTS = {"pdf", "epub"}
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


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
    try:
        chunks = []
        for page_num in range(doc.page_count):
            # Extract per page in isolation: malformed PDFs (e.g. "malformed page
            # tree") raise on a single page. Skip the bad page rather than abort
            # the whole document, so partial text is still indexed and a fully
            # broken file returns [] (caller marks it processed, no endless retry).
            try:
                text = doc[page_num].get_text().strip()
            except Exception:
                continue
            if not text:
                continue
            chunks.append({
                "chunk_idx": len(chunks),
                "page": page_num + 1,
                "chapter": None,
                "text": text,
            })
        return chunks
    finally:
        doc.close()


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
    all_names = set(zf.namelist())
    id_to_href: dict[str, str] = {}
    for item in root.findall(".//{http://www.idpf.org/2007/opf}item"):
        item_id = item.get("id", "")
        href = item.get("href", "")
        mt = item.get("media-type", "")
        if "xhtml" in mt or "html" in mt:
            resolved = base + href
            if resolved in all_names:
                id_to_href[item_id] = resolved
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


class _TextExtractor(_html_parser.HTMLParser):
    _SKIP = {"script", "style"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.get_text()
