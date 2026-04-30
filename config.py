import sys
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[error] Config file not found: {path}")
        print("Copy config.yaml.example to config.yaml and fill in your credentials.")
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    _validate(raw, path)
    _apply_defaults(raw)

    return raw


def _validate(raw: dict, path: str) -> None:
    errors = []

    tg = raw.get("telegram", {})
    if not tg.get("api_id") or tg["api_id"] == 12345:
        errors.append("telegram.api_id must be set to your real API id")
    if not tg.get("api_hash") or tg["api_hash"] == "your_api_hash_here":
        errors.append("telegram.api_hash must be set to your real API hash")

    channels = raw.get("channels")
    if not channels:
        errors.append("'channels' list must not be empty")

    if errors:
        print(f"[error] Invalid config ({path}):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


def _apply_defaults(raw: dict) -> None:
    raw["telegram"].setdefault("session_file", "tg_session")

    dl = raw.setdefault("download", {})
    dl.setdefault("destination", "~/Downloads/telegram")
    dl.setdefault("max_messages_per_channel", 200)

    filters = raw.setdefault("filters", {})
    filters.setdefault("extensions", [])

    # Normalize extensions: lowercase, no leading dot
    filters["extensions"] = [
        e.lower().lstrip(".") for e in filters["extensions"]
    ]

    # Expand ~ in destination
    dl["destination"] = str(Path(dl["destination"]).expanduser())
