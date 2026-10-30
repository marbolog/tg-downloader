#!/bin/sh
# Called by yazi's delete keys (yazi/keymap.toml). `tgdctl browse` exports TGD_PYTHON
# (the interpreter it runs under, which already has the dependencies); outside that
# launcher we fall back to uv.
DIR=$(cd "$(dirname "$0")/.." && pwd)
if [ -n "$TGD_PYTHON" ]; then
    exec "$TGD_PYTHON" "$DIR/tgdctl.py" discard-path "$@"
fi
exec uv run --project "$DIR" tgdctl discard-path "$@"
