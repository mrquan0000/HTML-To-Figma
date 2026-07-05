# Animation Peak-Freeze + Raster Bbox Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two extractor bugs found on scene_9 — a raster capture that crops absolutely-positioned descendants out of its bounding box, and an animation freeze that always jumps to an animation's absolute end even when that end is a deliberate "recede/dim" state rather than the visual peak — so Figma builds show every animated element at its own most-visible moment.

**Architecture:** Both fixes live entirely inside `agents/html_extractor.py`'s per-element JS evaluated via Playwright, plus a matching mirror in `utils/render_html.py` (the QC reference generator, which must stay symmetric with the extractor per existing convention). No new files, no new dependencies, no test framework introduced — verification uses tiny synthetic HTML fixtures run through the real `extract()` function (this project has no unit-test suite; its established practice is running the real pipeline against real/synthetic HTML and inspecting the resulting spec/PNG).

**Tech Stack:** Python 3.13, Playwright (sync API), the existing `agents/html_extractor.py` / `utils/render_html.py` scripts.

**Reference:** `docs/superpowers/specs/2026-07-05-animation-peak-freeze-design.md` (approved design this plan implements).

---

### Task 1: Raster bbox = union of container + all descendants

**Files:**
- Modify: `agents/html_extractor.py:1540-1546` (`_GET_BBOX_JS`, a local variable inside `extract()`)

- [ ] **Step 1: Write the fixture and failing verification script**

Create the scratch fixture (this directory is gitignored — safe scratch space):

```bash
mkdir -p output/_verify_scratch && cat > output/_verify_scratch/bbox_fixture.html <<'EOF'
<!DOCTYPE html>
<html><head><style>
  body { margin:0; background:#000; }
  .wrap { position:relative; display:flex; flex-direction:column; width:200px; filter: blur(1px); }
  .escaper { position:absolute; top:-50px; left:60px; width:80px; height:80px; background:#ff0000; }
  .inflow { width:100px; height:60px; background:#0000ff; }
</style></head>
<body>
  <div class="wrap">
    <div class="escaper"></div>
    <div class="inflow"></div>
  </div>
</body></html>
EOF
```

`.wrap` has its own `filter:blur` (forcing the whole subtree to rasterize as one flattened image, exactly like `.focal-wrapper` in scene_9), and contains `.escaper` — a `position:absolute` child positioned so it visually pokes 50px above `.wrap`'s own auto-computed height (60px, from `.inflow` alone, since absolutely-positioned children don't contribute to flex auto-sizing). This reproduces the scene_9 bug shape exactly.

Run the verification script:

```bash
.venv/bin/python3 <<'PYEOF'
from agents.html_extractor import extract
spec = extract("output/_verify_scratch/bbox_fixture.html", assets_dir="output/_verify_scratch/assets")
wrap_el = next(e for e in spec["elements"] if e["type"] == "image" and "wrap" in e["name"])
print("wrap element height:", wrap_el["height"])
assert wrap_el["height"] >= 140, f"expected height >= 140 (union must include the escaping child), got {wrap_el['height']}"
print("PASS")
PYEOF
```

- [ ] **Step 2: Run it, confirm it fails**

Expected output:
```
wrap element height: 98
AssertionError: expected height >= 140 (union must include the escaping child), got 98
```

