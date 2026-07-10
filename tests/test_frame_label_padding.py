from conftest import run_extract

# A pill/badge/CTA pattern: an element with its OWN padding, a solid/gradient
# background, direct text ("the label"), and a non-text child (icon/dot). Because
# it has an element child it becomes a Figma FRAME, and the label is emitted as a
# separate `_t` text child. The bug: that text child inherited the FRAME's full
# padded box geometry, so left/center-aligned text hugged the frame's edge instead
# of sitting inside the padding (text overflowed the pill on the left). The label
# must be placed at its TRUE rendered glyph rect (respecting padding + inline-flex).
BADGE_PADDING_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; }
  .btn { position:absolute; left:100px; top:100px;
         display:inline-flex; align-items:center; gap:10px;
         padding:16px 34px; background:#4c1d95; color:#fff;
         font-size:16px; font-weight:700; border-radius:14px; }
  .btn svg { width:18px; height:18px; }
</style></head><body>
  <div class="btn">Kham pha ngay<svg viewBox="0 0 24 24"><path d="M5 12h14"/></svg></div>
</body></html>"""


def _label(spec):
    for e in spec["elements"]:
        if e["type"] == "text" and any("Kham" in r.get("text", "") for r in e.get("runs", [])):
            return e
    return None


def _frame(spec):
    for e in spec["elements"]:
        if e["type"] == "frame" and "btn" in e.get("name", ""):
            return e
    return None


def test_frame_label_respects_padding(tmp_path):
    spec = run_extract(BADGE_PADDING_HTML, tmp_path)
    fr, tx = _frame(spec), _label(spec)
    assert fr is not None, "the padded pill must emit a frame"
    assert tx is not None, "the label must emit a text node"
    # The label must be inset from the frame's left edge by ~padding-left (34px),
    # not hug it (the bug left them at the same x).
    assert tx["x"] > fr["x"] + 15, \
        f"label x={tx['x']} should be inset from frame x={fr['x']} by padding-left"
    # And the label box must be narrower than the full padded frame.
    assert tx["width"] < fr["width"], \
        f"label w={tx['width']} should be narrower than padded frame w={fr['width']}"


# Guard: a frame whose text spans MULTIPLE visual lines (direct text + a
# text-bearing child on another line) must NOT get squashed to a single-line
# glyph rect — the single-line guard should skip the override there.
MULTILINE_FRAME_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; }
  .card { position:absolute; left:100px; top:100px; width:300px;
          padding:20px; background:#222; color:#fff; font-size:16px; }
  .card p { margin:8px 0 0; font-size:13px; color:#aaa; }
</style></head><body>
  <div class="card">Card Title Line<p>A second line of description text here.</p></div>
</body></html>"""


def test_multiline_frame_text_not_squashed(tmp_path):
    spec = run_extract(MULTILINE_FRAME_HTML, tmp_path)
    # The direct-text label of the card ("Card Title Line") should keep a sane
    # height (not be collapsed by a mis-applied single-line glyph override).
    for e in spec["elements"]:
        if e["type"] == "text" and any("Card Title" in r.get("text", "") for r in e.get("runs", [])):
            assert e["height"] >= 14, f"title height {e['height']} collapsed unexpectedly"
            return
    # If the title merged into one node that's fine too — just ensure no crash.
