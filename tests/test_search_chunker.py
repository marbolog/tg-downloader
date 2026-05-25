import io
import zipfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from search.chunker import chunk_file, SUPPORTED_EXTS


def test_supported_exts():
    assert "pdf" in SUPPORTED_EXTS
    assert "epub" in SUPPORTED_EXTS
    assert "mobi" not in SUPPORTED_EXTS


def test_unsupported_format_returns_empty():
    chunks = chunk_file(Path("book.mobi"), "mobi")
    assert chunks == []


def test_pdf_chunks_have_required_keys():
    mock_doc = MagicMock()
    mock_doc.page_count = 2
    mock_page0 = MagicMock()
    mock_page0.get_text.return_value = "Page one content here."
    mock_page1 = MagicMock()
    mock_page1.get_text.return_value = "Page two content here."
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page0, mock_page1]))
    mock_doc.__getitem__ = MagicMock(side_effect=[mock_page0, mock_page1])

    with patch("search.chunker.fitz.open", return_value=mock_doc):
        chunks = chunk_file(Path("book.pdf"), "pdf")

    assert len(chunks) == 2
    for i, chunk in enumerate(chunks):
        assert "chunk_idx" in chunk
        assert "page" in chunk
        assert "chapter" in chunk
        assert "text" in chunk
        assert chunk["chapter"] is None
        assert chunk["page"] == i + 1


def test_pdf_skips_empty_pages():
    mock_doc = MagicMock()
    mock_doc.page_count = 2
    mock_page0 = MagicMock()
    mock_page0.get_text.return_value = "   \n  "
    mock_page1 = MagicMock()
    mock_page1.get_text.return_value = "Real content."
    mock_doc.__getitem__ = MagicMock(side_effect=[mock_page0, mock_page1])

    with patch("search.chunker.fitz.open", return_value=mock_doc):
        chunks = chunk_file(Path("book.pdf"), "pdf")

    assert len(chunks) == 1
    assert chunks[0]["text"] == "Real content."


def test_epub_chunks_have_required_keys(tmp_path):
    # Build a minimal EPUB in memory
    epub_path = tmp_path / "test.epub"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/content.opf", """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf">
  <manifest>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>""")
        zf.writestr("OEBPS/ch1.xhtml", "<html><body><h1>Chapter 1</h1><p>First chapter text.</p></body></html>")
        zf.writestr("OEBPS/ch2.xhtml", "<html><body><h1>Chapter 2</h1><p>Second chapter text.</p></body></html>")
    epub_path.write_bytes(buf.getvalue())

    chunks = chunk_file(epub_path, "epub")
    assert len(chunks) == 2
    for chunk in chunks:
        assert "chunk_idx" in chunk
        assert "page" in chunk
        assert "chapter" in chunk
        assert "text" in chunk
        assert chunk["page"] is None
    assert "Chapter 1" in chunks[0]["chapter"] or "First chapter" in chunks[0]["text"]
