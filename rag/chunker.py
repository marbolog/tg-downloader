"""Split PDF and EPUB files into overlapping text chunks with source metadata."""

import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import fitz  # PyMuPDF -- already a project dependency

SUPPORTED_EXTS = {"pdf", "epub"}
_CHUNK_SIZE = 800    # target characters per chunk
_OVERLAP = 100       # characters of overlap between consecutive chunks
_MIN_CHUNK = 100     # short paragraph fragments below this length are merged/skipped


def chunk_file(file_path: Path, ext: str) -> list[dict]:
    """Return chunks for pdf/epub, or [] for unsupported formats or errors.

    Each chunk: {text: str, page: int|None, chapter: str|None, chunk_idx: int}
    """
    if ext not in SUPPORTED_EXTS or not file_path.exists():
        return []
    try:
        if ext == "pdf":
            return _chunk_pdf(file_path)
        if ext == "epub":
            return _chunk_epub(file_path)
    except Exception:
        return []
    return []


# -- PDF -----------------------------------------------------------------------

def _chunk_pdf(path: Path) -> list[dict]:
    doc = fitz.open(str(path))
    raw: list[dict] = []
    for page_num in range(doc.page_count):
        text = doc[page_num].get_text()
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para:
                raw.append({"text": para, "page": page_num + 1, "chapter": None})
    return _merge_and_split(raw)


# -- EPUB ----------------------------------------------------------------------

def _chunk_epub(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        opf_name = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
        opf_dir = "/".join(opf_name.split("/")[:-1]) if opf_name else ""

        spine_items = _parse_opf_spine(zf, opf_name) if opf_name else []

        if spine_items:
            html_files = []
            label_map: dict[str, str] = {}
            for item in spine_items:
                href = item["href"]
                full = f"{opf_dir}/{href}".lstrip("/") if opf_dir else href
                if full in zf.namelist():
                    html_files.append(full)
                    label_map[full] = item["label"]
        else:
            html_files = sorted(
                n for n in zf.namelist()
                if n.lower().endswith((".html", ".xhtml", ".htm"))
            )
            label_map = {}

        raw: list[dict] = []
        for html_path in html_files:
            chapter = label_map.get(html_path) or html_path.split("/")[-1].rsplit(".", 1)[0]
            try:
                text = _strip_html(zf.read(html_path).decode("utf-8", errors="ignore"))
            except Exception:
                continue
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if para:
                    raw.append({"text": para, "page": None, "chapter": chapter})

        return _merge_and_split(raw)


def _parse_opf_spine(zf: zipfile.ZipFile, opf_name: str) -> list[dict]:
    """Return [{"href": ..., "label": ...}, ...] in spine order."""
    try:
        content = zf.read(opf_name).decode("utf-8", errors="ignore")
        root = ET.fromstring(content)
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {
            item.get("id", ""): item.get("href", "")
            for item in root.findall(".//opf:item", ns)
        }
        spine = []
        for itemref in root.findall(".//opf:itemref", ns):
            idref = itemref.get("idref", "")
            if idref in manifest:
                spine.append({"href": manifest[idref], "label": idref})
        return spine
    except Exception:
        return []


# -- Chunking helpers ----------------------------------------------------------

def _merge_and_split(raw: list[dict]) -> list[dict]:
    """Group paragraphs by source location, join them, split into sized chunks."""
    groups: list[tuple[tuple, list[str]]] = []
    for item in raw:
        key = (item["page"], item["chapter"])
        if groups and groups[-1][0] == key:
            groups[-1][1].append(item["text"])
        else:
            groups.append((key, [item["text"]]))

    result: list[dict] = []
    chunk_idx = 0
    for (page, chapter), texts in groups:
        for piece in _split_text("\n\n".join(texts)):
            result.append({
                "text": piece,
                "page": page,
                "chapter": chapter,
                "chunk_idx": chunk_idx,
            })
            chunk_idx += 1
    return result


def _split_text(text: str) -> list[str]:
    """Split text into _CHUNK_SIZE-character pieces with _OVERLAP overlap.

    Returns [text] for any non-empty text up to _CHUNK_SIZE chars.
    Returns [] only for empty/whitespace-only text.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= _CHUNK_SIZE:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _CHUNK_SIZE, len(text))
        if end < len(text):
            bp = text.rfind("\n\n", start + 50, end)
            if bp == -1:
                bp = text.rfind(" ", start + 50, end)
            if bp != -1:
                end = bp

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end - _OVERLAP if end < len(text) else end

    return chunks


# -- HTML stripping ------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self._parts.append(s)

    def text(self) -> str:
        return "\n".join(self._parts)


def _strip_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.text()
