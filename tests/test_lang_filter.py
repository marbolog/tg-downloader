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

    def test_filename_italian_month_name_detected(self):
        assert _looks_like_newspaper("Corriere della Sera - 16 Gennaio 2026.pdf", []) is True

    def test_filename_spanish_month_name_with_filler_words_detected(self):
        assert _looks_like_newspaper("El Pais - 7 de julio de 2026.pdf", []) is True

    def test_filename_french_month_name_with_weekday_prefix_detected(self):
        assert _looks_like_newspaper("Le Monde du Mardi 13 Janvier 2026.pdf", []) is True

    def test_filename_english_month_first_ordinal_range_detected(self):
        assert _looks_like_newspaper("The Economist US - JULY 4TH-10TH 2026.pdf", []) is True

    def test_filename_month_word_without_day_or_year_not_detected(self):
        assert _looks_like_newspaper("What Your Dog is Trying to Tell You - 8th Edition, 2026.pdf", []) is False

    def test_page_ratio_above_threshold_with_month_names_detected(self):
        pages = [
            "Servizio Clienti ANNO 151 - N. 13 VENERDI 16 GENNAIO 2026 di Ernesto Gal",
            "regular article text with no date at all here",
            "back page dateline 16 Gennaio 2026 continues",
            "another dateline 16 Gennaio 2026 on this page too",
            "final page plain text",
        ]
        # 3 of 5 pages carry a month-name dateline -> ratio 0.6 >= 0.5
        assert _looks_like_newspaper("Corriere_della_Sera.pdf", pages) is True

    def test_page_mentioning_month_and_year_once_not_detected(self):
        pages = [
            "In July 2026 the committee published its findings on climate policy",
            "chapter two, no dates mentioned anywhere",
            "chapter three, still no dates here",
            "chapter four, plain narrative text",
        ]
        # 1 of 4 pages -> ratio 0.25 < 0.5, and no day number is present anyway
        assert _looks_like_newspaper("book.pdf", pages) is False

    def test_filename_compact_ddmm_date_detected(self):
        assert _looks_like_newspaper("NYT 1602.pdf", []) is True

    def test_filename_compact_mmdd_date_detected(self):
        assert _looks_like_newspaper("NY Daily News_1204.pdf", []) is True

    def test_filename_compact_yyyymmdd_date_detected(self):
        assert _looks_like_newspaper("WAPO_20240413.pdf", []) is True

    def test_filename_two_digit_year_dotted_date_detected(self):
        assert _looks_like_newspaper("FT How to Spend it 7.3.26.pdf", []) is True

    def test_filename_bare_year_not_detected_as_compact_date(self):
        assert _looks_like_newspaper("Vietnam 1975.pdf", []) is False

    def test_filename_bare_recent_year_not_detected_as_compact_date(self):
        assert _looks_like_newspaper("My Book 2026.pdf", []) is False

    def test_filename_december_31_compact_date_detected(self):
        assert _looks_like_newspaper("Late Edition 1231.pdf", []) is True
