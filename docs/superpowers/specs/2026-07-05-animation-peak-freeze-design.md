# Animation Peak-Freeze + Raster Bbox Fix — Design

## Problem

Reported on scene_9 (`input/scene_9.html`) after the earlier canvas-detection fix:

1. **Icon bị cắt** — the location-pin icon inside `[focal-wrapper/Image]` (spec element `e33`) is cropped at the top in the exported raster PNG, independent of the overall frame size.
2. **Không phải đỉnh của animation + opacity** — the pin + "TECHNIQUE 1" + "PLACE" group is captured at a faded/receded visual state (~22% opacity), not the moment it looks its best. Different objects in the same scene reach their own "peak" (most legible/most opaque) at different times, and the current pipeline doesn't account for that.

Both bugs were traced to concrete code locations in `agents/html_extractor.py` (see Root Cause below). A survey of `input/*.html` confirms the "recede to hand off to the next beat" animation pattern exists in at least two scenes (`scene_9`: `fadeBack`, `scene_11`: `fadeOutLeft`), so this is a systemic extractor issue, not scene_9-specific.

## Root Cause

### 1. Raster bbox ignores absolutely-positioned descendants

`_GET_BBOX_JS` (`agents/html_extractor.py:1540-1546`) computes the screenshot clip rect from `target.getBoundingClientRect()` — the container's own layout box only:

```js
const r = el.getBoundingClientRect();
return {x: r.x, y: r.y, w: r.width, h: r.height};
```

`.focal-wrapper` is `display:flex; flex-direction:column` and auto-sized by its in-flow children (`.number-container`, `.title-section`). Its child `.pin-bg` is `position:absolute; top:38%; left:50%; transform:translate(-50%,-50%); width:520px; height:680px` — removed from normal flow, so it does **not** contribute to `.focal-wrapper`'s own auto-computed size. Since the pin visually extends above where `.focal-wrapper`'s own box starts, the screenshot clip (built from that box + bleed padding for blur/shadow) cuts off the pin's rounded top.

### 2. `.finish()` freezes every animation at its absolute end, including "recede" beats

The freeze step (`agents/html_extractor.py:~1240-1248`) calls `a.finish(); a.pause();` for every `Animation` in `document.getAnimations()`, unconditionally jumping to the 100% keyframe.

For a **reveal** animation (e.g. `pinFadeIn`: opacity 0→1), 100% is correctly the peak. But `.focal-wrapper`'s own animation, `fadeBack` (`opacity:1→0.22, scale:1→0.95, filter:blur(0)→blur(1.5px)`, starting at 2.6s), is a **recede** — a deliberate one-shot dimming to hand focus to the next beat (`WHERE ARE YOU?` scale-punches in right after, at 3.2s). `.finish()` lands on `fadeBack`'s 100% keyframe (opacity 0.22) — the *least* visible moment, not the peak.

`utils/render_html.py` mirrors this same freeze sequence (by design — see "Render-HTML Reference Animation Freeze Fix" memory) to generate the QC reference PNG, so it reproduces the identical wrong-state artifact there too.

## Design

### Fix A — Raster bbox = union of container + all descendants

In `_GET_BBOX_JS`, replace the single `getBoundingClientRect()` read with the union of the target's own rect and the rects of every element inside it:

```js
(uid) => {
    const el = document.querySelector(`[data-extract-uid="${uid}"]`);
    if (!el) return null;
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
```

This guarantees any absolutely/fixed-positioned descendant that visually escapes the container's own auto-sized box is still fully included in the capture window. Bleed padding (glow/shadow) is computed and applied exactly as today, just on top of this corrected union box instead of the container-only box.

**Scope:** applies to every raster capture (`_isolated_screenshot` call sites), not just `.focal-wrapper` — any container+absolute-child combination anywhere in any scene benefits.

### Fix B — Per-animation opacity-peak seeking (CSS/WAAPI only this round)

Replace the blanket `.finish()` loop with per-animation logic:

```js
for (const a of document.getAnimations()) {
    try {
        if (a.effect) a.effect.updateTiming({ fill: 'both' });
        const kfs = a.effect && a.effect.getKeyframes ? a.effect.getKeyframes() : [];
        const opacityKfs = kfs
            .map((k, i) => ({ offset: k.offset ?? (i / Math.max(1, kfs.length - 1)), opacity: k.opacity }))
            .filter(k => k.opacity !== undefined)
            .map(k => ({ offset: k.offset, opacity: parseFloat(k.opacity) }));
        if (opacityKfs.length > 0) {
            // Last keyframe achieving the max opacity value (handles reveal,
            // recede, and mid-timeline flash/pulse shapes with one rule).
            const peak = opacityKfs.reduce((best, k) =>
                k.opacity >= best.opacity ? k : best, opacityKfs[0]);
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
```

- Animations with an `opacity` keyframe track: freeze at the keyframe with the highest opacity (ties broken by taking the later one — a plain reveal's peak is still its 100% keyframe, unchanged behavior).
- Animations without an opacity track (pure transform, e.g. `cameraPush`): unchanged, still `.finish()` to their natural end.
- Each `Animation` is seeked independently, so elements whose peaks fall at different times (e.g. the pin's peak vs. "WHERE ARE YOU?"'s peak) are each captured correctly without needing one shared freeze time for the whole page.
- GSAP (`window.gsap.globalTimeline.seek(99999,false).pause()`) and anime.js seeking are **unchanged** this round — out of scope (see below).

### Keep `utils/render_html.py` symmetric

Port the exact same per-animation peak-seeking block into `render_html.py`'s freeze step, mirroring Fix B verbatim, so the QC reference PNG reflects the same peak states as the Figma build. (Fix A doesn't apply to `render_html.py` — that file does one full-page screenshot, not per-element isolated raster captures.)

## Testing

1. Re-run Bước 1+2 for `scene_9` and `scene_11` (both have confirmed recede-pattern animations). Compare the rebuilt Figma frame against a freshly-rendered `render_html.py` reference:
   - scene_9: pin icon top not clipped; pin + "TECHNIQUE 1" + "PLACE" rendered at full opacity/scale (pre-fadeBack state), not the dimmed 22% state.
   - scene_11: `fadeOutLeft`-affected element(s) similarly captured at peak, not faded end state.
2. Re-run `scene_2` and `scene_5` (GSAP-driven, reveal-only, no recede pattern) as a regression check — expect **no visual change** from the last validated build, since neither has a CSS opacity-recede keyframe (GSAP path untouched, and any CSS animations in these two scenes are plain reveals whose peak is already their 100% keyframe).
3. Spot-check one or two other existing scenes with CSS `@keyframes` reveal-only animations (e.g. `scene_12`) to confirm no regression from Fix A (bbox union) — sizes/positions of existing raster assets should stay identical when there are no absolutely-positioned escaping descendants.

## Out of scope (deferred, separate spec)

- GSAP timeline peak-seeking (`scene_2`, `scene_5` and any future GSAP-driven scene with a recede pattern). Requires timeline time-sampling (scrub + read computed opacity at multiple points) since GSAP doesn't expose a keyframe list the way WAAPI does. No confirmed instance of a GSAP recede bug exists yet — revisit if one surfaces.
