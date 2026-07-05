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
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser(description="Render HTML to a faithful PNG for QC")
    ap.add_argument("--input", required=True, help="Path to source HTML file")
    ap.add_argument("--output", required=True, help="Output PNG path")
    ap.add_argument("--viewport", default="1920x1080",
                    help="Initial viewport WxH (default 1920x1080); grows to fit content")
    args = ap.parse_args()

    vw, vh = (int(n) for n in args.viewport.lower().split("x"))
    url = Path(args.input).resolve().as_uri()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": vw, "height": vh},
                                device_scale_factor=2)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(400)

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

        # Clip to the actual rendered content box of <body> so the PNG matches
        # what the extractor normalizes to (bounding box of visible content).
        rect = page.evaluate(
            "() => { const b = document.body.getBoundingClientRect();"
            " return {x:b.x, y:b.y, w:b.width, h:b.height}; }")
        clip = {"x": max(rect["x"], 0), "y": max(rect["y"], 0),
                "width": rect["w"], "height": rect["h"]}

        page.screenshot(path=str(out), clip=clip)
        browser.close()

    print(f"Rendered → {out}  ({int(clip['width'])}×{int(clip['height'])}px)")


if __name__ == "__main__":
    main()
