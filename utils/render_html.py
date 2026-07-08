#!/usr/bin/env python3
"""
Render an HTML file in Chromium and save a faithful screenshot of what the
browser ACTUALLY draws. This is the QC reference for comparing against Figma.

Why: comparing Figma to the raw HTML *source* invites inferring intent from CSS
(e.g. a `background:red` div that actually renders at height=0 → invisible).
The rendered PNG shows only what is truly visible, so QC can't false-flag
invisible elements as "missing".

Usage:
    python utils/render_html.py --input input/scene_5.html --output output/scene_5_html_render.png
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Reuse the extractor's own viewport-detection + frame-sizing logic so this
# reference screenshot always measures the identical region of the page that
# html_extractor.py sizes the Figma frame to. Previously this file just
# cropped to document.body's bare bounding box (no card-mode +100px margin,
# no accounting for decorative elements bleeding past body's own edge), so it
# could silently disagree with the actual Figma frame size (scene_22).
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root))
from agents.html_extractor import (  # noqa: E402
    _DETECT_DESIGN_JS, _EXTRACT_JS, _MEASURE_CANVAS_JS, PROBE_WIDTH,
    decide_design_viewport, compute_frame_size,
)


def main():
    ap = argparse.ArgumentParser(description="Render HTML to a faithful PNG for QC")
    ap.add_argument("--input", required=True, help="Path to source HTML file")
    ap.add_argument("--output", required=True, help="Output PNG path")
    ap.add_argument("--viewport-width", type=int, default=None,
                     help="Force a specific probe width (skips auto-detect). "
                          "Mirrors html_extractor.py's --viewport-width; default: auto-detect.")
    args = ap.parse_args()

    url = Path(args.input).resolve().as_uri()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    auto_width = args.viewport_width is None
    init_width = PROBE_WIDTH if auto_width else args.viewport_width

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": init_width, "height": 900},
                                 device_scale_factor=2)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(500)

        canvas_mode, canvas_dims = False, None
        if auto_width:
            design = page.evaluate(_DETECT_DESIGN_JS)
            decision = decide_design_viewport(design)
            chosen, chosen_h = decision["chosen_w"], decision["chosen_h"]
            canvas_mode, canvas_dims = decision["canvas_mode"], decision["canvas_dims"]
            if chosen != init_width or chosen_h != 900:
                page.set_viewport_size({"width": chosen, "height": chosen_h})
                page.wait_for_timeout(400)  # let layout reflow

        # Measure the tagged canvas element's position NOW — after the probe
        # viewport is final, but before the freeze below can distort it via a
        # scale/translateZ "camera" animation on the canvas element itself.
        # Mirrors agents/html_extractor.py's identical step; see its comment.
        canvas_pos = page.evaluate(_MEASURE_CANVAS_JS)
        canvas_pos = (canvas_pos["x"], canvas_pos["y"]) if canvas_pos else None

        # Jump every animation to its OWN peak-opacity moment (or natural end
        # state if it has no opacity keyframe) — mirrors extractor's
        # _FREEZE_ANIMATIONS_JS in agents/html_extractor.py exactly, so the QC
        # reference reflects the same frozen state as the Figma build.
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
                        a.pause();
                        a.currentTime = (timing.delay || 0) + peak.offset * duration;
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

        # Prevent NEW animations/transitions from triggering before the screenshot.
        page.add_style_tag(content="""
            *, *::before, *::after {
                animation-play-state: paused !important;
                transition-duration: 0s !important;
                transition-delay: 0s !important;
            }
        """)
        page.wait_for_timeout(100)

        result = page.evaluate(_EXTRACT_JS)
        raw_elements = result["elements"]
        frame_w, frame_h = result["frameWidth"], result["frameHeight"]

        geom = compute_frame_size(raw_elements, frame_w, frame_h, canvas_mode, canvas_dims, canvas_pos)
        adj_w, adj_h = geom["adj_w"], geom["adj_h"]
        origin_x, origin_y = geom["origin_x"], geom["origin_y"]

        def _frame_origin():
            r = page.evaluate(
                "() => { const b = document.body.getBoundingClientRect(); return {left:b.left, top:b.top}; }")
            return r["left"], r["top"]

        # origin_x/origin_y are relative to body's own top-left (matching
        # _EXTRACT_JS's coordinate convention) — add body's viewport-relative
        # position back to get a clip Playwright can actually capture.
        frame_left, frame_top = _frame_origin()
        clip_x = max(0.0, frame_left + origin_x)
        clip_y = max(0.0, frame_top + origin_y)

        # The frame can be wider/taller than the current viewport (card-mode's
        # +100px margin, or content bleeding past the probe width) — grow the
        # viewport to fit before the final screenshot, then re-read body's
        # position since growing can shift a flex-centered layout.
        vp = page.viewport_size
        need_w = int(clip_x + adj_w) + 1
        need_h = int(clip_y + adj_h) + 1
        if need_w > vp["width"] or need_h > vp["height"]:
            page.set_viewport_size({"width": max(vp["width"], need_w),
                                     "height": max(vp["height"], need_h)})
            page.wait_for_timeout(300)
            frame_left, frame_top = _frame_origin()
            clip_x = max(0.0, frame_left + origin_x)
            clip_y = max(0.0, frame_top + origin_y)

        clip = {"x": clip_x, "y": clip_y, "width": adj_w, "height": adj_h}
        page.screenshot(path=str(out), clip=clip)
        browser.close()

    print(f"Rendered → {out}  ({int(clip['width'])}×{int(clip['height'])}px)")


if __name__ == "__main__":
    main()
