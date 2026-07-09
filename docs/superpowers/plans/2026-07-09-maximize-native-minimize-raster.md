# Maximize Native, Minimize Raster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agents/html_extractor.py`'s `classify()` keep more content native (editable in Figma) instead of rasterizing it, specifically: `filter: blur()`/`drop-shadow()` (map to Figma's native `LAYER_BLUR`/`DROP_SHADOW` effects) and simple linear/radial gradients on shapes or text (approximate to one solid color, darker-biased, instead of a raster PNG).

**Architecture:** All changes live in `agents/html_extractor.py` only. `classify()` (JS, runs in-browser during the DOM walk) decides native vs raster per element; two Python-side builder functions (`_emit_native_element`'s effects/fills assembly, `_build_text_runs`) then attach the actual effect/fill data once classify() has already committed to 'native'. `figma_builder.py` needs zero changes — it already forwards `LAYER_BLUR` effects and `SOLID` fills.

**Tech Stack:** Python 3 (stdlib `re`, `colorsys`), Playwright (Chromium), pytest.

**Design doc:** `docs/superpowers/specs/2026-07-09-maximize-native-minimize-raster-design.md`

---

### Task 1: `filter: blur()`/`drop-shadow()` → native `LAYER_BLUR`/`DROP_SHADOW` effects

**Files:**
- Modify: `agents/html_extractor.py:346-352` (rename/expand `filterIsOnlyDropShadow`)
- Modify: `agents/html_extractor.py:354-423` (`classify()`)
- Modify: `agents/html_extractor.py` call site of `classify()` (currently `agents/html_extractor.py:625`)
- Modify: `agents/html_extractor.py:1242-1256` area (add `_parse_filter_blur`, right after `_parse_filter_drop_shadows`)
- Modify: `agents/html_extractor.py:1399-1421` (`_emit_native_element`'s shape-level Effects block)
- Modify: `agents/html_extractor.py:1556` area (`_emit_native_element`'s TEXT-level `text_effects`)
- Test: `tests/test_native_effects.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_native_effects.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_native_effects.py -v`
Expected: `test_blur_only_leaf_stays_native`, `test_blur_on_container_stays_native`, `test_dropshadow_only_shape_stays_native`, `test_blur_and_dropshadow_combined_stays_native`, and `test_blur_on_text_leaf_attaches_to_text_not_phantom_shape` FAIL. `test_filter_combined_with_unrelated_function_still_rasters` currently passes by coincidence (confirm it still passes after implementation). `test_dropshadow_on_text_leaf_attaches_to_text_not_phantom_shape` currently ALSO passes (drop-shadow-only text was already native) — confirm it still passes after implementation (regression guard).

- [ ] **Step 3: Rename `filterIsOnlyDropShadow` → `filterIsBlurAndOrDropShadow` and update `classify()`**

In `agents/html_extractor.py`, replace (lines 346-352):

```javascript
    // A filter string made up ONLY of drop-shadow() functions (glow/shadow) —
    // no blur/brightness/etc. These map 1:1 to Figma DROP_SHADOW effects.
    function filterIsOnlyDropShadow(filter) {
        if (!filter || filter === 'none') return false;
        const stripped = filter.replace(/drop-shadow\((?:[^()]|\([^()]*\))*\)/g, '').trim();
        return stripped === '';
    }
```

with:

```javascript
    // A filter string made up ONLY of blur() and/or drop-shadow() functions
    // (any count/combination of either) — these map straight onto Figma's
    // LAYER_BLUR / DROP_SHADOW effects (no rasterization needed) whether the
    // element is a leaf or a container: Figma's LAYER_BLUR on a frame blurs
    // the whole rendered composite, matching CSS filter:blur() on a
    // container. Any other/combined filter (hue-rotate, contrast, ...) still
    // rasterizes.
    function filterIsBlurAndOrDropShadow(filter) {
        if (!filter || filter === 'none') return false;
        const stripped = filter
            .replace(/blur\([^)]*\)/g, '')
            .replace(/drop-shadow\((?:[^()]|\([^()]*\))*\)/g, '')
            .trim();
        return stripped === '';
    }
```

Then in `classify()` (lines 354-370), replace:

```javascript
    function classify(el, cs, hasDirectText, hasElemChildren) {
        if (el.tagName === 'svg')                            return 'raster';
        // <img> pixels can't be drawn natively → rasterize the rendered box.
        if (el.tagName === 'IMG')                            return 'raster';
        if (cs.filter && cs.filter !== 'none') {
            // Leaf text whose ONLY filter is drop-shadow(s) (a glow/soft shadow)
            // stays NATIVE: Figma renders the glow as a DROP_SHADOW effect and the
            // text remains editable (animatable in AE). A container-level (or
            // leaf) filter that is ONLY brightness() also stays NATIVE: it's a
            // linear R/G/B multiply, baked into each descendant's own color by
            // styleSnapshot/extractRuns (see visit()'s brightnessMul threading)
            // instead of rasterizing the whole subtree — keeps e.g. a dimmed
            // card's icon/text/shape as separate editable Figma layers. Any
            // richer/combined filter (blur, hue-rotate, …) still rasterizes.
            const keepNative = (filterIsOnlyDropShadow(cs.filter) && hasDirectText && !hasElemChildren)
                             || parseBrightnessOnly(cs.filter) !== null;
            if (!keepNative)                                 return 'raster';
        }
```

with:

```javascript
    function classify(el, cs, hasDirectText, hasElemChildren) {
        if (el.tagName === 'svg')                            return 'raster';
        // <img> pixels can't be drawn natively → rasterize the rendered box.
        if (el.tagName === 'IMG')                            return 'raster';
        if (cs.filter && cs.filter !== 'none') {
            // A filter made up ONLY of blur()/drop-shadow() (any mix) stays
            // NATIVE — both map onto Figma effects (LAYER_BLUR/DROP_SHADOW),
            // for leaf or container alike (see filterIsBlurAndOrDropShadow).
            // A container-level (or leaf) filter that is ONLY brightness()
            // also stays NATIVE: it's a linear R/G/B multiply, baked into
            // each descendant's own color by styleSnapshot/extractRuns (see
            // visit()'s brightnessMul threading) instead of rasterizing the
            // whole subtree. Any richer/combined filter (hue-rotate,
            // contrast, blur/drop-shadow mixed with something else, …) still
            // rasterizes.
            const keepNative = filterIsBlurAndOrDropShadow(cs.filter)
                             || parseBrightnessOnly(cs.filter) !== null;
            if (!keepNative)                                 return 'raster';
        }
```

(Everything below this block in `classify()` — clipPath, mask, backgroundImage url(), gradient checks, 3D transform, rotation, border-triangle, image-clipping viewport — is untouched by this task.)

- [ ] **Step 4: Add `_parse_filter_blur` in Python**

In `agents/html_extractor.py`, right after `_parse_filter_drop_shadows` (ends at line 1256), add:

```python
def _parse_filter_blur(filter_str: str) -> float | None:
    """Parse `filter: blur(Npx)` → radius in px, or None if absent."""
    if not filter_str or filter_str == "none":
        return None
    m = re.search(r"blur\(([\d.]+)px\)", filter_str)
    return float(m[1]) if m else None
```

- [ ] **Step 5: Wire the new effects into `_emit_native_element`**

In `agents/html_extractor.py`, replace (lines 1418-1421):

```python
    bf = cs.get("backdropFilter", "")
    bm = re.search(r"blur\(([\d.]+)px\)", bf)
    if bm:
        effects.append({"type": "BACKGROUND_BLUR", "radius": float(bm[1])})
```

with:

```python
    bf = cs.get("backdropFilter", "")
    bm = re.search(r"blur\(([\d.]+)px\)", bf)
    if bm:
        effects.append({"type": "BACKGROUND_BLUR", "radius": float(bm[1])})
    # filter:blur()/drop-shadow() map to native effects — classify() only let
    # a pure blur/drop-shadow filter reach here (see filterIsBlurAndOrDropShadow).
    # SKIP this for a PURE TEXT LEAF (direct text, no element children): its
    # filter-effects are attached to the TEXT node instead (see text_effects
    # below, Step 6) — otherwise a phantom background rectangle/frame would
    # appear with the same rectangular DROP_SHADOW/LAYER_BLUR, which CSS never
    # renders for filter:blur()/drop-shadow() on plain text (those are
    # glyph-shaped, not box-shaped).
    is_pure_text_leaf = bool(raw.get("runs")) and not raw.get("hasElementChildren")
    if not is_pure_text_leaf:
        filter_str = cs.get("filter", "")
        filter_blur_radius = _parse_filter_blur(filter_str)
        if filter_blur_radius is not None:
            effects.append({"type": "LAYER_BLUR", "radius": filter_blur_radius})
        effects.extend(_parse_filter_drop_shadows(filter_str))
```

- [ ] **Step 6: Extend the TEXT node's own effects to include blur, not just drop-shadow**

In `agents/html_extractor.py`, inside the `if has_text:` block, replace (currently):

```python
        # filter:drop-shadow (glow/soft shadow) → native DROP_SHADOW on the text node
        # (box-shadow stays on the emitted shape, if any). Lets glowing text stay
        # editable instead of being rasterized.
        text_effects = _parse_filter_drop_shadows(cs.get("filter", ""))
```

with:

```python
        # filter:blur()/drop-shadow() (glow/soft shadow) → native LAYER_BLUR/
        # DROP_SHADOW on the text node (box-shadow stays on the emitted shape,
        # if any). Lets blurred/glowing text stay editable instead of being
        # rasterized. (Shape-level effects skip this for a pure text leaf —
        # see the `is_pure_text_leaf` guard above — so it isn't duplicated.)
        text_filter_str = cs.get("filter", "")
        text_effects = _parse_filter_drop_shadows(text_filter_str)
        text_filter_blur_radius = _parse_filter_blur(text_filter_str)
        if text_filter_blur_radius is not None:
            text_effects.append({"type": "LAYER_BLUR", "radius": text_filter_blur_radius})
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_native_effects.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 8: Run the full test suite to confirm no regression**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass (the pre-existing ~19 tests plus the 7 new ones). Real-scene tests may SKIP if `input/*.html` isn't present locally — that's expected, not a failure.

- [ ] **Step 9: Commit**

```bash
git add agents/html_extractor.py tests/test_native_effects.py
git commit -m "$(cat <<'EOF'
feat: filter blur()/drop-shadow() map to native LAYER_BLUR/DROP_SHADOW

figma-mcp-go already supports both effect types (confirmed via live tool
schema) but classify() only kept drop-shadow-only text native and
rasterized everything else combined with any filter. Now any element
(leaf or container) whose filter is purely blur()/drop-shadow() (in any
combination) stays native instead of rasterizing.
EOF
)"
```

---

### Task 2: Gradient on shape/text → approximated native solid fill

**Files:**
- Modify: `agents/html_extractor.py` imports (add `colorsys`)
- Modify: `agents/html_extractor.py:354-423` (`classify()` gradient branches + new params)
- Modify: `agents/html_extractor.py:572-682` area (`visit()`'s call to `classify()`)
- Modify: `agents/html_extractor.py:1179-1204` area (add `_blend_gradient_to_solid` after `_parse_gradient`)
- Modify: `agents/html_extractor.py:1291-1332` (`_build_text_runs`)
- Modify: `agents/html_extractor.py:1344-1360` (`_emit_native_element` signature + Fills block)
- Modify: `agents/html_extractor.py:2134` (`_emit_native_element` call site)
- Test: `tests/test_gradient_approximation.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gradient_approximation.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_gradient_approximation.py -v`
Expected: `test_small_shape_gradient_becomes_solid_with_darker_bias`, `test_gradient_text_becomes_native_solid_text`, `test_three_stop_gradient_ignores_middle_stop` FAIL (currently raster). `test_full_frame_gradient_still_rasters` and `test_conic_gradient_still_rasters` currently PASS (already raster) — confirm they still pass after implementation.

- [ ] **Step 3: Add `colorsys` import**

In `agents/html_extractor.py`, in the import block near the top, replace:

```python
import argparse
import json
import math
import os
import re
import sys
```

with:

```python
import argparse
import colorsys
import json
import math
import os
import re
import sys
```

- [ ] **Step 4: Add `isSimpleGradientBg` JS helper and update `classify()`'s gradient branches**

In `agents/html_extractor.py`, right after the `filterIsBlurAndOrDropShadow` function added in Task 1 (before `classify()`), add:

```javascript
    // True when `backgroundImage` is EXACTLY one linear-gradient(...) or
    // radial-gradient(...) call (no stacked layers, no conic/repeating) —
    // mirrors the Python-side `_parse_gradient()` regex exactly so
    // classify()'s native/raster decision never disagrees with what the
    // color-blend step can actually parse.
    function isSimpleGradientBg(backgroundImage) {
        return /^(linear|radial)-gradient\(.+\)$/s.test((backgroundImage || '').trim());
    }
```

Then in `classify()`, replace (the gradient branches, currently):

```javascript
        if (cs.backgroundImage.includes('url('))             return 'raster';
        // background-clip:text + transparent fill → gradient text, must raster
        if ((cs.webkitTextFillColor === 'rgba(0, 0, 0, 0)' || cs.webkitTextFillColor === 'transparent')
            && cs.backgroundImage.includes('gradient'))      return 'raster';
        // Pure leaf with gradient bg (no element children) → raster whole element
        if (cs.backgroundImage.includes('gradient') && !hasElemChildren) return 'raster';
```

with:

```javascript
        if (cs.backgroundImage.includes('url('))             return 'raster';
        // background-clip:text + transparent fill → gradient text. A single
        // simple linear/radial gradient stays NATIVE — the text keeps its
        // runs/editability and gets ONE approximated solid color (see
        // _blend_gradient_to_solid in Python). Conic/multi-layer/repeating
        // gradients (isSimpleGradientBg fails) still rasterize.
        if ((cs.webkitTextFillColor === 'rgba(0, 0, 0, 0)' || cs.webkitTextFillColor === 'transparent')
            && cs.backgroundImage.includes('gradient')
            && !isSimpleGradientBg(cs.backgroundImage))      return 'raster';
        // Pure leaf with gradient bg (no element children): a full-frame
        // background (>=95% of viewport in both dimensions — same threshold
        // spirit as the Python-side _is_full_frame_bg) stays raster, so an
        // atmospheric/video-like background keeps full fidelity. A smaller
        // simple linear/radial gradient shape goes NATIVE with one
        // approximated solid fill instead. Conic/multi-layer gradients still
        // rasterize regardless of size.
        if (cs.backgroundImage.includes('gradient') && !hasElemChildren) {
            const isFullFrameBg = w >= window.innerWidth * 0.95 && h >= window.innerHeight * 0.95;
            if (!isSimpleGradientBg(cs.backgroundImage) || isFullFrameBg) return 'raster';
        }
```

Then update `classify()`'s parameter list (function signature only — body already updated above) from:

```javascript
    function classify(el, cs, hasDirectText, hasElemChildren) {
```

to:

```javascript
    function classify(el, cs, hasDirectText, hasElemChildren, w, h) {
```

- [ ] **Step 5: Pass `w, h` at the `classify()` call site**

In `agents/html_extractor.py`'s `visit()` function, replace:

```javascript
            const klass = classify(el, cs, hasDirectText, hasElementChildren);
```

with:

```javascript
            const klass = classify(el, cs, hasDirectText, hasElementChildren, w, h);
```

(`w` and `h` are already computed just above this line in `visit()` — no new variables needed.)

- [ ] **Step 6: Add `_blend_gradient_to_solid` in Python**

In `agents/html_extractor.py`, right after `_parse_gradient` (ends at line 1204), add:

```python
def _blend_gradient_to_solid(stops: list[dict]) -> dict:
    """Approximate a multi-stop gradient as ONE solid color: 70% the darkest
    stop + 30% the lightest stop (by HSL lightness), so the result reads as a
    base tone a user can add a lighter highlight to when editing. With only 2
    lightness-distinct stops this is exact; with 3+ stops the two
    lightness-extreme stops are used and any middle stops are ignored."""
    def lightness(stop):
        c = stop["color"]
        return colorsys.rgb_to_hls(c["r"], c["g"], c["b"])[1]
    darkest = min(stops, key=lightness)["color"]
    lightest = max(stops, key=lightness)["color"]
    return {
        "r": round(darkest["r"] * 0.7 + lightest["r"] * 0.3, 4),
        "g": round(darkest["g"] * 0.7 + lightest["g"] * 0.3, 4),
        "b": round(darkest["b"] * 0.7 + lightest["b"] * 0.3, 4),
        "a": round(darkest["a"] * 0.7 + lightest["a"] * 0.3, 4),
    }
```

- [ ] **Step 7: Thread `warnings` into `_emit_native_element` and `_build_text_runs`, wire the gradient-shape blend**

In `agents/html_extractor.py`, replace the `_emit_native_element` signature:

```python
def _emit_native_element(raw: dict, uid_to_id: dict[str, str], elements_out: list[dict], assets_dir: str) -> None:
```

with:

```python
def _emit_native_element(raw: dict, uid_to_id: dict[str, str], elements_out: list[dict], assets_dir: str, warnings: list[str]) -> None:
```

Replace the line that calls `_build_text_runs` (currently `runs = _build_text_runs(raw.get("runs")) if raw.get("runs") else None`) with:

```python
    runs = _build_text_runs(raw.get("runs"), warnings, raw["uid"]) if raw.get("runs") else None
```

Replace the Fills block (currently):

```python
    # ─── Fills ──────────────────────────────────────────────────────────────
    # Gradient bg is rasterized into a bg-only PNG child (handled below);
    # at this level, only SOLID bg color reaches `fills`.
    fills = []
    bg_color = _color_to_rgba(cs.get("backgroundColor", ""))
    has_gradient_bg = bool(raw.get("_bg_asset_filename"))
    if not has_gradient_bg and bg_color:
        fills.append({"type": "SOLID", "color": bg_color})
```

with:

```python
    # ─── Fills ──────────────────────────────────────────────────────────────
    # Gradient bg on a CONTAINER (with children) is rasterized into a bg-only
    # PNG child (handled below); at this level, only SOLID bg color reaches
    # `fills`. A gradient on a LEAF (no children) was already approved for
    # native by classify() (isSimpleGradientBg + not full-frame) —
    # approximate it to one solid color instead of a PNG, trading exact
    # fidelity for an editable native fill. EXCEPTION: a gradient-clip-text
    # leaf (direct text + transparent text-fill-color) uses its gradient only
    # to color the glyphs via _build_text_runs below — it must NOT ALSO get a
    # solid background shape painted behind the (invisible-box) text.
    fills = []
    bg_color = _color_to_rgba(cs.get("backgroundColor", ""))
    has_gradient_bg = bool(raw.get("_bg_asset_filename"))
    bg_image = cs.get("backgroundImage", "")
    is_gradient_clip_text = bool(raw.get("runs")) and cs.get("webkitTextFillColor", "") in ("rgba(0, 0, 0, 0)", "transparent")
    if not has_gradient_bg and not raw.get("hasElementChildren") and not is_gradient_clip_text and "gradient" in bg_image:
        grad = _parse_gradient(bg_image)
        if grad:
            solid = _blend_gradient_to_solid(grad["stops"])
            fills.append({"type": "SOLID", "color": solid})
            warnings.append(
                f"approximated gradient fill on {raw['uid']} as solid "
                f"rgb({round(solid['r']*255)}, {round(solid['g']*255)}, {round(solid['b']*255)}) "
                f"(native, was raster; darker-biased 70/30 blend of gradient stops)"
            )
    elif not has_gradient_bg and bg_color:
        fills.append({"type": "SOLID", "color": bg_color})
```

**Why this matters:** without the `is_gradient_clip_text` exclusion, a
`background-clip:text` heading (e.g. `GRADIENT_TEXT_HTML` in the tests below)
would ALSO satisfy `not hasElementChildren and "gradient" in bg_image`, causing
`has_visual` (line ~1425, unchanged) to become true and a spurious solid
rectangle to be emitted behind the text — something the original CSS never
renders (the gradient is only visible through the glyphs). The
`test_gradient_text_becomes_native_solid_text` test below asserts no such
shape appears.

- [ ] **Step 8: Wire the gradient-text blend in `_build_text_runs`**

In `agents/html_extractor.py`, replace the `_build_text_runs` signature:

```python
def _build_text_runs(raw_runs: list[dict]) -> list[dict]:
```

with:

```python
def _build_text_runs(raw_runs: list[dict], warnings: list[str], uid: str) -> list[dict]:
```

Replace the gradient-text detection block inside it (currently):

```python
        # Detect background-clip:text (gradient text fill)
        bg = r.get("backgroundImage", "")
        text_fill_transparent = r.get("webkitTextFillColor", "") in ("rgba(0, 0, 0, 0)", "transparent")
        bg_clip = r.get("backgroundClip", "")
        fills = None
        if text_fill_transparent and "gradient" in bg and bg_clip == "text":
            grad = _parse_gradient(bg)
            if grad:
                fills = [grad]
```

with:

```python
        # Detect background-clip:text (gradient text fill) — classify() only
        # let a text element with a SIMPLE linear/radial gradient reach here
        # natively; approximate it to one solid color (editable text kept).
        bg = r.get("backgroundImage", "")
        text_fill_transparent = r.get("webkitTextFillColor", "") in ("rgba(0, 0, 0, 0)", "transparent")
        bg_clip = r.get("backgroundClip", "")
        fills = None
        if text_fill_transparent and "gradient" in bg and bg_clip == "text":
            grad = _parse_gradient(bg)
            if grad:
                solid = _blend_gradient_to_solid(grad["stops"])
                fills = [{"type": "SOLID", "color": solid}]
                warnings.append(
                    f"approximated gradient text fill on {uid} as solid "
                    f"rgb({round(solid['r']*255)}, {round(solid['g']*255)}, {round(solid['b']*255)}) "
                    f"(native, was raster; darker-biased 70/30 blend of gradient stops)"
                )
```

- [ ] **Step 9: Update the `_emit_native_element` call site**

In `agents/html_extractor.py`, replace:

```python
            _emit_native_element(raw, uid_to_id, elements_spec, assets_dir)
```

with:

```python
            _emit_native_element(raw, uid_to_id, elements_spec, assets_dir, warnings)
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_gradient_approximation.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 11: Run the full test suite to confirm no regression**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass (pre-existing + Task 1's 5 + this task's 5). Real-scene tests may SKIP if `input/*.html` isn't present locally.

- [ ] **Step 12: Commit**

```bash
git add agents/html_extractor.py tests/test_gradient_approximation.py
git commit -m "$(cat <<'EOF'
feat: approximate simple gradients on shapes/text to a native solid fill

set_fills only supports solid colors (confirmed via live tool schema) so
true native gradient fill is a hard limit. Instead of rastering every
gradient shape/text, a simple linear/radial gradient under 95% of the
frame now becomes one native SOLID fill (70% darkest-stop + 30%
lightest-stop by HSL lightness, so it reads as a base tone a user can
add a lighter highlight to when editing). Full-frame backgrounds,
conic gradients, and multi-layer backgrounds are unchanged (still
raster). Each approximation logs a warning so Bước 3 QC doesn't
mistake the color difference for a bug.
EOF
)"
```

---

### Task 3: Real-scene regression checks

**Files:**
- Modify: `tests/test_real_scenes.py`

- [ ] **Step 1: Add real-scene checks and fix the now-stale comment**

In `tests/test_real_scenes.py`, replace the comment inside `test_real_content_survives_in_scene_9` (currently):

```python
    # (scene_9 has no NATIVE text: its heading is gradient-clip text that
    # rasterizes by CLAUDE.md's documented design, so we assert real *content*
    # layers survive, not text specifically.)
```

with:

```python
    # (Historically scene_9 had no NATIVE text: its heading used gradient-clip
    # styling that rasterized by design. Since the maximize-native-minimize-
    # raster change (topic #2), a simple linear/radial gradient-clip heading
    # goes native instead — see test_real_scene_gradient_text_becomes_native
    # below — so this check still asserts real *content* layers survive by
    # name, without assuming raster-only.)
```

Then append two new test functions at the end of the file:

```python
def test_real_scene_gains_native_blur_or_glow_effects(tmp_path):
    # scene_9 has several filter:blur()/drop-shadow() decorative elements that
    # used to rasterize — after the maximize-native change they should carry
    # native LAYER_BLUR/DROP_SHADOW effects instead.
    spec = _spec_for("scene_9", tmp_path)
    effect_types = {fx.get("type") for e in spec["elements"] for fx in e.get("effects", [])}
    assert effect_types & {"LAYER_BLUR", "DROP_SHADOW"}, \
        f"scene_9: expected at least one native LAYER_BLUR/DROP_SHADOW effect, got {effect_types}"


def test_real_scene_gradient_text_becomes_native(tmp_path):
    # scene_9's heading uses background-clip:text with a gradient — this used
    # to force it to raster; it should now survive as an editable native text
    # element with an approximated solid fill, plus a warning documenting the
    # trade-off.
    spec = _spec_for("scene_9", tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert texts, "scene_9's gradient-clip heading should now be a native text element"
    assert any("approximated gradient text fill" in w for w in spec["warnings"])
```

- [ ] **Step 2: Run the new tests**

Run: `.venv/bin/python -m pytest tests/test_real_scenes.py -v`
Expected: PASS if `input/scene_9.html` is present locally (dev machine); SKIP (not FAIL) if absent (fresh clone/CI).

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass or skip (none fail).

- [ ] **Step 4: Commit**

```bash
git add tests/test_real_scenes.py
git commit -m "$(cat <<'EOF'
test: real-scene regression for native blur/glow and gradient-text

Confirms scene_9's decorative blur/glow elements now carry native
LAYER_BLUR/DROP_SHADOW effects and its gradient-clip heading survives
as native editable text, and updates a comment that assumed
gradient-clip text always rasterizes (no longer true post topic #2).
EOF
)"
```

---

### Task 4: Update CLAUDE.md's known-limitations table

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the limitations table**

In `CLAUDE.md`, replace the table row block (currently):

```markdown
| Gradient fill native | ✗ Không hỗ trợ → rasterize toàn bộ |
| Image fill native | ✗ → `import_image` tạo image node |
| Per-run text styling (inline bold span, color span) | ✗ → concat thành 1 text node, dùng style của run đầu, warning |
| Conic gradient, clip-path, mask, CSS filter | → raster fallback |
| SVG (vector) | → raster PNG, không editable trong Figma |
| Background-clip: text + transparent fill (gradient text) | → raster |
```

with:

```markdown
| Gradient fill native | ✗ Không hỗ trợ thật (xác nhận qua schema `set_fills`: chỉ nhận solid hex). Gradient **linear/radial đơn giản** trên shape/text nhỏ hơn 95% frame → xấp xỉ 1 màu solid (blend 70% stop đậm + 30% stop sáng theo lightness), giữ native/editable, có warning. Gradient phủ ≥95% frame, conic, hoặc nhiều lớp background chồng nhau → vẫn raster. |
| Image fill native | ✗ → `import_image` tạo image node |
| Per-run text styling (inline bold span, color span) | ✗ → concat thành 1 text node, dùng style của run đầu, warning |
| `filter: blur()` / `drop-shadow()` (kể cả kết hợp) | ✓ Native — map sang effect `LAYER_BLUR`/`DROP_SHADOW` (áp dụng cho leaf lẫn container). Filter khác (hue-rotate, contrast, kết hợp với thứ khác ngoài blur/drop-shadow, ...) vẫn raster. |
| Conic gradient, clip-path, mask | → raster fallback |
| SVG (vector) | → raster PNG, không editable trong Figma |
| Background-clip: text + transparent fill (gradient text) | Gradient linear/radial đơn giản → native (xấp xỉ màu solid, xem hàng Gradient fill native ở trên). Conic/nhiều lớp → raster. |
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: update known-limitations table for native blur/glow + gradient approx

Reflects topic #2 (maximize-native-minimize-raster): blur/drop-shadow
filters and simple linear/radial gradients on shapes/text are now
native instead of always rastering.
EOF
)"
```
