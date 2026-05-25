"""Post-download content analysis: language detection and topic filtering.

Extracts a text sample from a downloaded file. Only PDF and EPUB are supported;
other formats return None / are skipped.

Language detection (two-stage):
  1. Text extraction + langdetect (confidence >= _CONFIDENCE).
  2. Filename heuristic — German umlauts or German-specific month names —
     used only when no text could be extracted (image-based / scanned files).

Topic filtering:
  Whole-word keyword search on extracted text. Returns the first topic whose
  keyword hit count reaches min_matches.

Combined analysis:
  analyze_file() opens each PDF/EPUB exactly once when topics are configured:
  text is split into (metadata, lang_body, topic_body). lang_body feeds
  langdetect; metadata + topic_body feeds topic matching. Use this in
  download_item() instead of calling detect_language() + detect_topic()
  separately, which would parse the file twice.
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
_PDF_PAGES = 4            # pages sampled for language detection only
_PDF_TOPIC_PAGES = 15     # pages sampled when topic detection is also needed
_EPUB_CHAPTERS = 3        # content files for language detection
_EPUB_TOPIC_CHAPTERS = 6  # content files for topic detection (includes TOC/nav)
_MIN_CHARS = 300

_GERMAN_MONTHS = {
    "januar", "februar", "märz", "mai", "juni",
    "juli", "oktober", "dezember",
}
_GERMAN_UMLAUTS = frozenset("äöüßÄÖÜ")

# Type alias for pre-compiled topic patterns.
# Each entry is (original_keyword_lowercase, compiled_regex).
CompiledPatterns = dict[str, list[tuple[str, re.Pattern]]]


def compile_topic_patterns(topic_keywords: dict[str, list[str]]) -> CompiledPatterns:
    """Pre-compile topic regex patterns. Call once before scanning many files."""
    return {
        topic: [
            (kw.lower(), re.compile(r"\b" + re.escape(kw.lower()) + r"\b"))
            for kw in keywords
        ]
        for topic, keywords in topic_keywords.items()
    }


def analyze_file(
    file_path: Path,
    ext: str,
    topic_keywords: dict[str, list[str]] | None = None,
    topic_min_matches: int = 2,
    topic_min_occurrences: int = 1,
    *,
    compiled_patterns: CompiledPatterns | None = None,
) -> tuple[str | None, str | None]:
    """Extract text once and return (language, matched_topic).

    More efficient than calling detect_language() + detect_topic() separately
    when both are needed — each of those calls extracts and parses the file.
    When topics are configured the extraction uses a larger page sample
    (_PDF_TOPIC_PAGES) and includes document metadata, which also gives
    langdetect more signal.
    """
    has_topics = bool(topic_keywords or compiled_patterns)

    if has_topics:
        # Single open of the file; split output so each detector gets the right slice.
        metadata, lang_text, topic_body = _extract_text_parts(file_path, ext)
        topic_text = (metadata + " " + topic_body).strip() if metadata else topic_body
    else:
        # Shallow path — no topic detection needed, so don't read deeper than necessary.
        lang_text = _extract_text(file_path, ext, topic_depth=False)
        topic_text = None

    lang = _run_lang_detection(file_path.name, lang_text)
    if lang is None and ext in ("pdf", "epub") and _filename_is_german(file_path.name):
        log.debug(f"{file_path.name}: detected 'de' via filename heuristic")
        lang = "de"
    elif lang is not None:
        log.debug(f"{file_path.name}: detected '{lang}' via text extraction")

    topic = None
    if has_topics:
        patterns = compiled_patterns or compile_topic_patterns(topic_keywords or {})
        topic = _run_topic_detection(file_path.name, topic_text, patterns, topic_min_matches, topic_min_occurrences)

    return lang, topic


def detect_language(file_path: Path, ext: str) -> str | None:
    """Return ISO 639-1 language code, or None if undetermined.

    Stage 1: text extraction + langdetect.
    Stage 2: filename heuristic (German umlauts / month names) — only when
    text extraction yields nothing (image-based / scanned PDFs).
    """
    text = _extract_text(file_path, ext, topic_depth=False)
    lang = _run_lang_detection(file_path.name, text)
    if lang is not None:
        log.debug(f"{file_path.name}: detected '{lang}' via text extraction")
        return lang

    if ext in ("pdf", "epub") and _filename_is_german(file_path.name):
        log.debug(f"{file_path.name}: detected 'de' via filename heuristic")
        return "de"

    return None


def detect_topic(
    file_path: Path,
    ext: str,
    topic_keywords: dict[str, list[str]],
    min_matches: int = 2,
    min_occurrences: int = 1,
    *,
    compiled_patterns: CompiledPatterns | None = None,
) -> str | None:
    """Return the first matched discard topic, or None.

    Uses whole-word matching to avoid false positives (e.g. 'car' in 'cardiac').
    min_occurrences controls how many times a keyword must appear to count —
    set > 1 to filter out incidental mentions.
    Pass pre-compiled patterns via compiled_patterns when scanning many files.
    """
    if not topic_keywords and not compiled_patterns:
        return None

    text = _extract_text(file_path, ext, topic_depth=True)
    if text is None or len(text.strip()) < _MIN_CHARS:
        return None

    patterns = compiled_patterns or compile_topic_patterns(topic_keywords)
    return _run_topic_detection(file_path.name, text, patterns, min_matches, min_occurrences)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_text_parts(file_path: Path, ext: str) -> tuple[str, str, str]:
    """Single-pass extraction used by analyze_file. Returns (metadata, lang_body, topic_body).

    Both bodies come from a single doc/zipfile open. lang_body uses the shallow
    page/chapter limits and excludes EPUB nav/TOC noise; topic_body uses the
    deeper limits and includes nav/TOC. Empty strings on error or unsupported format.
    """
    try:
        if ext == "pdf":
            return _pdf_text_parts(file_path)
        if ext == "epub":
            return _epub_text_parts(file_path)
    except Exception as exc:
        log.warning(f"{file_path.name}: text extraction error: {exc}")
    return "", "", ""


def _extract_text(file_path: Path, ext: str, *, topic_depth: bool) -> str | None:
    """Extract raw text from PDF or EPUB. Returns None for other formats or on error.

    When topic_depth=True, prepends document metadata (title, subject, keywords)
    to the body text. Metadata is excluded for language detection to avoid bias
    from bibliographic fields that may be in a different language than the body.
    """
    try:
        if ext == "pdf":
            pages = _PDF_TOPIC_PAGES if topic_depth else _PDF_PAGES
            return _pdf_text(file_path, pages, include_metadata=topic_depth)
        if ext == "epub":
            return _epub_text(file_path, topic_depth=topic_depth)
        return None
    except Exception as exc:
        log.warning(f"{file_path.name}: text extraction error: {exc}")
        return None


def _run_lang_detection(filename: str, text: str | None) -> str | None:
    """Run langdetect on pre-extracted text. Returns language code or None."""
    if text is None:
        return None
    try:
        if len(text.strip()) < _MIN_CHARS:
            log.debug(f"{filename}: too little text ({len(text.strip())} chars) — skipping lang detection")
            return None
        results = detect_langs(text)
        if not results:
            return None
        top = results[0]
        log.debug(f"{filename}: lang candidates {results}")
        return top.lang if top.prob >= _CONFIDENCE else None
    except LangDetectException:
        return None
    except Exception as exc:
        log.warning(f"{filename}: language detection error: {exc}")
        return None


def _run_topic_detection(
    filename: str,
    text: str | None,
    patterns: CompiledPatterns,
    min_matches: int,
    min_occurrences: int = 1,
) -> str | None:
    """Match pre-compiled topic patterns against text. Returns first matched topic or None."""
    if text is None or len(text.strip()) < _MIN_CHARS:
        return None
    text_lower = text.lower()
    for topic, kw_patterns in patterns.items():
        hits = [kw for kw, pat in kw_patterns if _meets_occurrence_threshold(pat, text_lower, min_occurrences)]
        if len(hits) >= min_matches:
            log.debug(f"{filename}: topic '{topic}' matched keywords: {hits}")
            return topic
    return None


def _meets_occurrence_threshold(pat: re.Pattern, text: str, min_occ: int) -> bool:
    """Return True if pat matches at least min_occ times in text."""
    if min_occ <= 1:
        return bool(pat.search(text))
    count = 0
    for _ in pat.finditer(text):
        count += 1
        if count >= min_occ:
            return True
    return False


def _filename_is_german(filename: str) -> bool:
    if any(c in _GERMAN_UMLAUTS for c in filename):
        return True
    words = set(re.findall(r"[A-Za-zäöüÄÖÜß]+", filename.lower()))
    return bool(words & _GERMAN_MONTHS)


def _pdf_text(file_path: Path, pages: int, *, include_metadata: bool = False) -> str:
    doc = fitz.open(str(file_path))
    parts = []
    if include_metadata:
        meta = doc.metadata or {}
        meta_text = " ".join(filter(None, [
            meta.get("title", ""),
            meta.get("subject", ""),
            meta.get("keywords", ""),
        ]))
        if meta_text:
            parts.append(meta_text)
    n = min(pages, doc.page_count)
    parts.extend(doc[i].get_text() for i in range(n))
    return " ".join(parts)


def _pdf_text_parts(file_path: Path) -> tuple[str, str, str]:
    """Open the PDF once and return (metadata, lang_body, topic_body).

    Reads up to _PDF_TOPIC_PAGES pages; lang_body is the first _PDF_PAGES of those.
    """
    doc = fitz.open(str(file_path))
    meta = doc.metadata or {}
    metadata = " ".join(filter(None, [
        meta.get("title", ""),
        meta.get("subject", ""),
        meta.get("keywords", ""),
    ]))
    topic_n = min(_PDF_TOPIC_PAGES, doc.page_count)
    pages_text = [doc[i].get_text() for i in range(topic_n)]
    lang_body = " ".join(pages_text[:_PDF_PAGES])
    topic_body = " ".join(pages_text)
    return metadata, lang_body, topic_body


def _epub_text(file_path: Path, *, topic_depth: bool) -> str:
    with zipfile.ZipFile(file_path) as zf:
        parts = []

        if topic_depth:
            # Prepend OPF metadata: dc:title, dc:subject, dc:description.
            # These explicitly state what the book is about — strong topic signal.
            opf = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
            if opf:
                try:
                    opf_content = zf.read(opf).decode("utf-8", errors="ignore")
                    fields = re.findall(
                        r"<dc:(?:title|subject|description)[^>]*>([^<]+)</dc:\w+>",
                        opf_content,
                        re.IGNORECASE,
                    )
                    if fields:
                        parts.append(" ".join(fields))
                except Exception:
                    pass

        names = [
            n for n in zf.namelist()
            if n.lower().endswith((".html", ".xhtml", ".htm"))
        ]
        if not topic_depth:
            # Exclude navigation files for language detection: they're short,
            # potentially mixed-language, and noisy. For topic detection we
            # want them — chapter titles in the TOC are dense domain vocabulary.
            names = [n for n in names if "toc" not in n.lower() and "nav" not in n.lower()]
        limit = _EPUB_TOPIC_CHAPTERS if topic_depth else _EPUB_CHAPTERS
        for name in sorted(names)[:limit]:
            try:
                raw = zf.read(name).decode("utf-8", errors="ignore")
                parts.append(_strip_html(raw))
            except Exception:
                continue
        return " ".join(parts)


def _epub_text_parts(file_path: Path) -> tuple[str, str, str]:
    """Open the EPUB once and return (metadata, lang_body, topic_body)."""
    with zipfile.ZipFile(file_path) as zf:
        metadata = ""
        opf = next((n for n in zf.namelist() if n.lower().endswith(".opf")), None)
        if opf:
            try:
                opf_content = zf.read(opf).decode("utf-8", errors="ignore")
                fields = re.findall(
                    r"<dc:(?:title|subject|description)[^>]*>([^<]+)</dc:\w+>",
                    opf_content,
                    re.IGNORECASE,
                )
                if fields:
                    metadata = " ".join(fields)
            except Exception:
                pass

        all_html = sorted(
            n for n in zf.namelist()
            if n.lower().endswith((".html", ".xhtml", ".htm"))
        )
        # Language path excludes nav/TOC — short, mixed-language, noisy.
        lang_html = [n for n in all_html if "toc" not in n.lower() and "nav" not in n.lower()]

        lang_body = _read_epub_chapters(zf, lang_html[:_EPUB_CHAPTERS])
        topic_body = _read_epub_chapters(zf, all_html[:_EPUB_TOPIC_CHAPTERS])
        return metadata, lang_body, topic_body


def _read_epub_chapters(zf: zipfile.ZipFile, names: list[str]) -> str:
    parts = []
    for name in names:
        try:
            raw = zf.read(name).decode("utf-8", errors="ignore")
            parts.append(_strip_html(raw))
        except Exception:
            continue
    return " ".join(parts)


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
