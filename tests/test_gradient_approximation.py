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
