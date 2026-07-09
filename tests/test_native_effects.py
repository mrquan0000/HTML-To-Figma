from conftest import run_extract

BLUR_LEAF_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  .blob { position:absolute; top:50px; left:50px; width:120px; height:120px;
          background:#3366ff; border-radius:50%; filter: blur(4px); }
</style></head><body><div class="blob"></div></body></html>"""


def test_blur_only_leaf_stays_native(tmp_path):
    spec = run_extract(BLUR_LEAF_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1, "a blurred leaf must stay native, not raster"
    effects = shapes[0].get("effects", [])
    assert any(e["type"] == "LAYER_BLUR" and abs(e["radius"] - 4) < 0.5 for e in effects), \
        f"expected a LAYER_BLUR radius~4 effect, got {effects}"


BLUR_CONTAINER_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  .card { position:absolute; top:40px; left:40px; width:200px; height:120px;
          background:#222; filter: blur(3px); }
  .card h2 { color:#fff; font-size:20px; margin:20px; }
</style></head><body><div class="card"><h2>Hello</h2></div></body></html>"""


def test_blur_on_container_stays_native(tmp_path):
    spec = run_extract(BLUR_CONTAINER_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert raster == [], "a blurred container must stay native, not rasterize"
    containers = [e for e in spec["elements"] if e["type"] in ("frame", "rectangle")]
    assert any(any(fx.get("type") == "LAYER_BLUR" for fx in c.get("effects", []))
               for c in containers), "the blurred container must carry a LAYER_BLUR effect"
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert any("Hello" in "".join(r["text"] for r in t.get("runs", [])) for t in texts), \
        "child text must remain its own editable element"


GLOW_SHAPE_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  .badge { position:absolute; top:60px; left:60px; width:80px; height:80px;
           background:#ffcc00; border-radius:50%;
           filter: drop-shadow(0 0 12px #ffcc00); }
</style></head><body><div class="badge"></div></body></html>"""


def test_dropshadow_only_shape_stays_native(tmp_path):
    spec = run_extract(GLOW_SHAPE_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1, "a glow (drop-shadow-only) SHAPE must stay native, not raster"
    effects = shapes[0].get("effects", [])
    assert any(e["type"] == "DROP_SHADOW" for e in effects), f"expected DROP_SHADOW, got {effects}"


COMBINED_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  .blob { position:absolute; top:50px; left:50px; width:100px; height:100px;
          background:#33ccff; border-radius:50%;
          filter: blur(2px) drop-shadow(0 0 10px #33ccff); }
</style></head><body><div class="blob"></div></body></html>"""


def test_blur_and_dropshadow_combined_stays_native(tmp_path):
    spec = run_extract(COMBINED_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1, "combined blur()+drop-shadow() must stay native"
    types = {e["type"] for e in shapes[0].get("effects", [])}
    assert "LAYER_BLUR" in types and "DROP_SHADOW" in types, f"expected both effect types, got {types}"


OTHER_FILTER_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  .blob { position:absolute; top:50px; left:50px; width:100px; height:100px;
          background:#33ccff; filter: blur(2px) hue-rotate(90deg); }
</style></head><body><div class="blob"></div></body></html>"""


def test_filter_combined_with_unrelated_function_still_rasters(tmp_path):
    spec = run_extract(OTHER_FILTER_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert len(raster) == 1, "blur() combined with an unrelated filter (hue-rotate) must still raster"


# filter:drop-shadow()-only DIRECTLY on a text leaf (not a container, not a
# plain shape) — this already stayed native before this task (the classify()
# text-only rule pre-dates it), but the effect must land on the TEXT node
# ONLY. A naive "always add filter-effects at shape level" change would ALSO
# spawn a phantom background rectangle/frame with the same DROP_SHADOW,
# rendering a rectangular shadow box the original CSS never shows (CSS
# filter:drop-shadow on text is glyph-shaped, not box-shaped).
GLOW_TEXT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:200px; background:#111; }
  h1 { position:absolute; top:60px; left:40px; color:#fff; font-size:32px;
       filter: drop-shadow(0 0 10px #fff); }
</style></head><body><h1>SHINE</h1></body></html>"""


def test_dropshadow_on_text_leaf_attaches_to_text_not_phantom_shape(tmp_path):
    spec = run_extract(GLOW_TEXT_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse", "frame")]
    assert shapes == [], f"glow text must not spawn a phantom background shape, got {shapes}"
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    assert any(e["type"] == "DROP_SHADOW" for e in texts[0].get("effects", []))


# filter:blur()-only DIRECTLY on a text leaf. Before this task this rasterized
# (classify()'s old rule only kept drop-shadow-only text native, not blur).
# After this task it must become native text with LAYER_BLUR on the TEXT
# node — same phantom-shape risk as the glow case above.
BLUR_TEXT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:200px; background:#111; }
  h1 { position:absolute; top:60px; left:40px; color:#fff; font-size:32px;
       filter: blur(3px); }
</style></head><body><h1>SOFT</h1></body></html>"""


def test_blur_on_text_leaf_attaches_to_text_not_phantom_shape(tmp_path):
    spec = run_extract(BLUR_TEXT_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse", "frame")]
    assert shapes == [], f"blurred text must not spawn a phantom background shape, got {shapes}"
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    assert any(e["type"] == "LAYER_BLUR" for e in texts[0].get("effects", []))
