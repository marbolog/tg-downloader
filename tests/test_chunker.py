import zipfile
from pathlib import Path
import fitz
import pytest
from rag.chunker import chunk_file, _split_text, SUPPORTED_EXTS


def _make_pdf(tmp_path: Path, pages: list[str]) -> Path:
    path = tmp_path / "test.pdf"
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 72), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(tmp_path: Path, chapters: list[tuple[str, str]]) -> Path:
    path = tmp_path / "test.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        items_xml = "\n".join(
            f'<item id="ch{i}" href="ch{i}.html" media-type="application/xhtml+xml"/>'
            for i in range(len(chapters))
        )
        itemrefs_xml = "\n".join(
            f'<itemref idref="ch{i}"/>' for i in range(len(chapters))
        )
        opf = (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf">'
            f"<manifest>{items_xml}</manifest>"
            f"<spine>{itemrefs_xml}</spine>"
            "</package>"
        )
        zf.writestr("content.opf", opf)
        for i, (title, text) in enumerate(chapters):
            html = f"<html><body><h1>{title}</h1><p>{text}</p></body></html>"
            zf.writestr(f"ch{i}.html", html)
    return path


def test_unsupported_format_returns_empty(tmp_path):
    p = tmp_path / "file.mobi"
    p.write_bytes(b"fake mobi")
    assert chunk_file(p, "mobi") == []


def test_missing_file_returns_empty(tmp_path):
    assert chunk_file(tmp_path / "nonexistent.pdf", "pdf") == []


def test_pdf_basic_chunks(tmp_path):
    text = "First paragraph.\n\nSecond paragraph with more words to reach the minimum."
    pdf = _make_pdf(tmp_path, [text])
    chunks = chunk_file(pdf, "pdf")
    assert len(chunks) >= 1
    for c in chunks:
        assert "text" in c and len(c["text"]) > 0
        assert c["page"] == 1
        assert c["chapter"] is None
        assert isinstance(c["chunk_idx"], int)


def test_pdf_page_numbers_are_sequential(tmp_path):
    pdf = _make_pdf(tmp_path, ["Page one text here " * 5, "Page two text here " * 5])
    chunks = chunk_file(pdf, "pdf")
    pages = [c["page"] for c in chunks]
    assert 1 in pages
    assert 2 in pages


def test_epub_basic_chunks(tmp_path):
    epub = _make_epub(tmp_path, [
        ("Chapter 1", "The quick brown fox jumped over the lazy dog. " * 15),
        ("Chapter 2", "To be or not to be that is the question here. " * 15),
    ])
    chunks = chunk_file(epub, "epub")
    assert len(chunks) >= 2
    for c in chunks:
        assert c["page"] is None
        assert c["chapter"] is not None


def test_chunk_idx_is_sequential(tmp_path):
    epub = _make_epub(tmp_path, [
        ("Ch 1", "Content " * 40),
        ("Ch 2", "Content " * 40),
    ])
    chunks = chunk_file(epub, "epub")
    for expected_idx, chunk in enumerate(chunks):
        assert chunk["chunk_idx"] == expected_idx


def test_split_text_short_returns_single():
    pieces = _split_text("Short text under the limit.")
    assert len(pieces) == 1


def test_split_text_long_splits_with_overlap():
    text = ("word " * 120 + "\n\n") * 2
    pieces = _split_text(text)
    assert len(pieces) >= 2
    for p in pieces:
        assert len(p) >= 10


def test_supported_exts_constant():
    assert "pdf" in SUPPORTED_EXTS
    assert "epub" in SUPPORTED_EXTS
    assert "mobi" not in SUPPORTED_EXTS
