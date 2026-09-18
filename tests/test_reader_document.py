import io
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from reader_document import build_document, Section, TocEntry, Document


def _mock_pdf_chunks():
    return [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "Page one text."},
        {"chunk_idx": 1, "page": 2, "chapter": None, "text": "Page two text."},
        {"chunk_idx": 2, "page": 3, "chapter": None, "text": "Page three text."},
    ]


def test_pdf_without_outline_falls_back_to_page_titles():
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = []
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [s.title for s in doc.sections] == ["Page 1", "Page 2", "Page 3"]
    assert doc.sections[0].text == "Page one text."
    # No real bookmarks -> TOC is synthesized 1:1 from the sections.
    assert [e.title for e in doc.toc] == ["Page 1", "Page 2", "Page 3"]
    assert [e.section_index for e in doc.toc] == [0, 1, 2]


def test_pdf_with_outline_maps_bookmarks_to_sections():
    mock_doc = MagicMock()
    # (level, title, page) -- PyMuPDF's get_toc() format, 1-based pages.
    mock_doc.get_toc.return_value = [
        [1, "Introduction", 1],
        [1, "Chapter One", 2],
    ]
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.title for e in doc.toc] == ["Introduction", "Chapter One"]
    assert [e.section_index for e in doc.toc] == [0, 1]
    # Sections themselves always stay page-granular, titled by page number,
    # regardless of the bookmark titles -- the TOC is a separate index into them.
    assert [s.title for s in doc.sections] == ["Page 1", "Page 2", "Page 3"]


def test_pdf_bookmark_targeting_skipped_page_uses_nearest_later_page():
    # Bookmark points at page 2, but page 2's text was skipped during chunking
    # (e.g. a blank divider) -- only pages 1 and 3 exist as chunks.
    chunks = [
        {"chunk_idx": 0, "page": 1, "chapter": None, "text": "Page one."},
        {"chunk_idx": 1, "page": 3, "chapter": None, "text": "Page three."},
    ]
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = [[1, "Chapter One", 2]]
    with patch("reader_document.chunk_file", return_value=chunks), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.section_index for e in doc.toc] == [1]  # section for page 3


def test_pdf_bookmark_targeting_page_past_the_end_falls_back_to_default_toc():
    # The one bookmark maps to nothing valid -- an empty TOC sidebar would be
    # useless, so this falls back to the same per-page TOC used when a PDF has
    # no outline at all, rather than leaving the reader with no navigation.
    mock_doc = MagicMock()
    mock_doc.get_toc.return_value = [[1, "Appendix", 99]]
    with patch("reader_document.chunk_file", return_value=_mock_pdf_chunks()), \
         patch("reader_document.fitz.open", return_value=mock_doc):
        doc = build_document(Path("book.pdf"), "pdf")

    assert doc is not None
    assert [e.title for e in doc.toc] == ["Page 1", "Page 2", "Page 3"]


def test_pdf_with_no_extractable_text_returns_none():
    with patch("reader_document.chunk_file", return_value=[]):
        doc = build_document(Path("scanned.pdf"), "pdf")
    assert doc is None


def test_epub_uses_chapter_titles_for_both_sections_and_toc():
    chunks = [
        {"chunk_idx": 0, "page": None, "chapter": "Chapter 1", "text": "First chapter."},
        {"chunk_idx": 1, "page": None, "chapter": None, "text": "Untitled section."},
    ]
    with patch("reader_document.chunk_file", return_value=chunks):
        doc = build_document(Path("book.epub"), "epub")

    assert doc is not None
    assert doc.sections[0].title == "Chapter 1"
    assert doc.sections[0].text == "First chapter."
    assert doc.sections[1].title == "Section 2"  # no chapter heading -> fallback title
    assert [e.title for e in doc.toc] == ["Chapter 1", "Section 2"]
    assert [e.section_index for e in doc.toc] == [0, 1]


def test_epub_with_no_extractable_text_returns_none():
    with patch("reader_document.chunk_file", return_value=[]):
        doc = build_document(Path("empty.epub"), "epub")
    assert doc is None


def test_unsupported_extension_returns_none():
    with patch("reader_document.chunk_file", return_value=[{"chunk_idx": 0, "page": None, "chapter": None, "text": "x"}]):
        doc = build_document(Path("book.mobi"), "mobi")
    assert doc is None
