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

Newspaper/periodical detection:
  _looks_like_newspaper() flags files via an explicit date in the filename
  (numeric, spelled-out month name in EN/ES/FR/IT/DE, or compact/separator-
  less like "1602"/"20240413"), a known-publication-name filename match (a
  built-in list plus config.yaml's filters.newspaper_names, for filenames
  with no date at all -- e.g. "FT US.pdf"), or a dateline-shaped token
  repeated across at least half of the sampled pages/chapters (a running
  masthead date, unlike ordinary books). The publication-name signal is the
  one exception to "no vocabulary keywords" below: it matches publication
  identity, not subject matter, so it doesn't undermine the topic-agnostic
  design. Opt-in via filters.discard_newspapers in config.yaml; independent
  of language/topic filtering (analyze_file() runs all three and returns a
  3-tuple).
"""

import logging
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import fitz
from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

# Silence MuPDF's C-layer stderr spew on malformed PDFs (it bypasses Python
# try/except). Failures are already handled where text is extracted.
fitz.TOOLS.mupdf_display_errors(False)

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

# Numeric, locale-agnostic date patterns (no month names -- unlike the German
# filename heuristic above, newspapers arrive in many languages).
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(\d{4}[-_.]\d{2}[-_.]\d{2}|\d{2}[-_.]\d{2}[-_.]\d{4})(?!\d)"
)
_DATELINE_RE = re.compile(
    r"(?<!\d)(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})(?!\d)"
)

# Spelled-out month names for the languages actually seen in this library's
# newspaper channels (EN/ES/FR/IT/DE). Many real papers date themselves this
# way (e.g. "Corriere della Sera - 16 Gennaio 2026", "El Pais - 7 de julio de
# 2026", "The Economist US 04 July 2026") with no numeric date anywhere in the
# filename or masthead, so the numeric-only patterns above miss them entirely.
_MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre",
    "janvier", "février", "fevrier", "mars", "avril", "mai", "juin", "juillet",
    "août", "aout", "septembre", "octobre", "novembre", "décembre", "decembre",
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
    "agosto", "settembre", "ottobre", "novembre", "dicembre",
    "januar", "februar", "märz", "marz", "april", "mai", "juni", "juli",
    "august", "september", "oktober", "november", "dezember",
}
_MONTH_NAME_ALT = "|".join(re.escape(m) for m in sorted(_MONTH_NAMES, key=len, reverse=True))
# A day number, optionally ordinal (4th) and optionally a range (4th-10th),
# for magazine-style "JULY 4TH-10TH 2026" masthead dates.
_DAY_RE = r"\d{1,2}(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{1,2}(?:st|nd|rd|th)?)?"
# Optional connector word between day/month/year ("7 de julio de 2026").
_DATE_FILLER_RE = r"(?:\s+(?:de|del|du|des|the|of|le|la|di))?"
_MONTH_NAME_DATE_RE = re.compile(
    rf"\b{_DAY_RE}{_DATE_FILLER_RE}\s+(?:{_MONTH_NAME_ALT})\b{_DATE_FILLER_RE}\s+(?:19|20)\d{{2}}\b"
    rf"|\b(?:{_MONTH_NAME_ALT})\b{_DATE_FILLER_RE}\s+{_DAY_RE}{_DATE_FILLER_RE}\s+(?:19|20)\d{{2}}\b",
    re.IGNORECASE,
)

# Compact date formats: digits jammed together with no separator, or a dotted
# date with a 2-digit year. Real-world filenames use these constantly (e.g.
# "NYT 1602.pdf", "NY Daily News_1204.pdf", "WAPO_20240413.pdf") but the
# separator-based patterns above miss them entirely.
#
# _COMPACT_DATE_RE matches a bare 4-digit run only if it's plausible as a
# day+month pair in EITHER order (day 01-31 + month 01-12, or the reverse).
# This is what excludes bare years: "2026" fails both interpretations because
# "20" is not a valid month, so a book title ending in a year never matches.
_COMPACT_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])"  # DDMM
    r"|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"  # MMDD
    r")(?!\d)"
)
# A 19xx/20xx year directly followed by a valid month and day, no separators
# ("WAPO_20240413.pdf").
_COMPACT_LONGDATE_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)

# Publications observed in this library's downloaded files that carry no date
# anywhere in the filename (e.g. "FT US.pdf", "NatGeo.pdf") -- there is no date
# signal to key off for these, so filename identity is the only option. Common
# single dictionary-word names (e.g. "Time") are deliberately excluded: they'd
# false-positive on unrelated titles that happen to start with the same word,
# and in practice such files are already caught by the compact-date regex
# above (e.g. "Time_2601.pdf" -> "2601" validates as a DDMM date).
_KNOWN_PUBLICATION_NAMES = frozenset({
    "ft", "financial times", "nyt", "new york times", "wapo", "washington post",
    "national geographic", "new scientist", "the guardian", "the economist",
    "wsj", "wall street journal", "vogue", "the week", "the new yorker",
    "happiful", "the simple things", "usa today", "newsweek", "new york post",
    "the independent", "toronto star", "der spiegel", "le monde",
    "corriere della sera", "el pais",
})

_PUBLICATION_EXT_RE = re.compile(r"\.(pdf|epub)$", re.IGNORECASE)
_PUBLICATION_SEP_RE = re.compile(r"[_\-]+")
_PUBLICATION_WS_RE = re.compile(r"\s+")


def _normalize_for_publication_match(filename: str) -> str:
    """Lowercase, strip the .pdf/.epub extension, and collapse separators/whitespace to single spaces."""
    stem = _PUBLICATION_EXT_RE.sub("", filename)
    stem = _PUBLICATION_SEP_RE.sub(" ", stem)
    return _PUBLICATION_WS_RE.sub(" ", stem).strip().lower()


def _filename_matches_known_publication(filename: str, extra_names: frozenset[str] = frozenset()) -> bool:
    """Return True if filename starts with a known publication name.

    Matches only at the start of the normalized filename (lowercased,
    extension stripped, separators collapsed to single spaces), followed by
    either end-of-string or a space -- never mid-word and never elsewhere in
    the filename. extra_names (from config.yaml's filters.newspaper_names) is
    checked in addition to the built-in list, never in place of it.
    """
    normalized = _normalize_for_publication_match(filename)
    names = _KNOWN_PUBLICATION_NAMES | {n.lower() for n in extra_names}
    return any(normalized == name or normalized.startswith(name + " ") for name in names)

_NEWSPAPER_PAGE_RATIO = 0.5   # fraction of sampled pages/chapters needing a dateline
_NEWSPAPER_MIN_PAGES = 4      # below this sample size the ratio is too noisy to trust

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
    discard_newspapers: bool = False,
    newspaper_names: frozenset[str] = frozenset(),
) -> tuple[str | None, str | None, bool]:
    """Extract text once and return (language, matched_topic, is_newspaper).

    More efficient than calling detect_language() + detect_topic() separately
    when both are needed — each of those calls extracts and parses the file.
    When topics are configured or discard_newspapers is set, extraction uses a
    larger page sample (_PDF_TOPIC_PAGES) and includes document metadata, which
    also gives langdetect more signal.
    """
    has_topics = bool(topic_keywords or compiled_patterns)
    needs_pages = has_topics or discard_newspapers

    if needs_pages:
        # Single open of the file; split output so each detector gets the right slice.
        metadata, lang_text, topic_body, pages = _extract_text_parts(file_path, ext)
        topic_text = (metadata + " " + topic_body).strip() if metadata else topic_body
    else:
        # Shallow path — no topic/newspaper detection needed, so don't read deeper than necessary.
        lang_text = _extract_text(file_path, ext, topic_depth=False)
        topic_text = None
        pages = []

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

    is_newspaper = False
    if discard_newspapers and ext in ("pdf", "epub"):
        is_newspaper = _looks_like_newspaper(file_path.name, pages, newspaper_names)
        if is_newspaper:
            log.debug(f"{file_path.name}: detected as newspaper/periodical")

    return lang, topic, is_newspaper


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


def detect_newspaper(
    file_path: Path, ext: str, newspaper_names: frozenset[str] = frozenset()
) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Used by the retroactive `scan-newspapers` command, which calls this
    directly per file rather than through analyze_file's combined
    language+topic path.
    """
    if ext not in ("pdf", "epub"):
        return False
    _, _, _, pages = _extract_text_parts(file_path, ext)
    return _looks_like_newspaper(file_path.name, pages, newspaper_names)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_text_parts(file_path: Path, ext: str) -> tuple[str, str, str, list[str]]:
    """Single-pass extraction used by analyze_file. Returns (metadata, lang_body, topic_body, pages).

    Both bodies come from a single doc/zipfile open. lang_body uses the shallow
    page/chapter limits and excludes EPUB nav/TOC noise; topic_body uses the
    deeper limits and includes nav/TOC. pages is the per-page/chapter list behind
    topic_body, used by the newspaper detector. Empty values on error or unsupported format.
    """
    try:
        if ext == "pdf":
            return _pdf_text_parts(file_path)
        if ext == "epub":
            return _epub_text_parts(file_path)
    except Exception as exc:
        log.warning(f"{file_path.name}: text extraction error: {exc}")
    return "", "", "", []


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


def _looks_like_newspaper(
    filename: str, pages: list[str], newspaper_names: frozenset[str] = frozenset()
) -> bool:
    """Return True if the file looks like a newspaper/periodical.

    Three independent signals, any one triggers a match:
      1. Filename carries an explicit date -- numeric (locale-agnostic),
         spelled-out month name (EN/ES/FR/IT/DE), or compact/separator-less
         (e.g. "1602", "20240413").
      2. Filename starts with a known publication name (built-in list plus
         config.yaml's filters.newspaper_names) -- for filenames that carry
         no date at all (e.g. "FT US.pdf").
      3. A dateline-shaped token (numeric or month-name) repeats across at
         least half of the sampled pages/chapters -- a running masthead/footer
         date, which books rarely do but daily papers do by construction.
         Below _NEWSPAPER_MIN_PAGES samples the ratio is too noisy to trust,
         so it's skipped.
    """
    if (
        _FILENAME_DATE_RE.search(filename)
        or _MONTH_NAME_DATE_RE.search(filename)
        or _DATELINE_RE.search(filename)
        or _COMPACT_DATE_RE.search(filename)
        or _COMPACT_LONGDATE_RE.search(filename)
        or _filename_matches_known_publication(filename, newspaper_names)
    ):
        return True
    if len(pages) < _NEWSPAPER_MIN_PAGES:
        return False
    hits = sum(
        1 for p in pages
        if _DATELINE_RE.search(p) or _MONTH_NAME_DATE_RE.search(p)
    )
    return (hits / len(pages)) >= _NEWSPAPER_PAGE_RATIO


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


def _pdf_text_parts(file_path: Path) -> tuple[str, str, str, list[str]]:
    """Open the PDF once and return (metadata, lang_body, topic_body, pages).

    Reads up to _PDF_TOPIC_PAGES pages; lang_body is the first _PDF_PAGES of those.
    pages is the per-page text list (same sample as topic_body) -- used by the
    newspaper dateline-repetition check, which needs page-level granularity.
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
    return metadata, lang_body, topic_body, pages_text


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


def _epub_text_parts(file_path: Path) -> tuple[str, str, str, list[str]]:
    """Open the EPUB once and return (metadata, lang_body, topic_body, chapters).

    chapters is the per-content-file text list behind topic_body -- used by the
    newspaper dateline-repetition check at chapter granularity.
    """
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
        chapters = [_read_epub_chapters(zf, [name]) for name in all_html[:_EPUB_TOPIC_CHAPTERS]]
        topic_body = " ".join(chapters)
        return metadata, lang_body, topic_body, chapters


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
