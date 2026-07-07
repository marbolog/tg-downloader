import pytest  # noqa: F401 — used for pytest.raises
import yaml
from pathlib import Path
from unittest.mock import patch

from config import _apply_defaults, _validate, load_config


def _write_config(path: Path, data: dict) -> str:
    p = path / "config.yaml"
    with open(p, "w") as f:
        yaml.dump(data, f)
    return str(p)


def _minimal_valid() -> dict:
    return {
        "telegram": {"api_id": 99999, "api_hash": "abc123realvalue"},
    }


class TestValidate:
    def test_valid_config_passes(self):
        raw = _minimal_valid()
        # Should not raise or call sys.exit.
        with patch("sys.exit") as mock_exit:
            _validate(raw, "config.yaml")
            mock_exit.assert_not_called()

    def test_placeholder_api_id_exits(self):
        raw = _minimal_valid()
        raw["telegram"]["api_id"] = 12345
        with patch("sys.exit") as mock_exit:
            _validate(raw, "config.yaml")
            mock_exit.assert_called_once_with(1)

    def test_missing_api_hash_exits(self):
        raw = _minimal_valid()
        raw["telegram"]["api_hash"] = "your_api_hash_here"
        with patch("sys.exit") as mock_exit:
            _validate(raw, "config.yaml")
            mock_exit.assert_called_once_with(1)

    def test_missing_telegram_section_exits(self):
        raw = {}
        with patch("sys.exit") as mock_exit:
            _validate(raw, "config.yaml")
            mock_exit.assert_called_once_with(1)


class TestApplyDefaults:
    def test_session_file_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["telegram"]["session_file"] == "data/tg_session"

    def test_session_file_preserved_if_set(self):
        raw = _minimal_valid()
        raw["telegram"]["session_file"] = "custom/session"
        _apply_defaults(raw)
        assert raw["telegram"]["session_file"] == "custom/session"

    def test_download_destination_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["download"]["destination"] == "data/downloads"

    def test_retention_days_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["download"]["retention_days"] == 365

    def test_concurrent_downloads_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["download"]["concurrent_downloads"] == 1

    def test_extension_filter_normalised_lowercase_no_dot(self):
        raw = _minimal_valid()
        raw["filters"] = {"extensions": [".PDF", "EPUB", ".mobi"]}
        _apply_defaults(raw)
        assert raw["filters"]["extensions"] == ["pdf", "epub", "mobi"]

    def test_empty_extensions_becomes_empty_list(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["filters"]["extensions"] == []

    def test_topic_min_matches_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["filters"]["topic_min_matches"] == 2

    def test_topic_min_keyword_occurrences_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["filters"]["topic_min_keyword_occurrences"] == 1

    def test_discard_newspapers_default(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["filters"]["discard_newspapers"] is False

    def test_rag_defaults_applied(self):
        raw = _minimal_valid()
        _apply_defaults(raw)
        assert raw["rag"]["enabled"] is False
        assert "index_path" in raw["rag"]
        assert raw["rag"]["top_k"] == 5


class TestLoadConfig:
    def test_missing_file_exits(self, tmp_path):
        with patch("sys.exit", side_effect=SystemExit(1)) as mock_exit:
            with pytest.raises(SystemExit):
                load_config(str(tmp_path / "nonexistent.yaml"))
            mock_exit.assert_called_once_with(1)

    def test_valid_file_returns_dict(self, tmp_path):
        p = _write_config(tmp_path, _minimal_valid())
        with patch("sys.exit") as mock_exit:
            cfg = load_config(p)
            mock_exit.assert_not_called()
        assert isinstance(cfg, dict)
        assert cfg["telegram"]["api_id"] == 99999

    def test_defaults_applied_after_load(self, tmp_path):
        p = _write_config(tmp_path, _minimal_valid())
        cfg = load_config(p)
        assert "retention_days" in cfg["download"]
