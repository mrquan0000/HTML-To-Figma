from conftest import run_extract

# Black→white 2-stop gradient on a small shape (well under 95% of an 800x600
# frame). Exact blend is verifiable: 70% black (lightness 0) + 30% white
# (lightness 1) → gray at r=g=b=0.3 (0-1 scale).
SMALL_GRADIENT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: linear-gradient(90deg, #000000 0%, #ffffff 100%); }
</style></head><body><div class="card"></div></body></html>"""


def test_small_shape_gradient_becomes_solid_with_darker_bias(tmp_path):
    spec = run_extract(SMALL_GRADIENT_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert raster == [], "a small (non-full-frame) gradient shape must go native"
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1
    fill = shapes[0]["fills"][0]
    assert fill["type"] == "SOLID"
    c = fill["color"]
    assert abs(c["r"] - 0.3) < 0.02 and abs(c["g"] - 0.3) < 0.02 and abs(c["b"] - 0.3) < 0.02, \
        f"expected 70% black + 30% white ~= 0.3 gray, got {c}"
    assert any("approximated gradient fill" in w for w in spec["warnings"])


# Same gradient, but the element now covers ~the whole frame (>=95%) — must
# stay raster (atmospheric/background gradients keep full fidelity).
FULL_FRAME_GRADIENT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; position:relative; }
  .bg { position:absolute; inset:0;
        background: linear-gradient(180deg, #000000 0%, #ffffff 100%); }
</style></head><body><div class="bg"></div></body></html>"""


def test_full_frame_gradient_still_rasters(tmp_path):
    spec = run_extract(FULL_FRAME_GRADIENT_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert len(raster) == 1, "a full-frame (>=95%) gradient background must still raster"


# background-clip:text gradient — same black->white blend, applied to text.
GRADIENT_TEXT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:300px; background:#111; }
  h1 { font-size:48px; margin:40px;
       background: linear-gradient(90deg, #000000 0%, #ffffff 100%);
       -webkit-background-clip: text; background-clip: text;
       -webkit-text-fill-color: transparent; color: transparent; }
</style></head><body><h1>GLOW TEXT</h1></body></html>"""


def test_gradient_text_becomes_native_solid_text(tmp_path):
    spec = run_extract(GRADIENT_TEXT_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert raster == [], "gradient text (simple linear gradient) must stay native"
    # The gradient is only visible THROUGH the glyphs (background-clip:text) —
    # it must not ALSO spawn a solid background rectangle/frame behind the text.
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse", "frame")]
    assert shapes == [], \
        f"gradient-clip text must not also emit a background shape, got {shapes}"
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    joined = "".join(r["text"] for r in texts[0]["runs"])
    assert "GLOW TEXT" in joined, "text content/editability must be preserved"
    fill = texts[0]["runs"][0]["fills"][0]
    assert fill["type"] == "SOLID"
    c = fill["color"]
    assert abs(c["r"] - 0.3) < 0.02
    assert any("approximated gradient text fill" in w for w in spec["warnings"])


# 3-stop gradient: black (0%) -> red (50%) -> white (100%). Lightness extremes
# are black (L=0) and white (L=1); red's L~=0.5 must be ignored.
THREE_STOP_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: linear-gradient(90deg, #000000 0%, #ff0000 50%, #ffffff 100%); }
</style></head><body><div class="card"></div></body></html>"""


def test_three_stop_gradient_ignores_middle_stop(tmp_path):
    spec = run_extract(THREE_STOP_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1
    c = shapes[0]["fills"][0]["color"]
    assert abs(c["r"] - 0.3) < 0.05 and abs(c["g"] - 0.3) < 0.05 and abs(c["b"] - 0.3) < 0.05, \
        f"middle red stop must be ignored; expected black/white blend, got {c}"


# conic-gradient is NOT a simple linear/radial gradient — must still raster.
CONIC_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: conic-gradient(#000000, #ffffff); }
</style></head><body><div class="card"></div></body></html>"""


def test_conic_gradient_still_rasters(tmp_path):
    spec = run_extract(CONIC_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert len(raster) == 1, "conic-gradient is out of scope, must still raster"


# Stacked/multi-layer background (two gradients, comma-separated at the top
# level) — must still raster. A naive check could wrongly treat this as one
# "simple" gradient and blend unrelated stops/alphas from the second layer.
STACKED_LAYERS_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(0,0,0,0.6) 100%),
                      linear-gradient(180deg, #ff0000 0%, #0000ff 100%); }
</style></head><body><div class="card"></div></body></html>"""


def test_stacked_multilayer_gradient_still_rasters(tmp_path):
    spec = run_extract(STACKED_LAYERS_HTML, tmp_path)
    raster = [e for e in spec["elements"] if e["type"] == "image"]
    assert len(raster) == 1, "a stacked multi-layer gradient background must still raster"


# A transparent-text element with its OWN gradient background but WITHOUT
# background-clip:text (e.g. a visually-hidden label over a decorative
# gradient card) is NOT gradient-clip-text — it must still get the shape's
# gradient approximated to a solid fill, not silently lose it.
HIDDEN_TEXT_ON_GRADIENT_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: linear-gradient(90deg, #000000 0%, #ffffff 100%);
          color: transparent; -webkit-text-fill-color: transparent; }
</style></head><body><div class="card">hidden label</div></body></html>"""


def test_transparent_text_on_gradient_card_keeps_solid_fill(tmp_path):
    spec = run_extract(HIDDEN_TEXT_ON_GRADIENT_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse", "frame")]
    assert len(shapes) == 1, "the gradient card's own fill must not silently disappear"
    fill = shapes[0]["fills"][0]
    assert fill["type"] == "SOLID"
    c = fill["color"]
    assert abs(c["r"] - 0.3) < 0.02, f"expected 70% black + 30% white blend, got {c}"


# A shape with BOTH a qualifying gradient background AND a qualifying blur
# filter must get BOTH treatments: solid fill (gradient approximation) AND
# a LAYER_BLUR effect — proving Task 1 and Task 2's logic compose correctly.
GRADIENT_AND_BLUR_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#222; position:relative; }
  .card { position:absolute; top:100px; left:100px; width:200px; height:120px;
          background: linear-gradient(90deg, #000000 0%, #ffffff 100%);
          filter: blur(3px); }
</style></head><body><div class="card"></div></body></html>"""


def test_gradient_and_blur_compose_on_same_element(tmp_path):
    spec = run_extract(GRADIENT_AND_BLUR_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1
    fill = shapes[0]["fills"][0]
    assert fill["type"] == "SOLID"
    assert abs(fill["color"]["r"] - 0.3) < 0.02
    assert any(e["type"] == "LAYER_BLUR" for e in shapes[0].get("effects", []))


# Fade-in/fade-out edges (transparent stops) around a solid middle color — a
# common "glow line" / vignette-edge pattern (scene_12's underline). The
# transparent stops carry NO real color (their rgb is just a parse default,
# not a genuine dark color) and must not be allowed to win "darkest" and drag
# the blended color/alpha toward black — the visually-dominant solid middle
# color must win instead.
FADE_EDGES_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:800px; height:600px; background:#111; position:relative; }
  .underline { position:absolute; top:100px; left:100px; width:300px; height:20px;
               background: linear-gradient(90deg, transparent 0%, #35CC23 10%, #35CC23 90%, transparent 100%); }
</style></head><body><div class="underline"></div></body></html>"""


def test_fade_transparent_edges_dont_drag_color_toward_black(tmp_path):
    spec = run_extract(FADE_EDGES_HTML, tmp_path)
    shapes = [e for e in spec["elements"] if e["type"] in ("rectangle", "ellipse")]
    assert len(shapes) == 1
    fill = shapes[0]["fills"][0]
    assert fill["type"] == "SOLID"
    c = fill["color"]
    # #35CC23 = rgb(53,204,35)/255 ≈ (0.208, 0.800, 0.137) — the true solid
    # middle color, not diluted by the transparent edge stops.
    assert abs(c["r"] - 0.2078) < 0.01
    assert abs(c["g"] - 0.8) < 0.01
    assert abs(c["b"] - 0.1373) < 0.01
    assert c["a"] > 0.99, f"alpha must not be dragged down by transparent stops, got {c['a']}"
