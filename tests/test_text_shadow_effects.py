"""Tests for CSS text-shadow -> native Figma DROP_SHADOW effect stacking.

text-shadow uses the same "offx offy blur color" token shape as box-shadow, so
_parse_shadows() (already used for box-shadow) parses it directly. Each layer
becomes its own DROP_SHADOW effect on the text node (set_effects accepts an
array) — this is what gives a CSS multi-layer "3D extrusion" numeral/label its
beveled look instead of rendering as flat text (see
tight-lineheight-numeral-mismatch / figma-maximize-native-minimize-raster
memory, scene_9's .number-extrusion)."""
from conftest import run_extract

# Multi-layer text-shadow, each layer offset diagonally with a darkening
# color — the classic CSS "3D extrusion" trick.
MULTI_LAYER_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  h1 { font-size:100px; color:#B88525;
       text-shadow: 1px 1px 0 #AC7A22, 2px 2px 0 #A0701E, 3px 3px 0 #94661A; }
</style></head><body><h1>1</h1></body></html>"""


def test_multilayer_text_shadow_becomes_stacked_drop_shadows(tmp_path):
    spec = run_extract(MULTI_LAYER_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    effects = texts[0].get("effects", [])
    drop_shadows = [e for e in effects if e["type"] == "DROP_SHADOW"]
    assert len(drop_shadows) == 3, f"expected 3 stacked DROP_SHADOW effects, got {effects}"
    # Order and offsets preserved from the CSS layer order.
    assert [e["offset"] for e in drop_shadows] == [
        {"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}, {"x": 3.0, "y": 3.0},
    ]


# text-shadow combined with filter:drop-shadow (glow) on the SAME text node —
# both are independent CSS mechanisms and must both survive as effects.
COMBINED_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  h1 { font-size:80px; color:#fff;
       text-shadow: 2px 2px 0 #888;
       filter: drop-shadow(0 0 10px #0ff); }
</style></head><body><h1>2</h1></body></html>"""


def test_text_shadow_and_filter_drop_shadow_both_present(tmp_path):
    spec = run_extract(COMBINED_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    effects = texts[0].get("effects", [])
    drop_shadows = [e for e in effects if e["type"] == "DROP_SHADOW"]
    assert len(drop_shadows) == 2, f"expected text-shadow + filter:drop-shadow both present, got {effects}"


# No text-shadow at all — must not add any spurious effect.
NO_SHADOW_HTML = """<!doctype html><html><head><style>
  body { margin:0; width:400px; height:300px; background:#111; }
  h1 { font-size:80px; color:#fff; }
</style></head><body><h1>3</h1></body></html>"""


def test_no_text_shadow_adds_no_effect(tmp_path):
    spec = run_extract(NO_SHADOW_HTML, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) == 1
    assert texts[0].get("effects", []) == []
