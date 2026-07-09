"""Shared pytest helpers. Runs the real extractor on inline HTML fixtures."""
import sys
from pathlib import Path

# Make `agents` importable when pytest runs from repo root.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from agents.html_extractor import extract  # noqa: E402


def run_extract(html: str, tmp_path) -> dict:
    """Write `html` to a temp file, extract it at a fixed 800px width, return the spec dict."""
    html_file = tmp_path / "fixture.html"
    html_file.write_text(html, encoding="utf-8")
    assets = tmp_path / "assets"
    return extract(str(html_file), viewport_width=800, assets_dir=str(assets))
