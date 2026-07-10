from conftest import run_extract

# Fix C: a multi-line WRAPPING text box is widened so Figma's (slightly-wider, or
# font-substituted) text shaping keeps the same line breaks as Chrome — otherwise a
# line filling ~98% of its box re-wraps to an extra line that overflows a
# fixed-height box onto the content below (the glm_5.2 heading overlap).
#
# Two regimes:
#  - SOFT-wrapped text (no hard breaks): widen only +5%, bounded so it can never
#    merge two soft lines into one.
#  - HARD-break text (every line break is a literal <br>/\n): widening can NEVER
#    merge lines, so widen generously (+30%) to survive a Figma font swap that
#    renders the line much wider than Chrome did.
#
# Line 1 ("Trai nghiem toc do sieu viet") renders ~448px @34px Arial bold.

_STYLE = """<style>
  body { margin:0; width:800px; height:400px; background:#111; }
  .h { position:absolute; top:40px; left:40px; width:460px; color:#fff;
       font-family:Arial; font-size:34px; line-height:1.15; font-weight:700; }
</style>"""

# Soft wrap: no <br>; the trailing word can't fit line 1 so it wraps naturally.
SOFT_WRAP_HTML = f"""<!doctype html><html><head>{_STYLE}</head><body>
  <div class="h">Trai nghiem toc do sieu viet extra</div></body></html>"""

# Hard break: an explicit <br> forces line 2 — merging is impossible.
HARD_BREAK_HTML = f"""<!doctype html><html><head>{_STYLE}</head><body>
  <div class="h">Trai nghiem toc do sieu viet<br>dong hai</div></body></html>"""


def _text_width(spec, needle="Trai nghiem"):
    for e in spec["elements"]:
        if e["type"] == "text" and any(needle in r.get("text", "") for r in e.get("runs", [])):
            return e["width"]
    return None


def test_soft_wrap_widened_but_bounded(tmp_path):
    w = _text_width(run_extract(SOFT_WRAP_HTML, tmp_path))
    assert w is not None
    # widened past the 460px box (line 1 ~448px fills it → needs Figma slack)
    assert w > 460, f"a tight soft-wrapped box should be widened, got {w}"
    # bounded to ~box*1.05 so two SOFT lines can never merge into one
    assert w <= round(460 * 1.05) + 1, f"soft widen must stay bounded (~box*1.05), got {w}"


def test_hard_break_widened_generously(tmp_path):
    w = _text_width(run_extract(HARD_BREAK_HTML, tmp_path))
    assert w is not None
    # hard <br> can't merge → widen generously (well past the +5% soft bound) so a
    # Figma font swap that renders the line wider than Chrome still fits one line
    assert w > round(460 * 1.05) + 1, f"hard-break text should be widened generously, got {w}"
    # still frame-bounded (fixture frame is < 800px wide)
    assert w < 800, f"widen must stay within the frame, got {w}"


# A single short line in a wide box must NOT be widened (nothing wraps).
SINGLE_LINE_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:200px; background:#111; }
  .h { position:absolute; top:40px; left:40px; width:600px; color:#fff;
       font-family:Arial; font-size:34px; }
</style></head><body><div class="h">Short title</div></body></html>"""


def test_single_line_text_not_widened(tmp_path):
    w = _text_width(run_extract(SINGLE_LINE_HTML, tmp_path), needle="Short")
    assert w is not None
    assert w <= 600, f"single-line text must not be widened, got {w}"
