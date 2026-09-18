"""Adapter turning search/chunker.py's flat text chunks into a reader-ready
Document: a list of displayable sections plus a table of contents.

chunk_file() gives per-page text for PDF and per-chapter text for EPUB, in
document order -- already exactly what a reader needs to display. The one
thing it doesn't carry is a real table of contents for PDF (chunks only have
page numbers, not titles): PyMuPDF can read a PDF's own embedded bookmarks
via get_toc(); when present, that's mapped onto section indices. When absent
(common for scans), or for EPUB (whose chapters already carry titles from
chunk_file itself), the TOC is synthesized 1:1 from the sections -- so
Document.toc is never empty for a document that parsed successfully, and
callers never need two code paths for "has bookmarks" vs. "doesn't."
"""
from dataclasses import dataclass
from pathlib import Path

import fitz

from search.chunker import chunk_file


@dataclass
class Section:
    title: str
    text: str


@dataclass
class TocEntry:
    title: str
    section_index: int


@dataclass
class Document:
    sections: list[Section]
    toc: list[TocEntry]


def build_document(path: Path, ext: str) -> Document | None:
    """Build a reader Document from an existing PDF/EPUB file's extracted text.

    Returns None when there is no extractable text (image-only/scanned PDF,
    unparseable file) or ext isn't pdf/epub. Callers are expected to check
    path.exists() themselves first -- this only reports whether text came out.
    """
    if ext not in ("pdf", "epub"):
        return None
    chunks = chunk_file(path, ext)
    if not chunks:
        return None

    if ext == "pdf":
        sections = [Section(title=f"Page {c['page']}", text=c["text"]) for c in chunks]
        toc = _pdf_toc(path, chunks) or _default_toc(sections)
    else:
        sections = [
            Section(title=c["chapter"] or f"Section {i + 1}", text=c["text"])
            for i, c in enumerate(chunks)
        ]
        toc = _default_toc(sections)

    return Document(sections=sections, toc=toc)


def _default_toc(sections: list[Section]) -> list[TocEntry]:
    return [TocEntry(title=s.title, section_index=i) for i, s in enumerate(sections)]


def _pdf_toc(path: Path, chunks: list[dict]) -> list[TocEntry]:
    """Map PyMuPDF's outline (real embedded bookmarks) to section indices,
    using the nearest chunked page at or after each bookmark's target page (a
    bookmark can point at a page whose text extraction was skipped, e.g. a
    blank divider). Bookmarks past the last chunked page are dropped."""
    try:
        doc = fitz.open(str(path))
        outline = doc.get_toc()
        doc.close()
    except Exception:
        return []
    if not outline:
        return []

    pages = [c["page"] for c in chunks]
    entries = []
    for _level, title, target_page in outline:
        idx = _nearest_page_index(pages, target_page)
        if idx is not None:
            entries.append(TocEntry(title=title, section_index=idx))
    return entries


def _nearest_page_index(pages: list[int], target_page: int) -> int | None:
    for i, page in enumerate(pages):
        if page >= target_page:
            return i
    return None
