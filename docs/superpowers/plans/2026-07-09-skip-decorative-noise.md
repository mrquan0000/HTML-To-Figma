# Skip Decorative Noise Elements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect and drop swarms of tiny decorative particle/dust/mote elements during HTML extraction so they never reach the Figma spec, leaving only content worth animating.

**Architecture:** A structural-primary + keyword-booster detector added to `_EXTRACT_JS` (the Chromium DOM-walk in `agents/html_extractor.py`). When walking an element's children, a qualifying swarm (≥8 tiny textless leaves + keyword, OR ≥12 without keyword) is skipped before recursion, so those elements are never emitted and never rastered. Each drop emits one `warnings[]` entry. No other file changes — `figma_builder.py` consumes the filtered spec, and `render_html.py` intentionally stays faithful to raw HTML (QC image keeps particles).

**Tech Stack:** Python 3 + Playwright (Chromium), JS-in-page extraction. Tests: pytest calling `extract()` directly on HTML fixtures.

**Spec:** `docs/superpowers/specs/2026-07-09-skip-decorative-noise-design.md`

---

## File Structure

- `agents/html_extractor.py` — MODIFY `_EXTRACT_JS` (add detector helpers + swarm-skip in `visit()`'s recursion) and the Python `extract()` (merge JS warnings). Only file with production changes.
- `tests/conftest.py` — CREATE: pytest helper `run_extract(html, tmp_path)` that writes an HTML string to a temp file and calls `extract()`.
- `tests/test_decorative_noise.py` — CREATE: all behavior tests.
- `requirements-dev.txt` or venv install — add `pytest` (no test infra exists yet).

---

## Task 1: Test harness (pytest + extraction helper)

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_harness_smoke.py`
- Modify: install `pytest` into `.venv`

- [ ] **Step 1: Install pytest into the project venv**

Run:
```bash
.venv/bin/pip install pytest
```
Expected: `Successfully installed pytest-...`

- [ ] **Step 2: Create the extraction test helper**

Create `tests/conftest.py`:
```python
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
```

- [ ] **Step 3: Write a smoke test proving the harness works**

Create `tests/test_harness_smoke.py`:
```python
from conftest import run_extract


def test_extract_returns_text_element(tmp_path):
    html = """<!doctype html><html><head><style>
      body { margin:0; width:800px; height:200px; background:#111; }
      h1 { color:#fff; font-size:40px; }
    </style></head><body><h1>HELLO WORLD</h1></body></html>"""
    spec = run_extract(html, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert any("HELLO WORLD" in "".join(r["text"] for r in e.get("runs", []))
               for e in texts)
```

- [ ] **Step 4: Run the smoke test — verify it PASSES**

Run:
```bash
.venv/bin/python -m pytest tests/test_harness_smoke.py -v
```
Expected: PASS (proves `extract()` is callable from tests and returns a text element). If it fails on import, confirm `agents/html_extractor.py` imports cleanly.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/test_harness_smoke.py
git commit -m "test: add pytest extraction harness"
```

---

## Task 2: Swarm detection (drop tiny particle groups)

**Files:**
- Create: `tests/test_decorative_noise.py`
- Modify: `agents/html_extractor.py` (`_EXTRACT_JS` helpers + `visit()` recursion; Python `extract()` warning merge)

- [ ] **Step 1: Write the failing test — a particle swarm is dropped, real text kept, warning emitted**

Create `tests/test_decorative_noise.py`:
```python
from conftest import run_extract

# 20 tiny textless particle leaves inside a .particles container, plus one real
# heading whose class name (glowing-text) contains "glow" — must survive.
_PARTICLES = "\n".join(
    f'<div class="particle" style="top:{i*4}%;left:{(i*7)%100}%"></div>'
    for i in range(20)
)
SWARM_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:600px; background:#111; position:relative; }}
  h1 {{ color:#fff; font-size:40px; }}
  .particles {{ position:absolute; inset:0; }}
  .particle {{ position:absolute; width:4px; height:4px; border-radius:50%; background:gold; }}
</style></head><body>
  <h1 class="glowing-text">REAL HEADING</h1>
  <div class="particles">{_PARTICLES}</div>
</body></html>"""


def _tiny(elements):
    return [e for e in elements if e["width"] <= 12 and e["height"] <= 12]


def _has_text(spec, needle):
    return any(needle in "".join(r["text"] for r in e.get("runs", []))
               for e in spec["elements"] if e["type"] == "text")


def test_particle_swarm_dropped(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert _tiny(spec["elements"]) == [], "tiny particle leaves should be dropped"


def test_real_heading_survives_swarm(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert _has_text(spec, "REAL HEADING"), "glowing-text heading must be kept"


def test_swarm_drop_emits_warning(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert any("skipped decorative swarm" in w for w in spec["warnings"])
```

- [ ] **Step 2: Run the test — verify it FAILS**

Run:
```bash
.venv/bin/python -m pytest tests/test_decorative_noise.py -v
```
Expected: `test_particle_swarm_dropped` and `test_swarm_drop_emits_warning` FAIL (particles currently emitted as tiny ellipse elements; no warning). `test_real_heading_survives_swarm` may already pass.

- [ ] **Step 3: Add detector helpers to `_EXTRACT_JS`**

In `agents/html_extractor.py`, find the line `const out = [];` (currently ~line 429, immediately after `const bodyCS = ...`). Replace:
```javascript
    const out = [];
    let docOrder = 0;
```
with:
```javascript
    const out = [];
    const skipWarnings = [];
    let docOrder = 0;

    // ─── Decorative-noise swarm detection ──────────────────────────────────
    // Drop swarms of tiny textless "particle/dust/mote" leaves (they explode
    // into piles of dead Figma layers never animated in AE). Structure decides;
    // the keyword list only raises confidence and NEVER fires on its own, so a
    // real element like `.glowing-text` (has text) can't be dropped by name.
    const NOISE_WORDS = ['particle','mote','dust','spark','bokeh','snow',
                         'ember','fleck','speck','twinkle'];

    function noiseMatch(el) {
        const cls = (el.getAttribute('class') || '').toLowerCase();
        return NOISE_WORDS.some(w => cls.includes(w));
    }

    // A leaf (no real element children), holding no text, ≤12px in both dims.
    function isTinyTextlessLeaf(el) {
        for (const c of el.children) {
            if (!SKIP_TAGS.has(c.tagName) && c.tagName !== 'BR') return false;
        }
        for (const n of el.childNodes) {
            if (n.nodeType === 3 && n.textContent.trim()) return false;
        }
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return false;
        return Math.max(r.width, r.height) <= 12;
    }

    // Returns {set, count, keyword} if `parent`'s direct children hold a
    // droppable swarm, else null. A swarm = the largest group of near-equal-size
    // (delta ≤2px) tiny-textless-leaf children, dropped when count≥12, or count≥8
    // with a keyword match on the parent or a group member.
    function decorativeSwarmChildren(parent) {
        const cands = [];
        for (const c of parent.children) {
            if (SKIP_TAGS.has(c.tagName) || c.tagName === 'BR') continue;
            if (isTinyTextlessLeaf(c)) cands.push(c);
        }
        if (cands.length < 8) return null;
        let best = [];
        for (const seed of cands) {
            const sr = seed.getBoundingClientRect();
            const grp = cands.filter(c => {
                const r = c.getBoundingClientRect();
                return Math.abs(r.width - sr.width) <= 2 && Math.abs(r.height - sr.height) <= 2;
            });
            if (grp.length > best.length) best = grp;
        }
        if (best.length < 8) return null;
        const keyword = noiseMatch(parent) || best.some(noiseMatch);
        const drop = best.length >= 12 || (best.length >= 8 && keyword);
        if (!drop) return null;
        return { set: new Set(best), count: best.length, keyword };
    }
    // ───────────────────────────────────────────────────────────────────────
```

- [ ] **Step 4: Skip the swarm in `visit()`'s recursion**

In `agents/html_extractor.py`, find the recursion loop at the end of `visit()` (currently ~line 572):
```javascript
        for (const child of el.children) {
            visit(child, parentUid, visualParent, effectiveBrightness);
        }
```
Replace it with:
```javascript
        const swarm = decorativeSwarmChildren(el);
        if (swarm) {
            const label = el.getAttribute('class')
                ? '.' + el.getAttribute('class').split(/\s+/)[0]
                : '<' + tag + '>';
            const how = swarm.keyword
                ? 'structural+keyword'
                : 'structural-only, N=' + swarm.count;
            skipWarnings.push('skipped decorative swarm: ' + swarm.count
                + ' leaves under ' + label + ' (match: ' + how + ')');
        }
        for (const child of el.children) {
            if (swarm && swarm.set.has(child)) continue;
            visit(child, parentUid, visualParent, effectiveBrightness);
        }
```

- [ ] **Step 5: Return the warnings from `_EXTRACT_JS`**

In `agents/html_extractor.py`, find the return object at the end of `_EXTRACT_JS` (currently ~line 587):
```javascript
    return {
        elements: out,
        frameWidth: Math.round(frameRect.width),
        frameHeight: Math.round(frameRect.height),
        bodyBg: bodyCS.backgroundColor,
        rootBg: rootCS.backgroundColor,
        rootBgImage: rootCS.backgroundImage,
    };
```
Add the `warnings` field:
```javascript
    return {
        elements: out,
        warnings: skipWarnings,
        frameWidth: Math.round(frameRect.width),
        frameHeight: Math.round(frameRect.height),
        bodyBg: bodyCS.backgroundColor,
        rootBg: rootCS.backgroundColor,
        rootBgImage: rootCS.backgroundImage,
    };
```

- [ ] **Step 6: Merge JS warnings into the spec on the Python side**

In `agents/html_extractor.py`, find (currently ~line 1554):
```python
        result = page.evaluate(_EXTRACT_JS)
        raw_elements = result["elements"]
```
Insert the merge right after `raw_elements` is read:
```python
        result = page.evaluate(_EXTRACT_JS)
        raw_elements = result["elements"]
        warnings.extend(result.get("warnings", []))
```
(`warnings` is the list already declared at the top of `extract()` ~line 1493.)

- [ ] **Step 7: Run the test — verify it PASSES**

Run:
```bash
.venv/bin/python -m pytest tests/test_decorative_noise.py -v
```
Expected: all three tests PASS.

- [ ] **Step 8: Commit**

```bash
git add agents/html_extractor.py tests/test_decorative_noise.py
git commit -m "feat(html_extractor): drop decorative particle swarms during extraction"
```

---

## Task 3: Threshold behavior (false-positive guard + structural-only)

**Files:**
- Modify: `tests/test_decorative_noise.py`

- [ ] **Step 1: Add the guard + threshold tests**

Append to `tests/test_decorative_noise.py`:
```python
# --- Real UI must be KEPT: small repeated leaves below the keyword threshold. ---
# 5-star rating + 6 carousel dots. Same 10px size → they group together (N=11),
# but there's NO keyword match (star/dot are NOT in the vocab) so the drop needs
# N≥12 — 11 is kept.
_STARS = "\n".join(f'<i class="star" style="left:{i*12}px"></i>' for i in range(5))
_DOTS = "\n".join(f'<i class="dot" style="left:{i*12}px"></i>' for i in range(6))
REAL_UI_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .star, .dot {{ position:absolute; width:10px; height:10px; border-radius:50%;
                 background:#fff; top:20px; }}
  .dots {{ position:absolute; top:80px; }}
</style></head><body>
  <div class="rating">{_STARS}</div>
  <div class="dots">{_DOTS}</div>
</body></html>"""


def test_real_ui_kept_below_threshold(tmp_path):
    spec = run_extract(REAL_UI_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 11, "5 stars + 6 dots (N=11, no keyword) must all be kept"
    assert not any("skipped decorative swarm" in w for w in spec["warnings"])


# --- 9 keyword-less tiny leaves (8–11 range, no keyword) → KEPT. ---
_NINE = "\n".join(f'<span class="fx" style="left:{i*20}px"></span>' for i in range(9))
NINE_NOKW_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .fx {{ position:absolute; width:5px; height:5px; background:#0f0; top:20px; }}
</style></head><body>{_NINE}</body></html>"""


def test_nine_keywordless_leaves_kept(tmp_path):
    spec = run_extract(NINE_NOKW_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 9, "8–11 keyword-less tiny leaves are ambiguous → kept"


# --- 14 keyword-less tiny leaves (N≥12) → DROPPED by structure alone. ---
_FOURTEEN = "\n".join(f'<span class="fx" style="left:{i*20}px"></span>' for i in range(14))
FOURTEEN_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .fx {{ position:absolute; width:5px; height:5px; background:#0f0; top:20px; }}
</style></head><body>{_FOURTEEN}</body></html>"""


def test_fourteen_keywordless_leaves_dropped(tmp_path):
    spec = run_extract(FOURTEEN_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert tiny == [], "N≥12 tiny leaves are conclusive → dropped without keyword"


# --- 8 carousel dots with class "dot" → KEPT (dot excluded from vocab). ---
_EIGHT_DOTS = "\n".join(f'<i class="dot" style="left:{i*20}px"></i>' for i in range(8))
CAROUSEL_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .dot {{ position:absolute; width:8px; height:8px; border-radius:50%;
          background:#fff; top:20px; }}
</style></head><body>{_EIGHT_DOTS}</body></html>"""


def test_carousel_dots_kept(tmp_path):
    spec = run_extract(CAROUSEL_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 8, "'dot' is not a noise keyword; 8 dots (N<12) stay"
```

- [ ] **Step 2: Run the tests — verify they PASS**

Run:
```bash
.venv/bin/python -m pytest tests/test_decorative_noise.py -v
```
Expected: all tests PASS (the four new ones prove the thresholds protect real UI and that structure-alone still catches large keyword-less swarms).

- [ ] **Step 3: Commit**

```bash
git add tests/test_decorative_noise.py
git commit -m "test: threshold + false-positive guards for noise detection"
```

---

## Task 4: Real-scene integration verification

**Files:**
- Create: `tests/test_real_scenes.py`

- [ ] **Step 1: Write the real-scene regression test**

Create `tests/test_real_scenes.py`:
```python
"""Slower integration check against real particle-heavy scenes in input/.
Skips gracefully if a scene file isn't present."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from agents.html_extractor import extract  # noqa: E402


def _spec_for(scene, tmp_path):
    html = _ROOT / "input" / f"{scene}.html"
    if not html.exists():
        pytest.skip(f"{scene}.html not present")
    return extract(str(html), assets_dir=str(tmp_path / "assets"))


@pytest.mark.parametrize("scene", ["scene_9", "scene_24"])
def test_no_particle_elements_emitted(scene, tmp_path):
    spec = _spec_for(scene, tmp_path)
    # No emitted element should carry a particle/dust class token in its name,
    # and the swarm-skip warning should be present.
    names = " ".join(e.get("name", "").lower() for e in spec["elements"])
    assert "particle" not in names, f"{scene}: particle layers should be gone"
    assert "/dust" not in names and "[dust" not in names, f"{scene}: dust layers gone"
    assert any("skipped decorative swarm" in w for w in spec["warnings"]), \
        f"{scene}: expected a swarm-skip warning"


def test_scene_9_keeps_real_content(tmp_path):
    spec = _spec_for("scene_9", tmp_path)
    # scene_9 has real text content — at least some text elements must remain.
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert len(texts) > 0, "scene_9 real headings/labels must survive"
```

- [ ] **Step 2: Run the integration test — verify it PASSES**

Run:
```bash
.venv/bin/python -m pytest tests/test_real_scenes.py -v
```
Expected: PASS for scene_9 and scene_24 (particle/dust layers gone, warning present, real text kept). If a scene file is absent, that case SKIPS rather than fails.

- [ ] **Step 3: Full visual spot-check (manual, no code)**

Run the real pipeline on one scene and eyeball the extractor output count:
```bash
.venv/bin/python agents/html_extractor.py --input input/scene_9.html --output output/scene_9_spec.json
```
Confirm in the printed summary / `output/scene_9_spec.json` that the `[particle/ellipse]` layers are absent and a `skipped decorative swarm` warning is in `warnings`. Do NOT build to Figma here — that's a separate, user-initiated step per CLAUDE.md.

- [ ] **Step 4: Commit**

```bash
git add tests/test_real_scenes.py
git commit -m "test: real-scene regression for decorative-noise skipping"
```

---

## Task 5: Full regression sweep

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run:
```bash
.venv/bin/python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 2: Confirm a non-particle scene is unchanged**

Extract a scene with NO particles (e.g. scene_11) and confirm no swarm warning appears and element count matches expectations:
```bash
.venv/bin/python agents/html_extractor.py --input input/scene_11.html --output output/scene_11_spec.json
```
Expected: no `skipped decorative swarm` warning (scene_11 has no swarm), all content intact — proves the detector doesn't touch scenes without swarms.

- [ ] **Step 3: Final commit (if any output/doc tidying needed)**

```bash
git add -A
git commit -m "chore: decorative-noise skipping verified across scenes" --allow-empty
```

---

## Self-Review Notes (addressed)

- **Spec coverage:** detection algorithm (Task 2), hybrid keyword-as-booster (Task 2 + guard tests Task 3), Tier-1-only scope (only tiny-leaf swarms detected; Tier-2 overlays untouched because they're single large elements, never candidates), warnings (Task 2 step 5–6, asserted Task 2/4), pipeline placement in `_EXTRACT_JS` only (Task 2), `render_html.py` unchanged (no task touches it — intentional), verification (Tasks 4–5).
- **Known limitation (documented, not a bug):** if a swarm sits inside a container that is itself classified `raster` (e.g. a gradient-bg Tier-2 overlay with children), the container early-returns before the swarm-skip code and the particles bake into that one raster PNG. Acceptable: it's one layer, not a pile, and matches the "keep Tier-2" decision.
- **Type consistency:** `decorativeSwarmChildren` returns `{set, count, keyword}` used consistently in `visit()`. Python reads `result["warnings"]` matching JS `warnings:` key. Element fields `width`/`height`/`name`/`runs`/`type` match existing spec schema.
