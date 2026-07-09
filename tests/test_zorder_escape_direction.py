"""Regression tests for Pass 3.5's overflow-escape z-bump direction fix.

Pass 3.5 (agents/html_extractor.py) reparents a child to root level when it
overflows its frame parent (which would otherwise clip it — figma-mcp-go
frames always clip). The escaped child's z must be bumped so it renders
correctly relative to what it left behind. A frame composites as one
all-or-nothing unit in the outer stack, so an escaped child can only render
fully above or fully below the WHOLE frame — never interleaved with
individual children still inside it. The direction must depend on whether any
sibling REMAINING inside outranks the escaping child (see
tight-lineheight-numeral-mismatch's sibling memory, scene_9's pin-bg icon
(z-index:0) escaping a frame that also holds a z-index:5 numeral)."""
from conftest import run_extract


# Original scenario (the one this mechanism was first built for): a card with
# its own background, whose ONLY child is a label that overflows the card and
# was already the topmost content (no other sibling to conflict with). The
# escaping label must still bump ABOVE the card — unchanged from before this
# fix, since nothing inside outranks it.
CARD_LABEL_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:400px; background:#111; }
  .wrap { position:absolute; top:50px; left:50px; width:200px; height:200px; }
  .card { position:relative; width:100px; height:100px; background:#333; }
  .label { position:absolute; bottom:-30px; left:0; width:140px; color:#fff; font-size:20px; }
</style></head><body><div class="wrap"><div class="card"><span class="label">UNDERSTAND OVERFLOW</span></div></div></body></html>"""


def test_solo_overflowing_child_still_escapes_above(tmp_path):
    spec = run_extract(CARD_LABEL_HTML, tmp_path)
    by_type = {e["type"]: e for e in spec["elements"]}
    card = next(e for e in spec["elements"] if e["type"] == "frame")
    label = next(e for e in spec["elements"] if e["type"] == "text")
    assert label["parent_id"] is None, "the overflowing label must have escaped to root level"
    assert label["z"] > card["z"], \
        "a solo overflowing child (no higher-ranked sibling left inside) must still escape ABOVE its frame"
