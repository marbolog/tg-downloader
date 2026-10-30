"""PyMuPDF 1.28.2+ warns on `import fitz` and 2.0 turns that into a FutureWarning;
the alias is slated for removal. Importing the app's modules must not touch it."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_importing_pdf_modules_does_not_use_legacy_fitz_alias():
    result = subprocess.run(
        [sys.executable, "-W", "error::FutureWarning", "-c",
         "import lang_filter, search.chunker; import sys; "
         "assert 'fitz' not in sys.modules or 'pymupdf' in sys.modules"],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = result.stdout + result.stderr
    assert "fitz" not in output.lower(), output
    assert result.returncode == 0, output