This confirms the bug: the current bbox (`.wrap`'s own box, 60px content height + ~19px bleed top/bottom from the blur = 98) misses the `.escaper` child that pokes 50px above it.

- [ ] **Step 3: Fix `_GET_BBOX_JS`**

Current code at `agents/html_extractor.py:1540-1546`:

```python
        _GET_BBOX_JS = r"""
        (uid) => {
            const el = document.querySelector(`[data-extract-uid="${uid}"]`);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }
        """
```

Replace with:

```python
        _GET_BBOX_JS = r"""
        (uid) => {
            const el = document.querySelector(`[data-extract-uid="${uid}"]`);
            if (!el) return null;
            // Union of the target's own box and every descendant's box — a
            // position:absolute descendant (e.g. a pin icon inside a flex
            // wrapper) doesn't contribute to the wrapper's own auto-computed
            // size, so using only the target's own rect can crop it out of
            // the raster capture.
            const rects = [el, ...el.querySelectorAll('*')].map(e => e.getBoundingClientRect());
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            for (const r of rects) {
                if (r.width === 0 || r.height === 0) continue;
                x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
                x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
            }
            if (x0 === Infinity) return null;
            return {x: x0, y: y0, w: x1 - x0, h: y1 - y0};
        }
        """
```

- [ ] **Step 4: Re-run the verification script, confirm it passes**

```bash
.venv/bin/python3 <<'PYEOF'
from agents.html_extractor import extract
spec = extract("output/_verify_scratch/bbox_fixture.html", assets_dir="output/_verify_scratch/assets")
wrap_el = next(e for e in spec["elements"] if e["type"] == "image" and "wrap" in e["name"])
print("wrap element height:", wrap_el["height"])
assert wrap_el["height"] >= 140, f"expected height >= 140 (union must include the escaping child), got {wrap_el['height']}"
print("PASS")
PYEOF
```

Expected output:
```
wrap element height: 148
PASS
```

- [ ] **Step 5: Commit**

```bash
git add agents/html_extractor.py
git commit -m "$(cat <<'EOF'
fix(html_extractor): raster bbox includes absolutely-positioned descendants

_GET_BBOX_JS only measured the raster target's own getBoundingClientRect(),
missing position:absolute children that escape the target's auto-computed
layout box (e.g. a pin icon inside a flex wrapper) — cropping them out of
the captured PNG. Now unions the target's box with every descendant's box.
EOF
)"
```

---

### Task 2: Per-animation opacity-peak seeking (extractor)

**Files:**
- Modify: `agents/html_extractor.py:1245-1271` (the freeze block inside `extract()`)

- [ ] **Step 1: Extract the current freeze block into a named constant (no behavior change yet)**

Current code at `agents/html_extractor.py:1245-1271`:

```python
        # Step 1 (JS): Jump every animation to its OWN natural end state.
        # Web Animations API .finish() advances each CSS @keyframes animation to
        # its individual 100% keyframe, regardless of its duration — no fixed wait
        # needed. Each graphic ends at its own end time, not a shared 2s cutoff.
        page.evaluate("""() => {
            // CSS @keyframes / WAAPI: finish each animation at its own end time
            for (const a of document.getAnimations()) {
                try {
                    // Force fill:both so the 100% keyframe state persists after finish
                    if (a.effect) a.effect.updateTiming({ fill: 'both' });
                    a.finish();  // jump to this animation's own end state
                    a.pause();   // lock there
                } catch (e) {}
            }
            // GSAP: advance its internal timeline far into the future
            try {
                if (window.gsap) window.gsap.globalTimeline.seek(99999, false).pause();
            } catch (e) {}
            // anime.js: seek each tween to its own duration
            try {
                if (window.anime)
                    window.anime.running.slice().forEach(a => { a.seek(a.duration); a.pause(); });
            } catch (e) {}
            // Freeze requestAnimationFrame so no loop can mutate state after this point
            window.requestAnimationFrame = function () { return 0; };
        }""")
        page.wait_for_timeout(200)  # let browser paint the finished states
```

Replace with (identical logic, just moved to a named module-level constant so later steps can diff cleanly):

```python
        # Step 1 (JS): Jump every animation to its OWN peak-opacity moment (or
        # natural end state if it has no opacity keyframe). See _FREEZE_ANIMATIONS_JS.
        page.evaluate(_FREEZE_ANIMATIONS_JS)
        page.wait_for_timeout(200)  # let browser paint the frozen states
```

Add the new module-level constant right after `_DETECT_DESIGN_JS` (which currently ends at line 578, right before the `# CSS parsers (Python side)` section comment at line 581). Insert this between them:

```python
_FREEZE_ANIMATIONS_JS = r"""
() => {
    // CSS @keyframes / WAAPI: finish each animation at its own end time
    for (const a of document.getAnimations()) {
        try {
            // Force fill:both so the 100% keyframe state persists after finish
            if (a.effect) a.effect.updateTiming({ fill: 'both' });
            a.finish();  // jump to this animation's own end state
            a.pause();   // lock there
        } catch (e) {}
    }
    // GSAP: advance its internal timeline far into the future
    try {
        if (window.gsap) window.gsap.globalTimeline.seek(99999, false).pause();
    } catch (e) {}
    // anime.js: seek each tween to its own duration
    try {
        if (window.anime)
            window.anime.running.slice().forEach(a => { a.seek(a.duration); a.pause(); });
    } catch (e) {}
    // Freeze requestAnimationFrame so no loop can mutate state after this point
    window.requestAnimationFrame = function () { return 0; };
}
"""
```

Insert this immediately after `_DETECT_DESIGN_JS`'s closing `"""` (line 578), keeping the existing blank lines and the `# CSS parsers (Python side)` section comment (line 581) unchanged right after it.

Verify nothing broke (this step is a pure refactor, no behavior change expected):

```bash
.venv/bin/python3 <<'PYEOF'
from agents.html_extractor import extract
spec = extract("output/_verify_scratch/freeze_fixture.html", assets_dir="output/_verify_scratch/assets2")
for e in spec["elements"]:
    print(e["type"], e.get("name"), "opacity=", e.get("opacity"))
PYEOF
```

(The fixture file doesn't exist yet — create it now, it's also used by Step 2 below:)

```bash
cat > output/_verify_scratch/freeze_fixture.html <<'EOF'
<!DOCTYPE html>
<html><head><style>
@keyframes recede { 0% { opacity: 1; } 100% { opacity: 0.22; } }
@keyframes reveal { 0% { opacity: 0; } 100% { opacity: 1; } }
#recede { width:50px;height:50px;background:red; animation: recede 0.6s ease-in 0s both; }
#reveal { width:50px;height:50px;background:blue; animation: reveal 0.5s ease-out 0s both; }
</style></head>
<body style="margin:0;background:#000">
<div id="recede"></div>
<div id="reveal"></div>
</body></html>
EOF
```

Run the check above after creating the fixture. Expected output (unchanged from before the refactor — this step must NOT change behavior):
```
rectangle [recede/rectangle] opacity= 0.22
rectangle [reveal/rectangle] opacity= 1
```

- [ ] **Step 2: Commit the mechanical refactor**

```bash
git add agents/html_extractor.py
git commit -m "$(cat <<'EOF'
refactor(html_extractor): extract animation-freeze JS to named constant

Pure move, no behavior change — _FREEZE_ANIMATIONS_JS makes the next
peak-seeking change easier to review in isolation.
EOF
)"
```

- [ ] **Step 3: Write the failing verification script for the real fix**

```bash
.venv/bin/python3 <<'PYEOF'
from agents.html_extractor import extract
spec = extract("output/_verify_scratch/freeze_fixture.html", assets_dir="output/_verify_scratch/assets2")
by_name = {e["name"]: e for e in spec["elements"]}
recede_op = by_name["[recede/rectangle]"]["opacity"]
reveal_op = by_name["[reveal/rectangle]"]["opacity"]
print("recede opacity:", recede_op, "| reveal opacity:", reveal_op)
assert recede_op >= 0.99, f"expected recede frozen at its PEAK (opacity ~1, its 0% keyframe), got {recede_op}"
assert reveal_op >= 0.99, f"expected reveal to stay at its peak (opacity ~1, its 100% keyframe, unchanged), got {reveal_op}"
print("PASS")
PYEOF
```

- [ ] **Step 4: Run it, confirm it fails**

Expected output:
```
recede opacity: 0.22 | reveal opacity: 1
AssertionError: expected recede frozen at its PEAK (opacity ~1, its 0% keyframe), got 0.22
```

- [ ] **Step 5: Implement peak-opacity seeking in `_FREEZE_ANIMATIONS_JS`**

Replace the constant added in Step 1 with:

```python
_FREEZE_ANIMATIONS_JS = r"""
() => {
    // CSS @keyframes / WAAPI: for each animation, find the keyframe with the
    // HIGHEST opacity value and seek there — the true "peak" moment, not just
    // the 100% keyframe. A plain reveal (0→1) still peaks at 100% (unchanged
    // behavior). A "recede" beat (e.g. fadeBack: 1→0.22, dimming to hand off
    // to the next storytelling beat) peaks at its 0% keyframe instead. A
    // mid-timeline flash/pulse peaks wherever its opacity is highest, even
    // if that's neither its start nor its end.
    for (const a of document.getAnimations()) {
        try {
            // Force fill:both so the seeked/finished state persists afterward
            if (a.effect) a.effect.updateTiming({ fill: 'both' });
            const kfs = (a.effect && a.effect.getKeyframes) ? a.effect.getKeyframes() : [];
            const opacityKfs = kfs
                .map((k, i) => ({
                    offset: (k.offset !== null && k.offset !== undefined)
                        ? k.offset : i / Math.max(1, kfs.length - 1),
                    opacity: k.opacity,
                }))
                .filter(k => k.opacity !== undefined)
                .map(k => ({ offset: k.offset, opacity: parseFloat(k.opacity) }));
            if (opacityKfs.length > 0) {
                // Last keyframe achieving the max value — ties favor the later
                // one, so a plain reveal that holds opacity:1 through to 100%
                // still resolves to its natural end (no behavior change there).
                const peak = opacityKfs.reduce(
                    (best, k) => (k.opacity >= best.opacity ? k : best), opacityKfs[0]);
                const timing = a.effect.getComputedTiming();
                const duration = typeof timing.duration === 'number' ? timing.duration : 0;
                a.currentTime = (timing.delay || 0) + peak.offset * duration;
                a.pause();
            } else {
                // No opacity keyframe (pure transform, e.g. a camera push) —
                // its "peak" is just its natural finished end state.
                a.finish();
                a.pause();
            }
        } catch (e) {}
    }
    // GSAP: advance its internal timeline far into the future. Peak-seeking
    // for GSAP-driven timelines is a separate follow-up, not implemented here.
    try {
        if (window.gsap) window.gsap.globalTimeline.seek(99999, false).pause();
    } catch (e) {}
    // anime.js: seek each tween to its own duration
    try {
        if (window.anime)
            window.anime.running.slice().forEach(a => { a.seek(a.duration); a.pause(); });
    } catch (e) {}
    // Freeze requestAnimationFrame so no loop can mutate state after this point
    window.requestAnimationFrame = function () { return 0; };
}
"""
```

- [ ] **Step 6: Re-run the verification script, confirm it passes**

Run the exact same script from Step 3.

Expected output:
```
recede opacity: 1 | reveal opacity: 1
PASS
```

- [ ] **Step 7: Commit**

```bash
git add agents/html_extractor.py
git commit -m "$(cat <<'EOF'
fix(html_extractor): freeze animations at peak opacity, not absolute end

.finish() always jumped to an animation's 100% keyframe. For a "recede"
beat (opacity decreasing to hand off focus to the next storytelling beat,
e.g. scene_9's fadeBack 1→0.22) that's the dimmest moment, not the peak.
Now each animation's keyframes are inspected and it's seeked to whichever
keyframe has the highest opacity — correct for reveals (peak stays at
100%), recedes (peak is at their start), and mid-timeline flashes/pulses.
EOF
)"
```

---

### Task 3: Mirror the peak-seeking fix into `utils/render_html.py`

**Files:**
- Modify: `utils/render_html.py:44-60`

- [ ] **Step 1: Replace the freeze block**

Current code at `utils/render_html.py:44-60`:

```python
        page.evaluate("""() => {
            for (const a of document.getAnimations()) {
                try {
                    if (a.effect) a.effect.updateTiming({ fill: 'both' });
                    a.finish();
                    a.pause();
                } catch (e) {}
            }
            try {
                if (window.gsap) window.gsap.globalTimeline.seek(99999, false).pause();
            } catch (e) {}
            try {
                if (window.anime)
                    window.anime.running.slice().forEach(a => { a.seek(a.duration); a.pause(); });
            } catch (e) {}
            window.requestAnimationFrame = function () { return 0; };
        }""")
        page.wait_for_timeout(200)
```

Replace with (same peak-seeking logic as `_FREEZE_ANIMATIONS_JS` in `agents/html_extractor.py` — kept as an inline literal here since `render_html.py` doesn't otherwise use named JS constants, matching its existing local style):

```python
        page.evaluate("""() => {
            for (const a of document.getAnimations()) {
                try {
                    if (a.effect) a.effect.updateTiming({ fill: 'both' });
                    const kfs = (a.effect && a.effect.getKeyframes) ? a.effect.getKeyframes() : [];
                    const opacityKfs = kfs
                        .map((k, i) => ({
                            offset: (k.offset !== null && k.offset !== undefined)
                                ? k.offset : i / Math.max(1, kfs.length - 1),
                            opacity: k.opacity,
                        }))
                        .filter(k => k.opacity !== undefined)
                        .map(k => ({ offset: k.offset, opacity: parseFloat(k.opacity) }));
                    if (opacityKfs.length > 0) {
                        const peak = opacityKfs.reduce(
                            (best, k) => (k.opacity >= best.opacity ? k : best), opacityKfs[0]);
                        const timing = a.effect.getComputedTiming();
                        const duration = typeof timing.duration === 'number' ? timing.duration : 0;
                        a.currentTime = (timing.delay || 0) + peak.offset * duration;
                        a.pause();
                    } else {
                        a.finish();
                        a.pause();
                    }
                } catch (e) {}
            }
            try {
                if (window.gsap) window.gsap.globalTimeline.seek(99999, false).pause();
            } catch (e) {}
            try {
                if (window.anime)
                    window.anime.running.slice().forEach(a => { a.seek(a.duration); a.pause(); });
            } catch (e) {}
            window.requestAnimationFrame = function () { return 0; };
        }""")
        page.wait_for_timeout(200)
```

- [ ] **Step 2: Verify against the same freeze fixture**

```bash
.venv/bin/python utils/render_html.py --input output/_verify_scratch/freeze_fixture.html --output output/_verify_scratch/freeze_fixture_render.png
```

Expected: command succeeds (`Rendered → ...`). Open `output/_verify_scratch/freeze_fixture_render.png` with the Read tool — the red `#recede` square must look fully solid/opaque red (peak, opacity 1), not a dim/washed-out red (which would mean it's still at opacity 0.22).

- [ ] **Step 3: Commit**

```bash
git add utils/render_html.py
git commit -m "$(cat <<'EOF'
fix(render_html): mirror peak-opacity animation freeze from extractor

Keeps the QC reference PNG symmetric with agents/html_extractor.py's
freeze logic (see prior "Render-HTML Reference Animation Freeze Fix") —
otherwise the reference would show recede-animated elements at their
dimmed end state while the Figma build now shows their peak, producing
a false QC mismatch.
EOF
)"
```

---

### Task 4: End-to-end verification on real scenes

**Files:** none (verification only)

- [ ] **Step 1: Rebuild scene_9 and inspect the icon + opacity**

```bash
.venv/bin/python agents/html_extractor.py --input input/scene_9.html --output output/scene_9_spec.json --assets-dir output/assets/scene_9
```

Find the raster element covering the pin/place group (name contains `focal-wrapper`):

```bash
.venv/bin/python3 -c "
import json
spec = json.load(open('output/scene_9_spec.json'))
for e in spec['elements']:
    if e['type'] == 'image' and 'focal-wrapper' in e['name']:
        print(e['id'], e['name'], e['image_path'])
"
```

Open the printed `image_path` with the Read tool. Expected: the pin icon's rounded top is fully visible (not flatly cropped), and the whole group (pin + "TECHNIQUE 1" + "PLACE") looks bright/fully opaque, not faded.

- [ ] **Step 2: Rebuild in Figma and compare**

Requires Figma desktop open with the figma-mcp-go plugin connected (if the previous scene_9 frame `14:72` still exists in the Figma file, delete it first via `mcp__figma-mcp-go__delete_nodes` so the rebuild doesn't leave a stale duplicate):

```bash
.venv/bin/python agents/figma_builder.py --spec output/scene_9_spec.json --report output/scene_9_report.json
```

Read `output/scene_9_report.json` for the new `frame_id`, then use `mcp__figma-mcp-go__save_screenshots` to export it, and `utils/render_html.py` to regenerate `output/scene_9_html_render.png` as the reference. Compare the two visually with the Read tool: pin icon uncropped, pin+"TECHNIQUE 1"+"PLACE" at full brightness in both, "WHERE ARE YOU?" unchanged (already correct — a plain reveal, unaffected by this fix).

- [ ] **Step 3: Rebuild scene_11 (the other confirmed recede-pattern scene) and spot-check**

```bash
.venv/bin/python agents/html_extractor.py --input input/scene_11.html --output output/scene_11_spec.json --assets-dir output/assets/scene_11
.venv/bin/python utils/render_html.py --input input/scene_11.html --output output/scene_11_html_render.png
```

Open `output/scene_11_html_render.png` with the Read tool. Confirm whatever element uses `fadeOutLeft` (grep `input/scene_11.html` for the class using that animation) appears at its peak/legible state, not faded/off-screen.

- [ ] **Step 4: Regression spot-check — GSAP scenes unaffected**

```bash
.venv/bin/python agents/html_extractor.py --input input/scene_2.html --output output/_verify_scratch/scene_2_spec_after.json --assets-dir output/_verify_scratch/scene_2_assets_after
diff <(python3 -c "import json; print(json.load(open('output/scene_2_spec.json'))['elements'])") \
     <(python3 -c "import json; print(json.load(open('output/_verify_scratch/scene_2_spec_after.json'))['elements'])")
```

Expected: no diff output (scene_2 is GSAP-driven with no CSS opacity-recede keyframes, so its spec must be byte-identical before/after this fix).

Repeat the identical pattern for `scene_5.html`:

```bash
.venv/bin/python agents/html_extractor.py --input input/scene_5.html --output output/_verify_scratch/scene_5_spec_after.json --assets-dir output/_verify_scratch/scene_5_assets_after
diff <(python3 -c "import json; print(json.load(open('output/scene_5_spec.json'))['elements'])") \
     <(python3 -c "import json; print(json.load(open('output/_verify_scratch/scene_5_spec_after.json'))['elements'])")
```

Expected: no diff output, same reasoning.

- [ ] **Step 5: Regression spot-check — Fix A doesn't break scenes with no escaping descendants**

By this point Tasks 1-3 are already committed, so there's no uncommitted "before" state left to diff against. Run scene_12 through the fixed extractor and confirm the output is healthy — no crashes, no new warnings, sane raster sizes:

```bash
.venv/bin/python agents/html_extractor.py --input input/scene_12.html --output output/_verify_scratch/scene_12_spec_after.json --assets-dir output/_verify_scratch/scene_12_assets_after
python3 -c "
import json
spec = json.load(open('output/_verify_scratch/scene_12_spec_after.json'))
print('warnings:', spec['warnings'])
raster = [e for e in spec['elements'] if e['type'] == 'image']
print('raster element count:', len(raster))
for e in raster[:3]:
    print(' ', e['name'], e['width'], e['height'])
"
```

Expected: the extractor completes with no new warnings mentioning `isolation failed` or `not found in DOM`, and the printed raster element sizes look like ordinary, reasonable numbers (not near-zero or absurdly large) — confirming Fix A didn't corrupt bbox math for a scene with no absolutely-positioned-descendant-escaping case. Open one or two of `output/_verify_scratch/scene_12_assets_after/*.png` with the Read tool as a final visual sanity check — they should look like intact, uncropped graphics, same as scene_12 has always rendered.

- [ ] **Step 6: Clean up scratch fixtures**

```bash
git status --short output/
```

`output/` is gitignored, so `output/_verify_scratch/` never entered git — nothing to unstage or revert. Leave it or delete the directory manually; it has no bearing on the repository.

---

## Notes

- GSAP timeline peak-seeking (relevant to `scene_2`, `scene_5`'s counters, and any future GSAP-driven scene with a recede pattern) is explicitly **out of scope** for this plan — see the design doc's "Out of scope" section. No confirmed GSAP-recede bug exists yet.
- Do not touch `input/*.html` source files in this plan — the video-editor side that generates them is out of this project's scope (per prior user direction).
