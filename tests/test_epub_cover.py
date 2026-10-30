"""Tests for yazi/plugins/epub-cover.yazi/cover.py: the helper behind the EPUB preview
in `tgdctl browse`. Run as a subprocess, exactly as the Lua plugin does."""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

COVER_PY = Path(__file__).parent.parent / "yazi" / "plugins" / "epub-cover.yazi" / "cover.py"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"
CONTAINER_XML = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
)


def _make_epub(path, *, metadata="", manifest=""):
    opf = f'<package xmlns="http://www.idpf.org/2007/opf"><metadata>{metadata}</metadata><manifest>{manifest}</manifest></package>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/img/c.png", PNG_BYTES)
    return path


def _run(epub, cache_home):
    return subprocess.run(
        [sys.executable, "-I", str(COVER_PY), str(epub)],
        capture_output=True, text=True, env={"XDG_CACHE_HOME": str(cache_home)},
    )


@pytest.mark.parametrize("metadata,manifest", [
    ("", '<item id="x" href="img/c.png" media-type="image/png" properties="cover-image"/>'),  # EPUB3
    ('<meta name="cover" content="ci"/>', '<item id="ci" href="img/c.png" media-type="image/png"/>'),  # EPUB2
    ("", '<item id="cover-img" href="img/c.png" media-type="image/png"/>'),  # name heuristic
])
def test_extracts_cover(tmp_path, metadata, manifest):
    epub = _make_epub(tmp_path / "b.epub", metadata=metadata, manifest=manifest)
    result = _run(epub, tmp_path / "cache")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).read_bytes() == PNG_BYTES


def test_second_call_hits_cache(tmp_path):
    epub = _make_epub(tmp_path / "b.epub", manifest='<item id="x" href="img/c.png" media-type="image/png" properties="cover-image"/>')
    first = _run(epub, tmp_path / "cache").stdout
    second = _run(epub, tmp_path / "cache").stdout
    assert first == second


def test_no_cover_exits_1(tmp_path):
    epub = _make_epub(tmp_path / "b.epub")
    result = _run(epub, tmp_path / "cache")
    assert result.returncode == 1
    assert "no cover" in result.stderr


def test_not_a_zip_exits_2(tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_text("junk")
    result = _run(bad, tmp_path / "cache")
    assert result.returncode == 2
    assert "cannot read EPUB cover" in result.stderr


def test_dtd_entities_rejected(tmp_path):
    epub = tmp_path / "evil.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", '<!DOCTYPE p [<!ENTITY a "aaaa">]><package xmlns="http://www.idpf.org/2007/opf">&a;</package>')
    result = _run(epub, tmp_path / "cache")
    assert result.returncode == 2
    assert "DTD" in result.stderr
