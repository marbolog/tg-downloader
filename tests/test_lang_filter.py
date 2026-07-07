"""Unit tests for lang_filter.py's pure-Python logic.

No tests here require real PDF/EPUB file fixtures — those functions are
verified manually (see docs/superpowers/plans/2026-07-06-newspaper-filter.md).
"""

from lang_filter import _looks_like_newspaper


class TestLooksLikeNewspaper:
    def test_filename_iso_date_detected(self):
        assert _looks_like_newspaper("Der Spiegel 2026-07-06.pdf", []) is True

    def test_filename_dotted_date_detected(self):
        assert _looks_like_newspaper("Zeitung_06.07.2026.pdf", []) is True

    def test_filename_underscored_date_detected(self):
        assert _looks_like_newspaper("report_2024_01_15.pdf", []) is True

    def test_filename_without_date_and_no_pages_not_detected(self):
        assert _looks_like_newspaper("python_tutorial.pdf", []) is False

    def test_page_ratio_above_threshold_detected(self):
        pages = [
            "Ausgabe Nr. 27 . 6.7.2026 front page text",
            "regular article text with no date at all here",
            "back page dateline 6.7.2026 continues",
            "another dateline 6.7.2026 on this page too",
            "final page plain text",
        ]
        # 3 of 5 pages carry a dateline -> ratio 0.6 >= 0.5
        assert _looks_like_newspaper("masthead.pdf", pages) is True

    def test_page_ratio_below_threshold_not_detected(self):
        pages = [
            "front page dateline 6.7.2026 appears once",
            "chapter two, no dates mentioned anywhere",
            "chapter three, still no dates here",
            "chapter four, plain narrative text",
        ]
        # 1 of 4 pages -> ratio 0.25 < 0.5
        assert _looks_like_newspaper("book.pdf", pages) is False

    def test_below_min_pages_not_trusted_even_at_full_ratio(self):
        pages = [
            "dateline 6.7.2026 here",
            "dateline 6.7.2026 here too",
            "dateline 6.7.2026 again",
        ]
        # Only 3 sampled pages (< _NEWSPAPER_MIN_PAGES=4); ratio would be 1.0 but
        # the sample is too small to trust.
        assert _looks_like_newspaper("short.pdf", pages) is False

    def test_exactly_min_pages_at_threshold_detected(self):
        pages = [
            "dateline 6.7.2026 here",
            "dateline 6.7.2026 here too",
            "plain text, no date",
            "plain text, no date either",
        ]
        # 2 of 4 pages -> ratio exactly 0.5, meets the >= threshold
        assert _looks_like_newspaper("edge.pdf", pages) is True

    def test_empty_pages_list_not_detected(self):
        assert _looks_like_newspaper("plain.pdf", []) is False
