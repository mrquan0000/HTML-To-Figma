"""Regression tests for the tight-lineheight-numeral-mismatch fix.

A giant decorative numeral/label with CSS line-height squeezed much tighter
than the font's natural single-line height (e.g. line-height:.82 on a 550px
numeral) makes a bare text leaf's DOM bounding box far taller than its true
glyph ink. See memory: tight-lineheight-numeral-mismatch.md (scene_2, recurred
scene_9)."""
from conftest import run_extract

# Giant single glyph, squeezed line-height (0.82 << natural ~1.2 for this font
# size) — the DOM box is inflated well beyond the glyph's true ink. Must be
# corrected: height shrinks, exact_size flag set so figma_builder.py forces it.
SQUEEZED_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:600px; background:#111; position:relative; }
  .num { position:relative; font-family: Arial, sans-serif; font-weight:900;
         font-size:300px; line-height:0.82; color:#fff; }
</style></head><body><span class="num">1</span></body></html>"""


def test_squeezed_numeral_height_corrected_and_flagged(tmp_path):
    spec = run_extract(SQUEEZED_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    t = texts[0]
    line_height_px = 300 * 0.82  # 246
    assert t["height"] < line_height_px * 1.3, \
        f"expected corrected (tighter) height, got {t['height']}"
    assert t.get("exact_size") is True, \
        "corrected text must carry exact_size so the builder forces the resize"


# Same glyph, NORMAL line-height (no squeeze) — must NOT be touched: no
# exact_size flag, height stays at the natural DOM-measured value.
NORMAL_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:600px; background:#111; position:relative; }
  .num { position:relative; font-family: Arial, sans-serif; font-weight:900;
         font-size:300px; line-height:1.2; color:#fff; }
</style></head><body><span class="num">1</span></body></html>"""


def test_normal_lineheight_glyph_untouched(tmp_path):
    spec = run_extract(NORMAL_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    assert not texts[0].get("exact_size"), \
        "a glyph with normal (non-squeezed) line-height must not trigger the ink correction"


# Genuinely multi-line block text with tight line-height — must NOT be
# squashed to a single canvas-measured line's ink height.
MULTILINE_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:600px; background:#111; position:relative; }
  .para { display:block; font-family: Arial, sans-serif; font-size:24px;
          line-height:0.9; color:#fff; width:200px; }
</style></head><body><span class="para">This is a longer paragraph that wraps across multiple lines for sure.</span></body></html>"""


def test_multiline_block_text_not_squashed(tmp_path):
    spec = run_extract(MULTILINE_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    t = texts[0]
    assert not t.get("exact_size"), \
        "genuine multi-line text must not trigger the single-line ink correction"
    line_height_px = 24 * 0.9  # 21.6
    assert t["height"] > line_height_px * 2, \
        f"multi-line text height must reflect several lines, got {t['height']}"
