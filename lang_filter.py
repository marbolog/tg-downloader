"""Post-download language detection.

Extracts a text sample from a downloaded file and returns the detected
ISO 639-1 language code. Only PDF and EPUB are supported; all other
formats return None.

Two-stage detection:
  1. Text extraction + langdetect (confidence >= _CONFIDENCE).
  2. Filename heuristic — German umlauts or German-specific month names —
     used only when no text could be extracted (image-based / scanned files).
"""

import logging
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import fitz
from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0  # make detection deterministic

log = logging.getLogger(__name__)

DISCARD_LANG = "de"
_CONFIDENCE = 0.90
_PDF_PAGES = 4
_EPUB_CHAPTERS = 3
_MIN_CHARS = 300

# German-specific month names that differ from English equivalents.
# April/August/September/November are excluded — identical in English.
_GERMAN_MONTHS = {
    "januar", "februar", "märz", "mai", "juni",
    "juli", "oktober", "dezember",
}
_GERMAN_UMLAUTS = frozenset("äöüßÄÖÜ")


def detect_language(file_path: Path, ext: str) -> str | None:
    """Returns ISO 639-1 language code, or None if undetermined.

    Stage 1: text extraction + langdetect (confidence >= 0.90).
    Stage 2: filename heuristic (German umlauts / month names) — only when
    text extraction yields nothing (image-based / scanned PDFs).
    Formats other than pdf and epub always return None.
    """
    lang = _detect_from_text(file_path, ext)
    if lang is not None:
        log.debug(f"{file_path.name}: detected '{lang}' via text extraction")
        return lang

    if ext in ("pdf", "epub") and _filename_is_german(file_path.name):
        log.debug(f"{file_path.name}: detected 'de' via filename heuristic")
        return "de"

    return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _detect_from_text(file_path: Path, ext: str) -> str | None:
    try:
        if ext == "pdf":
            text = _pdf_text(file_path)
        elif ext == "epub":
            text = _epub_text(file_path)
        else:
            return None

        if len(text.strip()) < _MIN_CHARS:
            log.debug(f"{file_path.name}: too little text ({len(text.strip())} chars) — skipping detection")
            return None

        results = detect_langs(text)
        if not results:
            return None

        top = results[0]
        log.debug(f"{file_path.name}: lang candidates {results}")
        return top.lang if top.prob >= _CONFIDENCE else None

    except LangDetectException:
        return None
    except Exception as exc:
        log.warning(f"{file_path.name}: language detection error: {exc}")
        return None


def _filename_is_german(filename: str) -> bool:
    if any(c in _GERMAN_UMLAUTS for c in filename):
        return True
    words = set(re.findall(r"[A-Za-zäöüÄÖÜß]+", filename.lower()))
    return bool(words & _GERMAN_MONTHS)


def _pdf_text(file_path: Path) -> str:
    doc = fitz.open(str(file_path))
    pages = min(_PDF_PAGES, doc.page_count)
    return " ".join(doc[i].get_text() for i in range(pages))


def _epub_text(file_path: Path) -> str:
    with zipfile.ZipFile(file_path) as zf:
        html_names = sorted(
            n for n in zf.namelist()
            if n.lower().endswith((".html", ".xhtml", ".htm"))
            and "toc" not in n.lower()
            and "nav" not in n.lower()
        )
        texts = []
        for name in html_names[:_EPUB_CHAPTERS]:
            try:
                raw = zf.read(name).decode("utf-8", errors="ignore")
                texts.append(_strip_html(raw))
            except Exception:
                continue
        return " ".join(texts)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def text(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    return extractor.text()
