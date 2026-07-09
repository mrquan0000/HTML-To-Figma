#!/usr/bin/env python3
"""
HTML → Figma spec extractor (v2).

Renders an HTML file in a headless Chromium browser (Playwright),
classifies every visible element into one of:
  - native primitives Figma can render exactly (rectangle, ellipse, text, image)
  - raster fallback PNG (SVG, filter, conic-gradient, clip-path, complex transforms)
  - pure container (group only, no fill)

Outputs a deterministic spec.json consumed by agents/figma_builder.py.

Usage:
    python agents/html_extractor.py --input path/to/file.html --output output/spec.json
                                    [--viewport-width 600] [--assets-dir output/assets]

═══════════════════════════════════════════════════════════════════════════════
SPEC v2 SCHEMA
═══════════════════════════════════════════════════════════════════════════════

{
  "version": 2,
  "frame_name": str,
  "frame_width": int, "frame_height": int,
  "frame_bg": "#RRGGBB" | null,
  "assets_dir": "relative/path/to/png/folder",
  "warnings": [str, ...],
  "elements": [Element, ...]    # depth-first, sorted by effective z-index
}

Element (common fields):
  id: str                        # stable, e.g. "e0", "e1"
  parent_id: str | null          # for nesting / grouping
  type: "rectangle" | "ellipse" | "text" | "image" | "frame" | "group"
  name: str                      # human-readable layer name
  x, y, width, height: int
  rotation: float                # degrees
  opacity: float                 # 0..1
  z: int                         # effective z for sort

Shape-only:
  fills: [Fill, ...]
  strokes: [Fill, ...]
  stroke_weight: float
  stroke_align: "INSIDE" | "OUTSIDE" | "CENTER"
  corner_radii: [tl, tr, br, bl]
  effects: [Effect, ...]
  clip_content: bool             # true for frame with overflow:hidden

Text-only:
  runs: [TextRun, ...]
  text_align: "LEFT" | "CENTER" | "RIGHT"
  line_height: float | null

Image-only:
  image_path: str                # relative to spec.json

Fill / Stroke:
  {type: "SOLID", color: {r, g, b, a}}                          (0..1 floats)
  {type: "GRADIENT_LINEAR", stops: [{position, color}], angle}  (angle in deg, 0=top)
  {type: "GRADIENT_RADIAL", stops: [{position, color}]}
  {type: "IMAGE", image_path: str, scale_mode: "FILL"|"FIT"}

Effect:
  {type: "DROP_SHADOW", color, offset: {x,y}, radius, spread}
  {type: "INNER_SHADOW", color, offset, radius, spread}
  {type: "BACKGROUND_BLUR", radius}
  {type: "LAYER_BLUR",     radius}

TextRun:
  text: str
  font_family: str
  font_size: float
  font_weight: int          # 100..900
  italic: bool
  fills: [Fill, ...]        # solid or gradient (for background-clip:text)
  letter_spacing: float     # px
  decoration: "NONE" | "UNDERLINE" | "STRIKETHROUGH"
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import re
import sys
from pathlib import Path


# ═════════════════════════════════════════════════════════════════════════════
# JS injected into the page — extracts raw style data for every element
# ═════════════════════════════════════════════════════════════════════════════

_EXTRACT_JS = r"""
(extraNoiseWords) => {
    const SKIP_TAGS = new Set(['SCRIPT','STYLE','HEAD','META','LINK','NOSCRIPT','TITLE']);
    const TEXT_TAGS = new Set(['H1','H2','H3','H4','H5','H6','P','SPAN','LABEL','LI','A','STRONG','EM','B','I','SMALL','CODE']);

    // Element gets a unique id assigned during walk
    let uidCounter = 0;
    function uid() { return 'e' + (uidCounter++); }

    // CSS `filter: brightness(N)` is a linear R/G/B multiplier (alpha untouched).
    // Baking it into a color string lets a brightness-filtered container stay
    // NATIVE (shape+icon+text as separate editable Figma layers) instead of
    // rasterizing the whole subtree to reproduce the dimming.
    function applyBrightness(colorStr, mul) {
        if (mul === 1 || !colorStr) return colorStr;
        const m = colorStr.match(/rgba?\(([^)]+)\)/);
        if (!m) return colorStr;
        const parts = m[1].split(',').map(s => s.trim());
        const r = Math.min(255, Math.round(parseFloat(parts[0]) * mul));
        const g = Math.min(255, Math.round(parseFloat(parts[1]) * mul));
        const b = Math.min(255, Math.round(parseFloat(parts[2]) * mul));
        return parts.length > 3 ? `rgba(${r}, ${g}, ${b}, ${parts[3]})` : `rgb(${r}, ${g}, ${b})`;
    }

    // Matches a filter string that is ONLY a single brightness() call (no blur,
    // hue-rotate, etc. combined in) — those still fall back to raster since
    // they aren't a simple per-channel color multiply. Returns the factor, or
    // null if the filter isn't (purely) brightness.
    function parseBrightnessOnly(filter) {
        if (!filter || filter === 'none') return null;
        const m = filter.trim().match(/^brightness\(\s*([\d.]+)(%?)\s*\)$/);
        if (!m) return null;
        let v = parseFloat(m[1]);
        if (m[2] === '%') v = v / 100;
        return v;
    }

    function styleSnapshot(el, pseudo, brightnessMul = 1) {
        const cs = window.getComputedStyle(el, pseudo);
        const snap = {
            display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
            position: cs.position, zIndex: cs.zIndex,
            backgroundColor: cs.backgroundColor, backgroundImage: cs.backgroundImage,
            backgroundSize: cs.backgroundSize,
            // Per-side border widths + colors + styles
            borderTopWidth: cs.borderTopWidth, borderRightWidth: cs.borderRightWidth,
            borderBottomWidth: cs.borderBottomWidth, borderLeftWidth: cs.borderLeftWidth,
            borderTopColor: cs.borderTopColor, borderRightColor: cs.borderRightColor,
            borderBottomColor: cs.borderBottomColor, borderLeftColor: cs.borderLeftColor,
            borderTopStyle: cs.borderTopStyle, borderRightStyle: cs.borderRightStyle,
            borderBottomStyle: cs.borderBottomStyle, borderLeftStyle: cs.borderLeftStyle,
            // Per-corner radius
            borderTopLeftRadius: cs.borderTopLeftRadius,
            borderTopRightRadius: cs.borderTopRightRadius,
            borderBottomRightRadius: cs.borderBottomRightRadius,
            borderBottomLeftRadius: cs.borderBottomLeftRadius,
            // Effects
            boxShadow: cs.boxShadow,
            filter: cs.filter,
            backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter || '',
            clipPath: cs.clipPath,
            mask: cs.mask || cs.webkitMask || 'none',
            overflow: cs.overflow,
            transform: cs.transform,
            transformOrigin: cs.transformOrigin,
            mixBlendMode: cs.mixBlendMode,
            // Text
            color: cs.color, fontSize: cs.fontSize, fontWeight: cs.fontWeight,
            fontFamily: cs.fontFamily, fontStyle: cs.fontStyle,
            textAlign: cs.textAlign, lineHeight: cs.lineHeight,
            letterSpacing: cs.letterSpacing, textDecorationLine: cs.textDecorationLine,
            webkitTextFillColor: cs.webkitTextFillColor || '',
            webkitTextStrokeWidth: cs.webkitTextStrokeWidth || '',
            webkitTextStrokeColor: cs.webkitTextStrokeColor || '',
            backgroundClip: cs.backgroundClip || cs.webkitBackgroundClip || '',
            content: pseudo ? cs.content : '',
        };
        if (brightnessMul !== 1) {
            snap.backgroundColor = applyBrightness(snap.backgroundColor, brightnessMul);
            snap.borderTopColor = applyBrightness(snap.borderTopColor, brightnessMul);
            snap.borderRightColor = applyBrightness(snap.borderRightColor, brightnessMul);
            snap.borderBottomColor = applyBrightness(snap.borderBottomColor, brightnessMul);
            snap.borderLeftColor = applyBrightness(snap.borderLeftColor, brightnessMul);
            snap.color = applyBrightness(snap.color, brightnessMul);
            if (snap.webkitTextFillColor) snap.webkitTextFillColor = applyBrightness(snap.webkitTextFillColor, brightnessMul);
            if (snap.webkitTextStrokeColor) snap.webkitTextStrokeColor = applyBrightness(snap.webkitTextStrokeColor, brightnessMul);
        }
        return snap;
    }

    function rectOf(el) {
        const r = el.getBoundingClientRect();
        return {x: r.left, y: r.top, w: r.width, h: r.height,
                right: r.right, bottom: r.bottom};
    }

    // Absolute rotation (deg) of a computed transform. translate-only → 0.
    function rotationDeg(transform) {
        if (!transform || transform === 'none') return 0;
        const m = transform.match(/matrix\(\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)/);
        if (m) return Math.abs(Math.atan2(parseFloat(m[2]), parseFloat(m[1])) * 180 / Math.PI);
        const r = transform.match(/rotate\(\s*(-?[\d.]+)deg/);
        return r ? Math.abs(parseFloat(r[1])) : 0;
    }

    // Union of an element's own text line boxes (where the glyphs actually render),
    // returned as an OFFSET from the element's border-box (elX/elY are the element's
    // frame-relative coords) so it survives the later origin shift. Used to give a
    // text element its TRUE geometry when the element also paints a box of a
    // different size than its text — e.g. a fixed 80×160 .domino card whose
    // single-word label renders on one line OVERFLOWING the box. null if none.
    function textBoxOffset(el, elX, elY) {
        try {
            const range = document.createRange();
            range.selectNodeContents(el);
            const rects = range.getClientRects();
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            for (const r of rects) {
                if (r.width === 0 || r.height === 0) continue;
                x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
                x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
            }
            if (x0 === Infinity) return null;
            return {dx: (x0 - frameRect.left) - elX, dy: (y0 - frameRect.top) - elY,
                    w: Math.round(x1 - x0), h: Math.round(y1 - y0)};
        } catch (e) { return null; }
    }

    // Frame root = body. All top-level layout (ambient layers, main container, etc.)
    // becomes children. Body background is captured separately as frame_bg.
    function findFrameRoot() { return document.body; }

    // Extract inline text runs from a text-bearing element.
    // Walks immediate descendants: text nodes inherit element style;
    // <span>/<strong>/<em>/<b>/<i> with text children become separate runs.
    function extractRuns(el, brightnessMul = 1) {
        const runs = [];
        function pushFromText(node, styleEl) {
            let txt = node.textContent;
            if (!txt) return;
            const cs = window.getComputedStyle(styleEl);
            // Collapse whitespace per CSS white-space (default normal: runs of
            // whitespace/newlines from HTML indentation → single space).
            if (!(cs.whiteSpace || 'normal').startsWith('pre')) {
                txt = txt.replace(/\s+/g, ' ');
            }
            if (!txt) return;
            // Apply text-transform so the spec stores the rendered casing.
            const tt = cs.textTransform;
            if (tt === 'uppercase') txt = txt.toUpperCase();
            else if (tt === 'lowercase') txt = txt.toLowerCase();
            else if (tt === 'capitalize') txt = txt.replace(/\b\w/g, c => c.toUpperCase());
            // extractRuns reads its own getComputedStyle (not styleSnapshot's),
            // so an ancestor's brightness() filter must be baked in here too —
            // see visit()'s effectiveBrightness / classify()'s brightness note.
            runs.push({
                text: txt,
                fontFamily: cs.fontFamily, fontSize: cs.fontSize,
                fontWeight: cs.fontWeight, fontStyle: cs.fontStyle,
                color: applyBrightness(cs.color, brightnessMul), letterSpacing: cs.letterSpacing,
                textDecorationLine: cs.textDecorationLine,
                backgroundImage: cs.backgroundImage,
                backgroundClip: cs.backgroundClip || cs.webkitBackgroundClip || '',
                webkitTextFillColor: cs.webkitTextFillColor ? applyBrightness(cs.webkitTextFillColor, brightnessMul) : '',
            });
        }
        function walk(node, styleEl) {
            for (const child of node.childNodes) {
                if (child.nodeType === 3) {
                    pushFromText(child, styleEl);
                } else if (child.nodeType === 1) {
                    const tn = child.tagName;
                    if (SKIP_TAGS.has(tn)) continue;
                    if (tn === 'BR') { runs.push({text: '\n', _br: true}); continue; }
                    // Recurse with the child as new styling context
                    walk(child, child);
                }
            }
        }
        walk(el, el);
        // Merge adjacent runs with identical style
        const merged = [];
        for (const r of runs) {
            const prev = merged[merged.length-1];
            if (prev && !r._br && !prev._br
                && prev.fontFamily===r.fontFamily && prev.fontSize===r.fontSize
                && prev.fontWeight===r.fontWeight && prev.color===r.color
                && prev.fontStyle===r.fontStyle && prev.backgroundImage===r.backgroundImage
                && prev.webkitTextFillColor===r.webkitTextFillColor) {
                prev.text += r.text;
            } else {
                merged.push({...r});
            }
        }
        return merged;
    }

    // Effective paint order, respecting CSS stacking-context INHERITANCE.
    // A non-positioned (or auto-z) descendant of a positioned z-indexed ancestor
    // must stack at that ancestor's level — e.g. text inside `.title-block
    // {position:absolute; z-index:10}` paints above ambient glows (z-index:1),
    // even though the text itself is non-positioned. The old per-element formula
    // gave such text a tiny z and let glows/grid paint over it.
    //
    //   band  = nearest ancestor (incl self) that creates a stacking context
    //           (positioned + explicit z-index) → 100000 + zIndex*1000
    //   +500  = within a band, positioned elements paint above non-positioned
    //   +docOrder = tree-order tiebreak (DOM paint order)
    function stackingBand(el) {
        let node = el;
        while (node && node !== document.body && node !== document.documentElement) {
            const cs = window.getComputedStyle(node);
            if (cs.position !== 'static' && cs.zIndex !== 'auto') {
                return 100000 + (parseInt(cs.zIndex) || 0) * 1000;
            }
            node = node.parentElement;
        }
        return 0;
    }
    function effectiveZ(el, docOrder) {
        const cs = window.getComputedStyle(el);
        const positioned = cs.position !== 'static' ? 1 : 0;
        return stackingBand(el) + positioned * 500 + docOrder;
    }

    // Detect SVG vs raster vs native.
    // figma-mcp-go set_fills only supports SOLID hex — any gradient must be rasterized.
    // Detect a CSS "border-triangle" pseudo (::before/::after): a zero-size
    // content box whose shape comes purely from a solid colored border — the
    // classic ▶ play-icon / caret trick. Such pseudos have no DOM node so they
    // can't be screenshotted alone; we rasterize their (leaf) host instead.
    function hasBorderTrianglePseudo(el) {
        for (const ps of ['::before', '::after']) {
            const p = window.getComputedStyle(el, ps);
            if (!p.content || p.content === 'none') continue;
            const w = parseFloat(p.width) || 0;
            const h = parseFloat(p.height) || 0;
            if (w > 0 && h > 0) continue;            // triangle needs width:0 or height:0
            for (const s of ['Top', 'Right', 'Bottom', 'Left']) {
                const bw = parseFloat(p['border' + s + 'Width']) || 0;
                const bc = p['border' + s + 'Color'] || '';
                if (bw > 0 && p['border' + s + 'Style'] === 'solid'
                    && bc && bc !== 'transparent' && bc !== 'rgba(0, 0, 0, 0)') {
                    return true;
                }
            }
        }
        return false;
    }

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

    // True when `backgroundImage` is EXACTLY one linear-gradient(...) or
    // radial-gradient(...) call filling the ENTIRE value — no stacked/
    // multi-layer backgrounds, no conic/repeating. Rejects a trailing
    // comma-separated layer by checking the gradient's own closing paren
    // (tracked by depth) is the LAST character of the string, not just
    // matched somewhere via a greedy `.+` (a naive regex would wrongly
    // accept `linear-gradient(a,b), linear-gradient(c,d)` as "simple" and
    // blend unrelated stops/alphas from the second layer into the first).
    function isSimpleGradientBg(backgroundImage) {
        const s = (backgroundImage || '').trim();
        const m = s.match(/^(linear|radial)-gradient\(/);
        if (!m) return false;
        let depth = 0;
        for (let i = m[0].length - 1; i < s.length; i++) {
            const ch = s[i];
            if (ch === '(') depth++;
            else if (ch === ')') {
                depth--;
                if (depth === 0) return i === s.length - 1;
            }
        }
        return false;
    }

    function classify(el, cs, hasDirectText, hasElemChildren, w, h) {
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
        if (cs.clipPath && cs.clipPath !== 'none')           return 'raster';
        if (cs.mask && cs.mask !== 'none')                   return 'raster';
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
            // Compare against frameRect (the body's actual rendered box), NOT
            // window.innerWidth/innerHeight (the fixed probe-viewport size) —
            // frameRect is exactly what becomes frameWidth/frameHeight, the
            // same reference the Python-side _is_full_frame_bg compares
            // against. Using the raw viewport instead would misjudge on any
            // page whose body height differs from the arbitrary probe height.
            const isFullFrameBg = w >= frameRect.width * 0.95 && h >= frameRect.height * 0.95;
            if (!isSimpleGradientBg(cs.backgroundImage) || isFullFrameBg) return 'raster';
        }
        // 3d transforms → raster, UNLESS the matrix3d is only a perspective-divisor
        // artifact with no real 3D rotation/scale/translation. An animation
        // declared with `perspective(N) translateX(...) scale(...)` resolves its
        // computed transform to matrix3d(...) even once translateX/scale settle
        // back to their neutral values — leaving only the perspective term (the
        // d3 component, -1/N) non-identity (e.g. scene_18's cameraSlideRight at
        // its 100% keyframe: matrix3d(1,0,0,0, 0,1,0,0, 0,0,1,-1/1400, 0,0,0,1)).
        // That's visually indistinguishable from transform:none, so only raster
        // when the 3×3 rotation/scale submatrix or the translation actually
        // deviates from identity.
        if (cs.transform && (cs.transform.includes('matrix3d') || cs.transform.includes('perspective'))) {
            const m3d = cs.transform.match(/matrix3d\(([^)]+)\)/);
            let isIdentity3d = false;
            if (m3d) {
                const v = m3d[1].split(',').map(s => parseFloat(s.trim()));
                const EPS = 1e-4;
                const near = (a, b) => Math.abs(a - b) < EPS;
                isIdentity3d = near(v[0], 1) && near(v[1], 0) && near(v[2], 0)
                    && near(v[4], 0) && near(v[5], 1) && near(v[6], 0)
                    && near(v[8], 0) && near(v[9], 0) && near(v[10], 1)
                    && near(v[12], 0) && near(v[13], 0) && near(v[14], 0);
            }
            if (!isIdentity3d) return 'raster';
        }
        // Non-trivially ROTATED leaf → raster: figma-mcp-go's node rotation is
        // applied opposite-sign to CSS and the native box stores the AABB (not the
        // true unrotated size), so a rotated native shape renders wrong. Baking the
        // rotation into the screenshot PNG reproduces it faithfully (same mechanism
        // as SVG/gradient). Scope to leaves (no element children) so we never
        // flatten a subtree; covers scene_22's rotated .domino cards.
        if (!hasElemChildren && rotationDeg(cs.transform) > 0.5) return 'raster';
        // Leaf (no text/children to flatten) whose only visual extra is a CSS
        // border-triangle pseudo (▶ play icon) → rasterize so the pseudo renders.
        if (!hasDirectText && !hasElemChildren && hasBorderTrianglePseudo(el)) return 'raster';
        // Image-clipping viewport (carousel / gallery / thumb): an element that
        // clips overflow and contains <img> descendants but no direct text. Its
        // own box shows exactly the visible (clipped) image, while inner <img>
        // slides may extend far off-screen (e.g. a slick-track). Rasterize the
        // clipped box as one image and stop recursion so off-screen slides aren't
        // emitted as stray, frame-inflating image nodes.
        if (!hasDirectText
            && (cs.overflow.includes('hidden') || cs.overflow.includes('clip'))
            && el.querySelector('img')) return 'raster';
        return 'native';
    }

    const frameRoot = findFrameRoot();
    const frameRect = frameRoot.getBoundingClientRect();
    const bodyCS = window.getComputedStyle(document.body);

    const out = [];
    const skipWarnings = [];
    const noiseReview = [];
    let docOrder = 0;

    // ─── Decorative-noise swarm detection ──────────────────────────────────
    // Drop swarms of tiny textless "particle/dust/mote" leaves (they explode
    // into piles of dead Figma layers never animated in AE). Structure decides;
    // the keyword list only raises confidence and NEVER fires on its own, so a
    // real element like `.glowing-text` (has text) can't be dropped by name.
    const NOISE_WORDS = ['particle','mote','dust','spark','bokeh','snow',
                         'ember','fleck','speck','twinkle'];
    // Merge in user-curated extras from config/noise_keywords.txt (passed by the
    // Python caller). The core above is always active; extras only ADD.
    if (Array.isArray(extraNoiseWords)) {
        for (const w of extraNoiseWords) if (w) NOISE_WORDS.push(String(w).toLowerCase());
    }

    function noiseMatch(el) {
        const cls = (el.getAttribute('class') || '').toLowerCase();
        return NOISE_WORDS.some(w => cls.includes(w));
    }

    const SWARM_FLOOR = 8;      // min group size to be swarm-shaped
    const DROP_MAX_PX = 12;     // a droppable particle is this small or smaller
    const REVIEW_MAX_PX = 24;   // review net is wider — catches "hạt hơi to hơn"

    // `parent`'s direct children that are leaves (no real element children),
    // hold no text, and whose max dimension is ≤ maxPx. Returns [{el, r}].
    function smallTextlessLeaves(parent, maxPx) {
        const cands = [];
        for (const c of parent.children) {
            if (SKIP_TAGS.has(c.tagName) || c.tagName === 'BR') continue;
            let isLeaf = true;
            for (const cc of c.children) {
                if (!SKIP_TAGS.has(cc.tagName) && cc.tagName !== 'BR') { isLeaf = false; break; }
            }
            if (!isLeaf) continue;
            let hasText = false;
            for (const n of c.childNodes) {
                if (n.nodeType === 3 && n.textContent.trim()) { hasText = true; break; }
            }
            if (hasText) continue;
            const r = c.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) continue;
            if (Math.max(r.width, r.height) <= maxPx) cands.push({ el: c, r });
        }
        return cands;
    }

    // Largest sub-group of near-equal-size (delta ≤2px) candidates. Returns [{el,r}].
    function largestSameSizeGroup(cands) {
        let best = [];
        for (const seed of cands) {
            const grp = cands.filter(c =>
                Math.abs(c.r.width - seed.r.width) <= 2 && Math.abs(c.r.height - seed.r.height) <= 2);
            if (grp.length > best.length) best = grp;
        }
        return best;
    }

    // Classify `parent`'s children as a decorative swarm:
    //   {decision:'drop', set, count, keyword}  — remove it (piles of dead layers)
    //   {decision:'review', count, els}         — swarm-SHAPED but NOT confident
    //       enough to drop (no keyword & N<12, or only forms in the wider size
    //       band) → keep it, but log for human review so new particle types /
    //       keywords can be added later after seeing real HTML.
    //   null                                    — not swarm-shaped
    function classifySwarm(parent) {
        const dropCands = smallTextlessLeaves(parent, DROP_MAX_PX);
        if (dropCands.length >= SWARM_FLOOR) {
            const best = largestSameSizeGroup(dropCands);
            if (best.length >= SWARM_FLOOR) {
                const els = best.map(c => c.el);
                const keyword = noiseMatch(parent) || els.some(noiseMatch);
                if (best.length >= 12 || keyword) {
                    return { decision: 'drop', set: new Set(els), count: best.length, keyword };
                }
            }
        }
        // Not dropped — is it still swarm-shaped under the wider review net?
        const revCands = smallTextlessLeaves(parent, REVIEW_MAX_PX);
        if (revCands.length >= SWARM_FLOOR) {
            const best = largestSameSizeGroup(revCands);
            if (best.length >= SWARM_FLOOR) {
                return { decision: 'review', count: best.length, els: best.map(c => c.el) };
            }
        }
        return null;
    }

    function labelOf(el) {
        return el.getAttribute('class')
            ? '.' + el.getAttribute('class').split(/\s+/)[0]
            : '<' + el.tagName.toLowerCase() + '>';
    }

    // Log a DROPPED swarm to warnings.
    function noteSwarm(el, swarm) {
        const how = swarm.keyword ? 'structural+keyword' : 'structural-only, N=' + swarm.count;
        skipWarnings.push('skipped decorative swarm: ' + swarm.count
            + ' leaves under ' + labelOf(el) + ' (match: ' + how + ')');
    }

    // Log a KEPT-but-suspect swarm to the review diagnostic as a STRUCTURED entry
    // (so utils/review_noise.py can aggregate cleanly), capturing the group's
    // distinct class tokens (candidate new keywords), size range, and a
    // human-readable message for eyeballing a single spec.
    function noteReview(el, swarm) {
        const tokens = new Set();
        let minS = Infinity, maxS = 0;
        for (const c of swarm.els) {
            for (const t of (c.getAttribute('class') || '').split(/\s+/)) if (t) tokens.add(t.toLowerCase());
            const r = c.getBoundingClientRect();
            const s = Math.max(r.width, r.height);
            if (s < minS) minS = s;
            if (s > maxS) maxS = s;
        }
        const classes = Array.from(tokens);
        const parent = labelOf(el);
        minS = Math.round(minS); maxS = Math.round(maxS);
        noiseReview.push({
            parent: parent,
            count: swarm.count,
            classes: classes,
            size_min: minS,
            size_max: maxS,
            message: 'potential swarm kept for review: ' + swarm.count + ' leaves under '
                + parent + ' (classes: ' + classes.join(', ')
                + '; sizes ' + minS + '–' + maxS + 'px; kept: no keyword & N<12)',
        });
    }

    // Route a classified swarm to the right log. Returns the drop-set (or null).
    function handleSwarm(el, swarm) {
        if (!swarm) return null;
        if (swarm.decision === 'drop') { noteSwarm(el, swarm); return swarm.set; }
        noteReview(el, swarm);
        return null;
    }
    // ───────────────────────────────────────────────────────────────────────

    function visit(el, parentUid, parentVisualUid, brightnessMul = 1) {
        if (SKIP_TAGS.has(el.tagName)) return;

        const r = rectOf(el);
        const hasSize = r.w >= 1 && r.h >= 1;
        const cs = styleSnapshot(el, null, brightnessMul);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return;

        const tag = el.tagName.toLowerCase();
        const isTextTag = TEXT_TAGS.has(el.tagName);
        const hasDirectText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
        // <br> is a line-break marker folded into `runs` as {text:'\n'} (see
        // extractRuns), never its own emitted element — it must NOT count as
        // "has element children" here, or a plain `Foo<br>Bar` text leaf gets
        // mis-forced into shape_type="frame" downstream, which disqualifies it
        // from the true-text-bbox sizing done elsewhere for box+text leaves
        // and leaves it sized to its own zero-margin DOM box width instead —
        // fragile to the smallest Chrome/Figma font-metric difference and
        // prone to mid-word wrap (scene_22 "HAPPENED" → "HAPPE"/"NED").
        const hasElementChildren = Array.from(el.children).some(c => !SKIP_TAGS.has(c.tagName) && c.tagName !== 'BR');

        // This element's OWN brightness() filter (if any) compounds on top of
        // the inherited multiplier — it darkens/brightens both its own paint
        // (background/border/text, applied below) AND everything it paints
        // underneath it, so descendants recurse with the compounded value.
        const ownBrightness = parseBrightnessOnly(cs.filter);
        const effectiveBrightness = ownBrightness !== null ? brightnessMul * ownBrightness : brightnessMul;
        if (ownBrightness !== null) {
            cs.backgroundColor = applyBrightness(cs.backgroundColor, ownBrightness);
            cs.borderTopColor = applyBrightness(cs.borderTopColor, ownBrightness);
            cs.borderRightColor = applyBrightness(cs.borderRightColor, ownBrightness);
            cs.borderBottomColor = applyBrightness(cs.borderBottomColor, ownBrightness);
            cs.borderLeftColor = applyBrightness(cs.borderLeftColor, ownBrightness);
            cs.color = applyBrightness(cs.color, ownBrightness);
            if (cs.webkitTextFillColor) cs.webkitTextFillColor = applyBrightness(cs.webkitTextFillColor, ownBrightness);
            if (cs.webkitTextStrokeColor) cs.webkitTextStrokeColor = applyBrightness(cs.webkitTextStrokeColor, ownBrightness);
        }

        const myUid = uid();
        const myZ = effectiveZ(el, docOrder++);
        el.setAttribute('data-extract-uid', myUid);  // for later screenshot lookup

        let visualParent = parentVisualUid;

        if (hasSize) {
            visualParent = myUid;
            
            // Coordinates relative to frameRoot
            const x = Math.round(r.x - frameRect.left);
            const y = Math.round(r.y - frameRect.top);
            const w = Math.round(r.w);
            const h = Math.round(r.h);

            const klass = classify(el, cs, hasDirectText, hasElementChildren, w, h);
            // Native gradient-bg container with children: needs a "bg-only" PNG so
            // children stay editable but the gradient still renders behind them.
            const isGradientContainer = klass === 'native'
                && cs.backgroundImage.includes('gradient')
                && hasElementChildren;

            // SVG: emit as raster image entry
            if (el.tagName === 'svg' || klass === 'raster') {
                out.push({
                    uid: myUid, parent_uid: parentVisualUid,
                    kind: 'raster',
                    x, y, w, h, z: myZ,
                    tag,
                    id: el.id || '',
                    className: el.className && el.className.baseVal !== undefined ? el.className.baseVal : (el.className || ''),
                    cssText: cs,
                    rasterTarget: true,   // builder will request screenshot
                    opacity: parseFloat(cs.opacity),
                    isCanvas: el.hasAttribute('data-detected-canvas'),
                });
                // Don't recurse into raster element (children captured in PNG)
                return;
            }

            // Native element: collect text + style
            let runs = null;
            if (hasDirectText) {
                runs = extractRuns(el, effectiveBrightness);
            }

            // Pseudo-elements: emit synthetic raster siblings if they have content
            const pseudos = [];
            for (const ps of ['::before', '::after']) {
                const psCS = styleSnapshot(el, ps, effectiveBrightness);
                const c = (psCS.content || '').trim();
                if (c && c !== 'none' && c !== 'normal') {
                    pseudos.push({pseudo: ps, cs: psCS});
                }
            }

            const elem = {
                uid: myUid, parent_uid: parentVisualUid,
                kind: 'native',
                tag,
                x, y, w, h, z: myZ,
                id: el.id || '',
                className: typeof el.className === 'string' ? el.className : '',
                cssText: cs,
                runs,
                hasElementChildren,
                pseudos,
                opacity: parseFloat(cs.opacity),
                isGradientContainer: isGradientContainer,
                textRect: hasDirectText ? textBoxOffset(el, x, y) : null,
                isCanvas: el.hasAttribute('data-detected-canvas'),
            };
            out.push(elem);
            // Gradient container's bg-only PNG is captured later in Python via
            // strip-children/screenshot/restore — no clone needed here.

            // Pseudo-elements: if non-empty, emit as raster (since exact placement of ::before/::after is complex)
            for (const p of pseudos) {
                // Skip for now — pseudo-elements often used for decorations.
                // The parent's bbox already includes them visually if positioned absolutely inside.
                // TODO: more precise extraction via getBoxQuads if needed.
            }
        }

        // Recurse: if element holds text (runs), treat its inline children as part of runs — stop recursion.
        // Else recurse into element children to capture nested layout.
        if (hasDirectText && !hasElementChildren) return;
        if (hasDirectText && hasElementChildren) {
            // Mixed content: keep runs, but also recurse for non-text element children
            // (e.g., <div>icon<span>label</span></div> — but if span is inline text, runs already captured it)
            // Simpler: don't recurse if runs captured everything. Heuristic: if any child is block-level, recurse.
            let anyBlock = false;
            for (const c of el.children) {
                if (SKIP_TAGS.has(c.tagName)) continue;
                const ccs = window.getComputedStyle(c);
                if (ccs.display !== 'inline' && ccs.display !== 'inline-block' && ccs.display !== 'contents') {
                    anyBlock = true; break;
                }
            }
            if (!anyBlock) return;
        }

        const dropSet = handleSwarm(el, classifySwarm(el));
        for (const child of el.children) {
            if (dropSet && dropSet.has(child)) continue;
            visit(child, parentUid, visualParent, effectiveBrightness);
        }
    }

    // Snapshot top-level children BEFORE walk — visit() mutates body by appending
    // offscreen bg-clones, and body.children is a live HTMLCollection that would
    // otherwise pick them up mid-iteration and skew normalization.
    const topLevel = Array.from(frameRoot.children);
    const rootDropSet = handleSwarm(frameRoot, classifySwarm(frameRoot));
    for (const child of topLevel) {
        if (rootDropSet && rootDropSet.has(child)) continue;
        visit(child, null, null);
    }
    // Also include frameRoot's own background if it has one
    const rootCS = styleSnapshot(frameRoot, null);

    return {
        elements: out,
        warnings: skipWarnings,
        noiseReview: noiseReview,
        frameWidth: Math.round(frameRect.width),
        frameHeight: Math.round(frameRect.height),
        bodyBg: bodyCS.backgroundColor,
        rootBg: rootCS.backgroundColor,
        rootBgImage: rootCS.backgroundImage,
    };
}
"""


# ═════════════════════════════════════════════════════════════════════════════
# Design-size detection
#   canvas   — largest element with an explicit fixed px width AND height
#              (a self-contained "scene canvas" like 1280×720 / 1920×1080).
#              Frame should match this exactly (no margin).
#   maxWidth — largest fixed px max-width (responsive card layouts like 1100).
# ═════════════════════════════════════════════════════════════════════════════

_DETECT_DESIGN_JS = r"""
() => {
    // Selectors that explicitly set BOTH width and height in px (stylesheets),
    // keeping the AUTHORED px values so we can use them instead of the rendered
    // box (which flex-shrink can squeeze below the design size — see below).
    const fixedRules = [];
    for (const sheet of document.styleSheets) {
        let rules;
        try { rules = sheet.cssRules; } catch (e) { continue; }
        if (!rules) continue;
        for (const rule of rules) {
            if (rule.style && rule.style.width && rule.style.height
                && rule.style.width.endsWith('px') && rule.style.height.endsWith('px')
                && rule.selectorText) {
                fixedRules.push({sel: rule.selectorText,
                                 w: parseFloat(rule.style.width),
                                 h: parseFloat(rule.style.height)});
            }
        }
    }
    let maxW = 0;
    let canvas = null, canvasArea = 0, canvasEl = null;
    for (const el of document.querySelectorAll('*')) {
        const cs = window.getComputedStyle(el);
        if (cs.maxWidth && cs.maxWidth.endsWith('px')) {
            const v = parseFloat(cs.maxWidth);
            if (isFinite(v) && v > maxW) maxW = v;
        }
        // Authored fixed px width+height (inline style or a matched stylesheet
        // rule)? Use the AUTHORED values, NOT getBoundingClientRect: a fixed design
        // canvas that is a flex item gets shrunk below its authored width when the
        // probe viewport is narrower than the design (default flex-shrink:1) — e.g.
        // a 1920px canvas probed at 1600px measures 1600, squeezing the whole layout
        // (scene_24 title wrapped to 2 lines). Authored px is the design intent.
        let aw = 0, ah = 0;
        if (el.style.width.endsWith('px') && el.style.height.endsWith('px')) {
            aw = parseFloat(el.style.width); ah = parseFloat(el.style.height);
        } else {
            for (const fr of fixedRules) {
                try { if (el.matches(fr.sel)) { aw = fr.w; ah = fr.h; break; } } catch (e) {}
            }
        }
        // ≥600px wide qualifies as a design canvas (skip small fixed boxes/icons).
        // Must also wrap content (children.length > 0): a real design canvas always
        // contains the scene's elements, whereas decorative effect divs (glow/blur/
        // vignette overlays) are empty leaves that can coincidentally match the same
        // authored px size (e.g. .ambient-glow--1 700×700 vs the true fit-content
        // canvas — scene_9 mispicked the glow as "the" 700×700 canvas).
        if (aw >= 600 && aw * ah > canvasArea && el.children.length > 0) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {   // element actually rendered (not display:none)
                canvas = {width: Math.round(aw), height: Math.round(ah)};
                canvasArea = aw * ah;
                canvasEl = el;
            }
        }
    }
    // Fullscreen detection: handles Tailwind w-full h-screen (and similar) layouts
    // where width/height are viewport-relative, not explicit px values.
    // Height must be ~viewport (a real h-screen hero), NOT merely ≥90%: a tall
    // scrolling section (e.g. a single full-width image taller than the viewport)
    // would otherwise be misread as fullscreen and clipped to viewport height.
    if (!canvas) {
        const firstChild = document.body.firstElementChild;
        if (firstChild) {
            const r = firstChild.getBoundingClientRect();
            const vw = window.innerWidth, vh = window.innerHeight;
            if (r.width >= vw * 0.95 && r.height >= vh * 0.9 && r.height <= vh * 1.1) {
                canvas = {width: Math.round(vw), height: Math.round(vh)};
                canvasEl = firstChild;
            }
        }
    }
    // Tag the actual winning DOM element so the later extraction walk (which
    // runs AFTER animations are frozen — a scale/translateZ camera animation on
    // this very element would otherwise distort its measured size well past
    // any authored-size tolerance) can identify it by IDENTITY, not by
    // re-matching its rendered size at a different point in time.
    if (canvasEl) canvasEl.setAttribute('data-detected-canvas', '');
    return {canvas, maxWidth: maxW || null};
}
"""

# Measures the tagged canvas element's viewport-relative position. Must be
# called AFTER any probe-viewport resize (so it reflects the final layout) but
# BEFORE animations are frozen — a scale/translateZ "camera" animation on the
# canvas element itself (common: a fixed-size .scene-container with its own
# push-in/zoom keyframe) renders at a different position/size once frozen at
# its peak, so measuring post-freeze (or worse, post-raster-union-sync, which
# can be wildly different again) badly misplaces the whole canvas origin.
_MEASURE_CANVAS_JS = r"""
() => {
    const el = document.querySelector('[data-detected-canvas]');
    if (!el) return null;
    const body = document.body.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    // Frame-root-relative (matches _EXTRACT_JS's `x = r.x - frameRect.left`
    // convention for every other element, since findFrameRoot() is always
    // document.body) — comparable to raw_elements' x/y before the origin shift.
    return {x: r.left - body.left, y: r.top - body.top};
}
"""


# viewport_width=None → auto-detect the design width from the layout's own
# max-width (split/wide layouts like a 1100px two-column scene render squished
# at the old 600px default). An explicit value always overrides.
PROBE_WIDTH = 1600
DEFAULT_WIDTH = 600

# User-curated extra decorative-noise keywords, merged on top of the built-in core
# (defined inside _EXTRACT_JS). Path overridable via NOISE_KEYWORDS_FILE (tests).
_NOISE_KEYWORDS_FILE = Path(__file__).resolve().parent.parent / "config" / "noise_keywords.txt"


def load_noise_keywords() -> list[str]:
    """Return extra noise keywords from config/noise_keywords.txt (one token per
    line; '#' comments and blank lines ignored). Missing file → empty list. These
    are ADDED to the JS core list, never replace it, so the safe core is protected."""
    path = os.environ.get("NOISE_KEYWORDS_FILE")
    path = Path(path) if path else _NOISE_KEYWORDS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    words = []
    for line in lines:
        token = line.strip().lower()
        if token and not token.startswith("#"):
            words.append(token)
    return words
# Fully-fluid responsive pages expose no design width (no fixed canvas, no px
# max-width). They are desktop-first: rendering at 600 collapses responsive
# multi-column grids to their mobile (1-column) breakpoint. Render at a
# desktop width so the intended desktop layout resolves.
DESKTOP_WIDTH = 1280


def decide_design_viewport(design: dict) -> dict:
    """Pure decision: given _DETECT_DESIGN_JS's result, choose the probe
    viewport width/height and whether this is a fixed-canvas design.

    Shared with utils/render_html.py so both tools probe the page at the
    identical viewport (a fluid page can lay out differently at different
    widths, so the QC reference must match the width the extractor used).
    Returns {"chosen_w", "chosen_h", "canvas_mode", "canvas_dims", "warning"}.
    """
    canvas = design.get("canvas")
    maxw = design.get("maxWidth")
    chosen_h = 900
    canvas_mode = False
    canvas_dims = None
    if canvas:
        # Fixed canvas → render at its exact size; frame = canvas (no margin).
        chosen = int(min(canvas["width"], 1920))
        chosen_h = int(min(canvas["height"], 1920))
        canvas_mode = True
        canvas_dims = (chosen, chosen_h)
        warning = f"auto canvas {chosen}×{chosen_h}px (fixed design canvas)"
    elif maxw and maxw > DEFAULT_WIDTH:
        chosen = int(min(maxw, 1920))
        warning = f"auto viewport width = {chosen}px (detected design max-width {int(maxw)}px)"
    elif maxw is None:
        # No width signal at all → fluid desktop-first page. Use desktop
        # width so responsive grids don't collapse to the mobile breakpoint.
        chosen = DESKTOP_WIDTH
        warning = f"auto viewport width = {chosen}px (fluid responsive page, no max-width — desktop default)"
    else:
        # A px max-width was detected but it's ≤ 600 → genuinely small card
        # design; keep the narrow default.
        chosen = DEFAULT_WIDTH
        warning = None
    return {"chosen_w": chosen, "chosen_h": chosen_h, "canvas_mode": canvas_mode,
            "canvas_dims": canvas_dims, "warning": warning}


def compute_frame_size(raw_elements: list[dict], frame_w: int, frame_h: int,
                        canvas_mode: bool, canvas_dims: tuple[int, int] | None,
                        canvas_pos: tuple[float, float] | None = None) -> dict:
    """Pure geometry decision — two modes:
      • canvas_mode: a fixed design canvas was detected → frame = canvas size
        EXACTLY (no margin). Origin = the canvas element's top-left.
      • card-mode: frame = real content bbox + 100px margin on all sides.
        Full-frame ambient overlays are excluded so they don't pin the frame
        to the viewport size.

    Shared with utils/render_html.py so the Figma frame and the QC reference
    screenshot always measure the identical region of the page (previously
    render_html.py cropped to the bare body bbox with no margin, while a
    decorative element bleeding past the body's own edge — e.g. an ambient
    glow positioned with `right:-10%` — could also widen this bbox, so the
    two "ground truth" outputs silently disagreed on the frame's size).

    Returns {"adj_w", "adj_h", "origin_x", "origin_y"}. Does not mutate
    raw_elements — callers apply the origin shift themselves.
    """
    PAD = 100
    if raw_elements and canvas_mode and canvas_dims:
        cw, ch = canvas_dims
        # Prefer `canvas_pos` — measured (by the caller, via _MEASURE_CANVAS_JS)
        # right after the probe-viewport resize but BEFORE animations freeze.
        # This is the only reliably stable anchor: the canvas element itself
        # commonly carries its own scale/translateZ "camera" animation, so ANY
        # measurement taken after freezing (or worse, after the raster loop's
        # union-with-descendants geometry sync) reflects that animation's peak
        # transform or a wildly-scattered union bbox — NOT the canvas's true
        # resting position (confirmed on real scenes with such a camera-push
        # container: both later measurements misplaced the whole canvas).
        if canvas_pos is not None:
            origin_x, origin_y = canvas_pos
        else:
            # Fallback (canvas_pos unavailable — e.g. an older caller): locate
            # the canvas element by identity tag, then by authored-size match.
            # Both are less reliable than canvas_pos for the reason above.
            cands = [r for r in raw_elements if r.get("isCanvas")]
            if not cands:
                cands = [r for r in raw_elements
                          if abs(r.get("_orig_w", r["w"]) - cw) <= 3 and abs(r.get("_orig_h", r["h"]) - ch) <= 3]
            if cands:
                origin_x, origin_y = cands[0]["x"], cands[0]["y"]
            else:
                origin_x, origin_y = min(r["x"] for r in raw_elements), min(r["y"] for r in raw_elements)
        return {"adj_w": cw, "adj_h": ch, "origin_x": origin_x, "origin_y": origin_y}
    elif raw_elements:
        def _is_full_frame_bg(r):
            return r["w"] >= frame_w * 0.95 and r["h"] >= frame_h * 0.95
        content = [r for r in raw_elements if not _is_full_frame_bg(r)]
        if not content:                      # all elements are full-frame → use them
            content = raw_elements
        min_x = min(r["x"] for r in content)
        min_y = min(r["y"] for r in content)
        max_x = max(r["x"] + r["w"] for r in content)
        max_y = max(r["y"] + r["h"] for r in content)
        # Shift everything so content starts at (PAD, PAD). Full-frame overlays
        # move with it (they sit behind, clipped by the frame on export).
        origin_x = min_x - PAD
        origin_y = min_y - PAD
        return {"adj_w": (max_x - min_x) + 2 * PAD, "adj_h": (max_y - min_y) + 2 * PAD,
                "origin_x": origin_x, "origin_y": origin_y}
    else:
        return {"adj_w": frame_w, "adj_h": frame_h, "origin_x": 0, "origin_y": 0}


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
                a.pause();
                a.currentTime = (timing.delay || 0) + peak.offset * duration;
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


# ═════════════════════════════════════════════════════════════════════════════
# CSS parsers (Python side)
# ═════════════════════════════════════════════════════════════════════════════

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_RGB_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")


def _color_to_rgba(css_color: str) -> dict | None:
    """Parse any CSS color → {r,g,b,a} 0..1 floats. Returns None if transparent/invalid."""
    if not css_color:
        return None
    s = css_color.strip()
    if s in ("none", "transparent", "currentColor"):
        return None
    m = _RGB_RE.search(s)
    if m:
        r, g, b = float(m[1]) / 255.0, float(m[2]) / 255.0, float(m[3]) / 255.0
        a = float(m[4]) if m[4] is not None else 1.0
        if a <= 0:
            return None
        return {"r": round(r, 4), "g": round(g, 4), "b": round(b, 4), "a": round(a, 4)}
    m = _HEX_RE.match(s)
    if m:
        hx = m[1]
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        elif len(hx) == 4:
            hx = "".join(c * 2 for c in hx)
        if len(hx) == 6:
            r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            a = 1.0
        elif len(hx) == 8:
            r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            a = int(hx[6:8], 16) / 255.0
        else:
            return None
        return {"r": round(r / 255.0, 4), "g": round(g / 255.0, 4), "b": round(b / 255.0, 4), "a": round(a, 4)}
    return None


def _px(s: str | None) -> float:
    if not s:
        return 0.0
    m = re.match(r"\s*(-?[\d.]+)px", s)
    return float(m[1]) if m else 0.0


def _px_or_pct(s: str | None, ref: float) -> float:
    """Parse pixel value OR percentage (relative to `ref`)."""
    if not s:
        return 0.0
    m = re.match(r"\s*(-?[\d.]+)px", s)
    if m:
        return float(m[1])
    m = re.match(r"\s*(-?[\d.]+)%", s)
    if m:
        return float(m[1]) / 100.0 * ref
    return 0.0


def _split_top_level_commas(s: str) -> list[str]:
    """Split by commas that are NOT inside parens. For multi-value CSS like box-shadow."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _parse_gradient_stops(inner: str, default_angle: float = 180.0) -> tuple[float, list[dict]] | None:
    """Parse gradient args: `angle, color [pos], color [pos], ...`. Returns (angle_deg, stops)."""
    angle = default_angle
    # Angle
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)deg\s*,\s*", inner)
    if m:
        angle = float(m[1])
        inner = inner[m.end():]
    else:
        # "to top/bottom/left/right"
        m2 = re.match(r"\s*to\s+([a-z\s]+)\s*,\s*", inner)
        if m2:
            dir_map = {
                "top": 0, "bottom": 180, "right": 90, "left": 270,
                "top right": 45, "right top": 45,
                "bottom right": 135, "right bottom": 135,
                "bottom left": 225, "left bottom": 225,
                "top left": 315, "left top": 315,
            }
            angle = dir_map.get(m2[1].strip(), 180)
            inner = inner[m2.end():]

    # Stops — split top-level commas, each is "color [position]"
    raw_stops = _split_top_level_commas(inner)
    stops = []
    for i, raw in enumerate(raw_stops):
        # Extract color token
        cm = re.search(r"(rgba?\([^)]+\)|#[0-9a-fA-F]+)", raw)
        if not cm:
            continue
        color = _color_to_rgba(cm[0])
        if not color:
            color = {"r": 0, "g": 0, "b": 0, "a": 0}
        # Extract position (optional, %)
        pm = re.search(r"([\d.]+)%", raw)
        if pm:
            pos = float(pm[1]) / 100.0
        else:
            pos = i / max(1, len(raw_stops) - 1)
        stops.append({"position": round(pos, 4), "color": color})

    if len(stops) < 2:
        return None
    return angle, stops


def _parse_gradient(bg_image: str) -> dict | None:
    """Parse linear-gradient/radial-gradient → Fill dict."""
    if not bg_image or bg_image == "none":
        return None
    s = bg_image.strip()
    m = re.match(r"linear-gradient\((.+)\)\s*$", s, re.DOTALL)
    if m:
        parsed = _parse_gradient_stops(m[1], default_angle=180.0)
        if not parsed:
            return None
        angle, stops = parsed
        return {"type": "GRADIENT_LINEAR", "angle": angle, "stops": stops}
    m = re.match(r"radial-gradient\((.+)\)\s*$", s, re.DOTALL)
    if m:
        inner = m[1]
        # Strip shape/size hints up to first comma if present
        shape_m = re.match(r"\s*(circle|ellipse)[^,]*,\s*", inner)
        if shape_m:
            inner = inner[shape_m.end():]
        parsed = _parse_gradient_stops(inner, default_angle=0.0)
        if not parsed:
            return None
        _, stops = parsed
        return {"type": "GRADIENT_RADIAL", "stops": stops}
    return None


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


def _parse_shadows(shadow_str: str) -> list[dict]:
    """Parse `box-shadow` value (can be multi). Returns list of Effect dicts."""
    if not shadow_str or shadow_str == "none":
        return []
    out = []
    for one in _split_top_level_commas(shadow_str):
        e = _parse_one_shadow(one)
        if e:
            out.append(e)
    return out


def _parse_one_shadow(s: str) -> dict | None:
    color_m = re.search(r"(rgba?\([^)]+\)|#[0-9a-fA-F]+)", s)
    if not color_m:
        return None
    color = _color_to_rgba(color_m[0])
    if not color:
        return None
    rest = s.replace(color_m[0], " ")
    inset = "inset" in rest
    rest = rest.replace("inset", " ")
    values = re.findall(r"(-?[\d.]+)px", rest)
    if len(values) < 3:
        return None
    ox, oy, blur = float(values[0]), float(values[1]), float(values[2])
    spread = float(values[3]) if len(values) > 3 else 0.0
    return {
        "type": "INNER_SHADOW" if inset else "DROP_SHADOW",
        "color": color,
        "offset": {"x": round(ox, 2), "y": round(oy, 2)},
        "radius": round(blur, 2),
        "spread": round(spread, 2),
    }


def _parse_filter_drop_shadows(filter_str: str) -> list[dict]:
    """Parse `filter: drop-shadow(...)` occurrences → DROP_SHADOW effects.

    CSS filter drop-shadow has the same `offx offy blur color` token shape as
    box-shadow but never carries spread or inset, so we reuse _parse_one_shadow.
    """
    if not filter_str or filter_str == "none":
        return []
    out = []
    for m in re.finditer(r"drop-shadow\(((?:[^()]|\([^()]*\))*)\)", filter_str):
        e = _parse_one_shadow(m.group(1))
        if e:
            e["type"] = "DROP_SHADOW"  # filter drop-shadow is never inset
            out.append(e)
    return out


def _parse_filter_blur(filter_str: str) -> float | None:
    """Parse `filter: blur(Npx)` → radius in px, or None if absent."""
    if not filter_str or filter_str == "none":
        return None
    m = re.search(r"blur\(([\d.]+)px\)", filter_str)
    return float(m[1]) if m else None


def _parse_transform_rotation(transform: str) -> float:
    """Extract rotation angle (degrees) from CSS transform matrix. Returns 0 if none."""
    if not transform or transform == "none":
        return 0.0
    m = re.match(r"matrix\(\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),\s*([-\d.]+),", transform)
    if m:
        a, b = float(m[1]), float(m[2])
        return round(math.degrees(math.atan2(b, a)), 2)
    m = re.search(r"rotate\(\s*(-?[\d.]+)deg\s*\)", transform)
    if m:
        return float(m[1])
    return 0.0


def _font_weight_num(w: str) -> int:
    try:
        return int(w)
    except (ValueError, TypeError):
        return {"normal": 400, "bold": 700, "lighter": 300, "bolder": 700}.get(str(w), 400)


def _first_font_family(ff: str) -> str:
    if not ff:
        return "Inter"
    first = ff.split(",")[0].strip().strip("'\"")
    return first or "Inter"


# ═════════════════════════════════════════════════════════════════════════════
# Build spec from raw element data
# ═════════════════════════════════════════════════════════════════════════════

def _build_text_runs(raw_runs: list[dict], warnings: list[str], uid: str) -> list[dict]:
    """Convert JS-extracted runs into spec TextRun list."""
    out = []
    for r in raw_runs or []:
        if r.get("_br"):
            out.append({"text": "\n", "_br": True})
            continue
        text = r.get("text", "")
        if not text:
            continue
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
        if fills is None:
            # None means genuinely transparent/invalid CSS color (e.g. rim-only
            # text with color:transparent) — leave fills empty rather than
            # defaulting to opaque black, which would paint a solid glyph body
            # over what the browser actually renders as invisible.
            color = _color_to_rgba(r.get("color", ""))
            fills = [{"type": "SOLID", "color": color}] if color else []
        out.append({
            "text": text,
            "font_family": _first_font_family(r.get("fontFamily", "")),
            "font_size": round(_px(r.get("fontSize", "")) or 14, 2),
            "font_weight": _font_weight_num(r.get("fontWeight", "400")),
            "italic": r.get("fontStyle") == "italic",
            "fills": fills,
            "letter_spacing": round(_px(r.get("letterSpacing", "")), 2),
            "decoration": _decoration(r.get("textDecorationLine", "")),
        })
    # Strip leading/trailing pure-whitespace runs
    while out and not out[0].get("text", "").strip() and not out[0].get("_br"):
        out.pop(0)
    while out and not out[-1].get("text", "").strip() and not out[-1].get("_br"):
        out.pop()
    return out


def _decoration(s: str) -> str:
    s = (s or "").lower()
    if "underline" in s:
        return "UNDERLINE"
    if "line-through" in s:
        return "STRIKETHROUGH"
    return "NONE"


def _emit_native_element(raw: dict, uid_to_id: dict[str, str], elements_out: list[dict], assets_dir: str, warnings: list[str]) -> None:
    """Convert one raw native element → 0, 1, or many spec elements."""
    cs = raw["cssText"]
    x, y, w, h = raw["x"], raw["y"], raw["w"], raw["h"]
    runs = _build_text_runs(raw.get("runs"), warnings, raw["uid"]) if raw.get("runs") else None
    has_text = bool(runs)
    el_id = uid_to_id[raw["uid"]]
    parent_id = uid_to_id.get(raw.get("parent_uid"))

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

    # ─── Strokes (per-side borders) ─────────────────────────────────────────
    btw = _px(cs.get("borderTopWidth"))
    brw = _px(cs.get("borderRightWidth"))
    bbw = _px(cs.get("borderBottomWidth"))
    blw = _px(cs.get("borderLeftWidth"))
    btc = _color_to_rgba(cs.get("borderTopColor", ""))
    brc = _color_to_rgba(cs.get("borderRightColor", ""))
    bbc = _color_to_rgba(cs.get("borderBottomColor", ""))
    blc = _color_to_rgba(cs.get("borderLeftColor", ""))

    uniform = (btw == brw == bbw == blw) and btw > 0 and (btc == brc == bbc == blc) and btc is not None

    strokes = []
    stroke_weight = 0
    if uniform:
        strokes = [{"type": "SOLID", "color": btc}]
        stroke_weight = btw

    # Gradient containers render their border INSIDE the BG-Gradient PNG (the PNG
    # is a screenshot of the bordered element). Drop the frame's own stroke to
    # avoid a doubled / axis-misaligned border (the frame stroke is axis-aligned
    # but the PNG content may be rotated by a parent transform).
    if has_gradient_bg:
        strokes = []
        stroke_weight = 0

    # ─── Per-corner radii (handles % values, common for circles) ────────────
    ref = min(w, h)
    tl = _px_or_pct(cs.get("borderTopLeftRadius"), ref)
    tr = _px_or_pct(cs.get("borderTopRightRadius"), ref)
    br = _px_or_pct(cs.get("borderBottomRightRadius"), ref)
    bl = _px_or_pct(cs.get("borderBottomLeftRadius"), ref)
    corner_radii = [round(tl), round(tr), round(br), round(bl)]
    # Cap radius to half the smaller dimension (Figma clamps anyway)
    cap = min(w, h) / 2
    corner_radii = [int(min(v, cap)) for v in corner_radii]

    # ─── Effects ────────────────────────────────────────────────────────────
    effects = _parse_shadows(cs.get("boxShadow", ""))
    # A hard-rim outset box-shadow (≈0 offset, ≈0 blur, positive spread) — e.g.
    # `box-shadow: 0 0 0 1px rgba(...)` — is an OUTLINE, not a shadow. Figma paints
    # DROP_SHADOW as the layer's silhouette BEHIND the layer, so on a translucent
    # fill it floods the whole interior with the shadow color (CSS never does this:
    # the box occludes its own outset shadow, only the 1px ring peeks out). Treat it
    # as an outline: adopt it as the stroke if the element has none, otherwise drop
    # it (the existing border already supplies the rim) — avoids both the flood and
    # a doubled ring.
    _rim = [e for e in effects
            if e["type"] == "DROP_SHADOW" and e["radius"] <= 0.5
            and abs(e["offset"]["x"]) <= 0.5 and abs(e["offset"]["y"]) <= 0.5
            and e["spread"] > 0]
    if _rim:
        effects = [e for e in effects if e not in _rim]
        if not strokes:
            strokes = [{"type": "SOLID", "color": _rim[0]["color"]}]
            stroke_weight = max(1, round(_rim[0]["spread"]))
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

    # ─── Decision: ellipse vs rectangle vs frame ─────────────────────────────
    is_circle = (abs(w - h) <= 1) and all(c >= min(w, h) / 2 - 1 for c in corner_radii) and any(c > 0 for c in corner_radii)
    has_visual = bool(fills) or bool(strokes) or bool(effects) or has_gradient_bg
    clip_content = cs.get("overflow") in ("hidden", "clip", "auto", "scroll")
    rotation = _parse_transform_rotation(cs.get("transform", ""))
    opacity = raw.get("opacity", 1.0)

    # Common base
    base = {
        "id": el_id,
        "parent_id": parent_id,
        "x": x, "y": y, "width": w, "height": h,
        "rotation": rotation,
        "opacity": round(opacity, 3),
        "z": raw["z"],
    }
    name_hint = raw.get("id") or (raw.get("className", "").split()[0] if raw.get("className") else raw["tag"])

    # ─── Emit shape (if any visual or needs grouping) ───────────────────────
    needs_children = raw.get("hasElementChildren") or has_gradient_bg
    # Pure-layout container: a div with element children but no visual of its own
    # (no fill/border/effect/gradient-bg), overflow visible, no rotation/opacity
    # grouping. Emitting it as a Figma frame would CLIP its children — Figma frames
    # clip by default and figma-mcp-go exposes no API to disable it, so any child
    # positioned outside the container's box (e.g. an absolutely-positioned label
    # at top:-38px) disappears. Skip the frame; Pass 3 promotes the children to the
    # nearest emitted ancestor, preserving their absolute positions without clipping.
    is_pure_layout_container = (
        raw.get("hasElementChildren")
        and not has_visual
        and not has_gradient_bg
        and not clip_content
        and rotation == 0
        and opacity >= 0.999
    )
    if is_pure_layout_container:
        needs_children = False
    emit_shape = has_visual or needs_children
    if emit_shape:
        # Rectangles and ellipses in Figma cannot have children. Anything with
        # element children OR a bg-only child PNG MUST become a frame.
        if needs_children:
            shape_type = "frame"
        elif is_circle:
            shape_type = "ellipse"
        else:
            shape_type = "rectangle"
        shape_elem = {
            **base,
            "type": shape_type,
            "name": f"[{name_hint}/{shape_type}]",
            "fills": fills,
            "strokes": strokes,
            "stroke_weight": stroke_weight,
            "stroke_align": "INSIDE",
            "corner_radii": corner_radii,
            "effects": effects,
            # Respect actual CSS overflow only. A gradient-bg container is NOT
            # force-clipped: its bg PNG is exactly frame-sized (no bleed) with the
            # rounded corners + border baked in, so it never overflows. Forcing
            # clip would cut off children that legitimately overflow under
            # overflow:visible — e.g. flex content taller than a fixed-height box
            # (scene_20 node labels overflowing a 100px box).
            "clip_content": clip_content,
        }
        elements_out.append(shape_elem)

        # Emit gradient bg as first child (image at 0,0 of frame, rendered behind native children).
        # x/y stored as ABSOLUTE to match the spec convention; builder converts to local.
        if has_gradient_bg:
            bg_asset = str(Path(assets_dir) / raw["_bg_asset_filename"])
            elements_out.append({
                "id": el_id + "_bg",
                "parent_id": el_id,  # child of the frame
                "type": "image",
                "name": f"[{name_hint}/BG-Gradient]",
                "x": x, "y": y, "width": w, "height": h,
                "rotation": 0,
                "opacity": 1.0,
                "z": -10**9,  # sentinel: always render at bottom among siblings
                "image_path": bg_asset,
            })

        # Non-uniform borders → emit thin rect lines (per-side).
        # Skip for gradient containers (border already baked into the BG PNG).
        if not uniform and not has_gradient_bg:
            for side, bw, color in [
                ("top",    btw, btc),
                ("right",  brw, brc),
                ("bottom", bbw, bbc),
                ("left",   blw, blc),
            ]:
                if bw > 0 and color:
                    if side == "top":
                        sx, sy, sw, sh = x, y, w, max(1, round(bw))
                    elif side == "bottom":
                        sx, sy, sw, sh = x, y + h - max(1, round(bw)), w, max(1, round(bw))
                    elif side == "left":
                        sx, sy, sw, sh = x, y, max(1, round(bw)), h
                    else:  # right
                        sx, sy, sw, sh = x + w - max(1, round(bw)), y, max(1, round(bw)), h
                    elements_out.append({
                        "id": el_id + f"_b{side[0]}",
                        "parent_id": parent_id,
                        "type": "rectangle",
                        "name": f"[{name_hint}/Border-{side}]",
                        "x": sx, "y": sy, "width": sw, "height": sh,
                        "rotation": 0, "opacity": round(opacity, 3),
                        "z": raw["z"],
                        "fills": [{"type": "SOLID", "color": color}],
                        "strokes": [], "stroke_weight": 0, "stroke_align": "INSIDE",
                        "corner_radii": [0, 0, 0, 0],
                        "effects": [], "clip_content": False,
                    })

    # ─── Text element ───────────────────────────────────────────────────────
    if has_text:
        # If no shape was emitted, this text element stands alone.
        # Use computed text-align.
        text_align = (cs.get("textAlign") or "left").upper()
        if text_align not in ("LEFT", "CENTER", "RIGHT", "JUSTIFY"):
            text_align = "LEFT"

        lh = cs.get("lineHeight", "")
        line_height = None
        if lh and lh != "normal":
            line_height = round(_px(lh), 2) if "px" in lh else None

        # Build name from first run text
        preview = (runs[0]["text"] if runs else "").strip()[:24].replace("\n", " ")
        # filter:blur()/drop-shadow() (glow/soft shadow) on this text node.
        # Only apply here when this text IS the whole leaf (no element
        # children) — a mixed node (text + element children sharing this
        # DOM node's own filter) already gets the SAME filter attached to
        # the emitted FRAME (see is_pure_text_leaf in the Effects block
        # above), so re-applying it here would double it.
        text_filter_str = cs.get("filter", "") if not raw.get("hasElementChildren") else ""
        text_effects = _parse_filter_drop_shadows(text_filter_str)
        text_filter_blur_radius = _parse_filter_blur(text_filter_str)
        if text_filter_blur_radius is not None:
            text_effects.append({"type": "LAYER_BLUR", "radius": text_filter_blur_radius})
        # -webkit-text-stroke (glyph rim/outline) → Figma text-node stroke, so the
        # text stays editable with its outline (e.g. scene_18 glowing "?"). Figma
        # text strokes paint centered on the glyph path like CSS text-stroke.
        text_strokes = []
        text_stroke_weight = 0
        tsw = _px(cs.get("webkitTextStrokeWidth"))
        tsc = _color_to_rgba(cs.get("webkitTextStrokeColor", ""))
        if tsw > 0 and tsc and tsc.get("a", 0) > 0:
            text_strokes = [{"type": "SOLID", "color": tsc}]
            text_stroke_weight = tsw
        text_elem = {
            **base,
            "id": el_id + ("_t" if emit_shape else ""),
            "parent_id": el_id if (emit_shape and shape_type == "frame") else parent_id,
            "type": "text",
            "name": f"[{name_hint}/Text-{preview or 'empty'}]",
            "runs": runs,
            "text_align": text_align,
            "line_height": line_height,
            "effects": text_effects,
            "strokes": text_strokes,
            "stroke_weight": text_stroke_weight,
            "stroke_align": "CENTER",
        }
        # When text is painted directly on a plain box (rectangle/ellipse), `base`
        # is the BOX geometry, not the text's. A single-word label that the browser
        # lays out on one line OVERFLOWING a narrow fixed box would otherwise be
        # (a) misread as multi-line (box height ≫ line height) and (b) resized to
        # the box width → the builder force-wraps the word (scene_22 .domino
        # "Understanding"/"Customers"). Replace with the text's ACTUAL rendered rect
        # so line count + glyph position are faithful. Scoped to box+text without a
        # frame (no element children) and unrotated (a rotated box's text AABB would
        # fight the rotation node property).
        tr = raw.get("textRect")
        if tr and emit_shape and shape_type != "frame" and rotation == 0:
            text_elem["x"] = round(base["x"] + tr["dx"])
            text_elem["y"] = round(base["y"] + tr["dy"])
            text_elem["width"] = tr["w"]
            text_elem["height"] = tr["h"]
        # Chrome's and Figma's text-shaping engines measure the same
        # font/size/letter-spacing a few px apart. That's invisible when a box
        # has real slack, but when the emitted width is a TIGHT fit to the
        # glyphs' own measured width (shrink-to-fit auto-width text, or the
        # true-bbox override just above) there's zero margin to absorb the
        # difference — Figma can wrap, even mid-word, text that renders on one
        # line in the browser (scene_22 "HAPPENED" → "HAPPE"/"NED", confirmed
        # by resizing the built node: +5px was enough to stop the wrap).
        # Nudge the width out by a small cushion ONLY at that zero-slack
        # boundary — an author-sized box with real wrap slack is untouched.
        if tr and abs(text_elem["width"] - tr["w"]) <= 2:
            text_elem["width"] += 6
        elements_out.append(text_elem)


def _emit_raster_element(raw: dict, asset_path: str, uid_to_id: dict[str, str], elements_out: list[dict]) -> None:
    el_id = uid_to_id[raw["uid"]]
    parent_id = uid_to_id.get(raw.get("parent_uid"))
    name_hint = raw.get("id") or (raw.get("className", "").split()[0] if raw.get("className") else raw["tag"])
    # Bleed expansion (fix A): PNG is larger than DOM bbox to include glow/shadow
    # fade-out. Shift x/y up-left by bleed_l/bleed_t so visual center stays.
    bleed = raw.get("_bleed") or {"l": 0, "t": 0, "r": 0, "b": 0}
    bl, bt, br, bb = bleed.get("l", 0), bleed.get("t", 0), bleed.get("r", 0), bleed.get("b", 0)
    elements_out.append({
        "id": el_id,
        "parent_id": parent_id,
        "type": "image",
        "name": f"[{name_hint}/Image]",
        "x": raw["x"] - bl,
        "y": raw["y"] - bt,
        "width": raw["w"] + bl + br,
        "height": raw["h"] + bt + bb,
        # Rotation is already baked into the rasterized PNG: the screenshot captures
        # the element's AABB region with the rotated content in place. Re-applying it
        # as a node rotation would double-transform — and figma-mcp-go's rotate_nodes
        # is OPPOSITE-sign to CSS (positive = counter-clockwise), so it actually
        # CANCELS the baked rotation, rendering rotated rasters nearly upright
        # (scene_11 cameras/icons). PNG = truth → node rotation 0.
        "rotation": 0.0,
        # The element's own opacity is already baked into the rasterized PNG: the
        # isolation CSS resets only ANCESTOR opacity (data-isolate-ancestor), never
        # the isolate-root's. Re-applying it on the node would double-darken
        # (e.g. scene_11 chaos icons at opacity:0.5 rendered ~0.25). PNG = truth.
        "opacity": 1.0,
        "z": raw["z"],
        "image_path": asset_path,
    })


# ═════════════════════════════════════════════════════════════════════════════
# Main entry
# ═════════════════════════════════════════════════════════════════════════════

def extract(html_path: str, viewport_width: int | None = None, assets_dir: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    html_file = Path(html_path).resolve()
    frame_name = html_file.stem

    # Where to write PNG fallbacks for raster elements
    if assets_dir is None:
        assets_dir = f"output/assets/{frame_name}"
    assets_path = Path(assets_dir).resolve()
    assets_path.mkdir(parents=True, exist_ok=True)

    warnings = []
    elements_spec: list[dict] = []

    # viewport_width=None → auto-detect the design width from the layout's own
    # max-width (split/wide layouts like a 1100px two-column scene render
    # squished at the old 600px default). An explicit value always overrides.
    auto_width = viewport_width is None
    init_width = PROBE_WIDTH if auto_width else viewport_width
    # canvas_mode: a fixed-size design canvas (width+height) was detected → frame
    # matches the canvas exactly. Otherwise card-mode (fit content + PAD margin).
    canvas_mode = False
    canvas_dims = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Render at 2x DPI for crisp raster fallbacks
        context = browser.new_context(
            viewport={"width": init_width, "height": 900},
            device_scale_factor=2,
        )
        page = context.new_page()
        page.goto(f"file://{html_file}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        if auto_width:
            # Detect a fixed design canvas first; else the largest px max-width.
            design = page.evaluate(_DETECT_DESIGN_JS)
            decision = decide_design_viewport(design)
            chosen, chosen_h = decision["chosen_w"], decision["chosen_h"]
            canvas_mode, canvas_dims = decision["canvas_mode"], decision["canvas_dims"]
            if decision["warning"]:
                warnings.append(decision["warning"])
            if chosen != init_width or chosen_h != 900:
                page.set_viewport_size({"width": chosen, "height": chosen_h})
                page.wait_for_timeout(400)  # let layout reflow
            viewport_width = chosen

        # Measure the tagged canvas element's position NOW — after the probe
        # viewport is final, but before _FREEZE_ANIMATIONS_JS can distort it via
        # a scale/translateZ "camera" animation on the canvas element itself.
        # None when canvas_mode is False (no element was tagged).
        canvas_pos = page.evaluate(_MEASURE_CANVAS_JS)
        canvas_pos = (canvas_pos["x"], canvas_pos["y"]) if canvas_pos else None

        # Step 1 (JS): Jump every animation to its OWN peak-opacity moment (or
        # natural end state if it has no opacity keyframe). See _FREEZE_ANIMATIONS_JS.
        page.evaluate(_FREEZE_ANIMATIONS_JS)
        page.wait_for_timeout(200)  # let browser paint the frozen states

        # Step 2 (CSS): prevent NEW animations/transitions from triggering
        # during the per-element screenshot loop (element isolation + raster capture).
        page.add_style_tag(content="""
            *, *::before, *::after {
                animation-play-state: paused !important;
                transition-duration: 0s !important;
                transition-delay: 0s !important;
            }
        """)
        page.wait_for_timeout(100)

        result = page.evaluate(_EXTRACT_JS, load_noise_keywords())
        raw_elements = result["elements"]
        warnings.extend(result.get("warnings", []))
        noise_review = result.get("noiseReview", [])
        frame_w = result["frameWidth"]
        frame_h = result["frameHeight"]
        body_bg = _color_to_rgba(result.get("bodyBg", ""))
        root_bg = _color_to_rgba(result.get("rootBg", ""))

        # Stash each element's ORIGINAL (DOM-walk, pre-raster-union) w/h before
        # the raster loop below can overwrite raw["w"]/raw["h"] with a bigger,
        # union-with-descendants size. compute_frame_size's canvas-mode origin
        # detection needs to match against the AUTHORED canvas size, which only
        # the original measurement reflects — see the comment at that call site.
        for _r in raw_elements:
            _r["_orig_w"], _r["_orig_h"] = _r["w"], _r["h"]

        # Sample rendered bg pixel early (before page is modified by raster loop).
        # Used as frame_bg fallback when body/root background is transparent.
        _sampled_bg = None
        try:
            import io as _io
            from PIL import Image as _Img
            _px = page.screenshot(clip={"x": 5, "y": 5, "width": 10, "height": 10})
            _img = _Img.open(_io.BytesIO(_px)).convert("RGB")
            _r, _g, _b = _img.getpixel((5, 5))
            _sampled_bg = {"r": round(_r / 255, 4), "g": round(_g / 255, 4), "b": round(_b / 255, 4), "a": 1.0}
        except Exception:
            pass

        # Assign stable string ids
        uid_to_id = {r["uid"]: r["uid"] for r in raw_elements}

        # Sort by effective z (stable doc order built in)
        raw_elements.sort(key=lambda r: r["z"])

        # Frame sizing (canvas-exact vs content-bbox+margin) is computed AFTER
        # the raster capture loop below, not here — a raster element's bbox can
        # grow significantly once _isolated_screenshot unions it with escaping
        # descendants (see that loop's geometry sync), and frame sizing must
        # account for the corrected (larger) size, not this pre-union DOM-walk
        # measurement, or content legitimately ends up outside the frame's own
        # bounds (confirmed on real scenes: a 3D-transform/scale animation's
        # peak state can push a raster element hundreds of px past a frame
        # sized from stale geometry). See the frame-sizing call further below,
        # right before browser.close().

        # ─── Layer-isolation + bleed-aware screenshot ───────────────────────────
        # Two combined fixes:
        #   (A) Expand screenshot clip beyond bbox by the target's ink-extent
        #       (box-shadow blur+spread+offset, filter blur, drop-shadow). This
        #       prevents rectangular "halo cutoff" of glow effects around rounded
        #       shapes — the PNG now contains the full feathered fade-out.
        #   (B) Isolate via injected <style> tag with !important rules covering
        #       inline styles, Tailwind utilities, AND pseudo-elements. Tag
        #       ancestors with data-isolate-ancestor to strip paint, others with
        #       data-isolate-hide to suppress.
        _ISOLATE_JS = r"""
        (args) => {
            const [uid, backingCss, backingMaxArea] = args;
            const target = document.querySelector(`[data-extract-uid="${uid}"]`);
            if (!target) return null;
            const ancestors = new Set();
            for (let c = target.parentElement; c; c = c.parentElement) ancestors.add(c);
            const tagged = [];
            document.querySelectorAll('*').forEach(el => {
                if (el === target || target.contains(el)) return;
                if (el === document.documentElement || el === document.body) {
                    el.setAttribute('data-isolate-root', '');
                    tagged.push({el, attr: 'data-isolate-root'});
                    return;
                }
                if (ancestors.has(el)) {
                    el.setAttribute('data-isolate-ancestor', '');
                    tagged.push({el, attr: 'data-isolate-ancestor'});
                } else {
                    el.setAttribute('data-isolate-hide', '');
                    tagged.push({el, attr: 'data-isolate-hide'});
                }
            });
            // Inject the isolation stylesheet once (idempotent)
            if (!document.getElementById('__isolate_style')) {
                const st = document.createElement('style');
                st.id = '__isolate_style';
                st.textContent = `
                    [data-isolate-root], [data-isolate-root]::before, [data-isolate-root]::after {
                        background: none !important;
                        background-color: transparent !important;
                        background-image: none !important;
                    }
                    [data-isolate-ancestor], [data-isolate-ancestor]::before, [data-isolate-ancestor]::after {
                        background: none !important;
                        background-color: transparent !important;
                        background-image: none !important;
                        box-shadow: none !important;
                        filter: none !important;
                        backdrop-filter: none !important;
                        -webkit-backdrop-filter: none !important;
                        outline: none !important;
                        border-color: transparent !important;
                        opacity: 1 !important;
                    }
                    [data-isolate-ancestor]::before, [data-isolate-ancestor]::after {
                        content: none !important;
                    }
                    [data-isolate-hide] { visibility: hidden !important; }
                `;
                document.head.appendChild(st);
            }
            window.__isolateTagged = tagged;

            // ─── Backdrop backing for semi-transparent (glassmorphism) bg ───────
            // Glass elements look dark only because the dark page shows through
            // their translucent fill. Isolation removed that backdrop → washed
            // out PNG. Re-inject the frame's solid dark color as the BOTTOM
            // background layer of the target itself; background-clip defaults to
            // border-box so it follows the element's rounded shape (corners +
            // glow bleed stay transparent). Skip if no translucency, or if the
            // background is clipped to text (gradient-text), or no backing given.
            window.__isolateBackingEl = null;
            window.__isolateBackingStyle = null;
            // Backing only applies to SMALL bounded elements (chips/badges). Large
            // translucent overlays (grids, glows, network lines) must stay
            // translucent so they don't turn opaque and occlude content.
            const _br = target.getBoundingClientRect();
            const _smallEnough = !backingMaxArea || (_br.width * _br.height < backingMaxArea);
            if (backingCss && _smallEnough) {
                const bcs = window.getComputedStyle(target);
                const clip = (bcs.backgroundClip || bcs.webkitBackgroundClip || '');
                if (!clip.includes('text')) {
                    const bk = backingCss.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
                    const BR = bk ? +bk[1] : 0, BG = bk ? +bk[2] : 0, BB = bk ? +bk[3] : 0;
                    const savedStyle = target.getAttribute('style');
                    let applied = false;
                    // (1) Translucent background-COLOR (e.g. white 0.9 play button):
                    //     FLATTEN over backing → opaque. A solid bg-image can't sit
                    //     below background-color, so compositing math is required.
                    const cm = (bcs.backgroundColor || '').match(
                        /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\)/);
                    if (cm) {
                        const r = +cm[1], g = +cm[2], b = +cm[3];
                        const a = cm[4] !== undefined ? +cm[4] : 1;
                        if (a > 0 && a < 1) {
                            const fr = Math.round(r * a + BR * (1 - a));
                            const fg = Math.round(g * a + BG * (1 - a));
                            const fb = Math.round(b * a + BB * (1 - a));
                            target.style.setProperty('background-color', `rgb(${fr}, ${fg}, ${fb})`, 'important');
                            applied = true;
                        }
                    }
                    // (2) Translucent gradient background-IMAGE (glass chips/badges):
                    //     add opaque solid as the BOTTOM background-image layer.
                    const bi = bcs.backgroundImage || '';
                    if (bi.includes('gradient')
                        && (/rgba\([^)]*,\s*0?\.\d+\s*\)/.test(bi) || bi.includes('transparent'))) {
                        const solid = `linear-gradient(${backingCss}, ${backingCss})`;
                        target.style.setProperty('background-image', bi + ', ' + solid, 'important');
                        applied = true;
                    }
                    if (applied) {
                        window.__isolateBackingEl = target;
                        window.__isolateBackingStyle = savedStyle;
                    }
                }
            }

            // Compute ink-extent from box-shadow + filter on the target itself.
            // Returns padding (px) to add on each side beyond the element bbox.
            const cs = window.getComputedStyle(target);
            let bT = 0, bR = 0, bB = 0, bL = 0;
            // Glow/shadow blur is a Gaussian; box-shadow & filter drop-shadow define
            // their blur radius such that σ = blur/2. The visible falloff reaches ~0
            // only at 3σ = 1.5×blur — capturing just 1×blur (=2σ) leaves ~5% opacity
            // at the clip edge, producing a hard rectangular cutoff line. Expand to 3σ
            // so the glow fades smoothly. (filter blur() below uses σ = radius, so its
            // 3σ extent is 3×radius.)
            // Plus a flat safety margin so the faint outermost tail of the glow
            // (and 2x-DPI resampling) never touches the clip edge.
            const GLOW_FALLOFF = 1.5;
            const GLOW_PAD = 16;
            const shadow = cs.boxShadow;
            if (shadow && shadow !== 'none') {
                for (const p of shadow.split(/,(?![^()]*\))/)) {
                    if (p.includes('inset')) continue;
                    const nums = (p.match(/-?\d+(?:\.\d+)?px/g) || []).map(s => parseFloat(s));
                    if (nums.length < 3) continue;
                    const [ox, oy, blur, spread = 0] = nums;
                    const r = blur * GLOW_FALLOFF + spread + GLOW_PAD;
                    bT = Math.max(bT, r - oy); bB = Math.max(bB, r + oy);
                    bL = Math.max(bL, r - ox); bR = Math.max(bR, r + ox);
                }
            }
            const filter = cs.filter;
            if (filter && filter !== 'none') {
                const blurM = filter.match(/blur\(([\d.]+)px\)/);
                if (blurM) {
                    const r = parseFloat(blurM[1]) * 3 + GLOW_PAD;
                    bT = Math.max(bT, r); bB = Math.max(bB, r);
                    bL = Math.max(bL, r); bR = Math.max(bR, r);
                }
                // Nested-paren-aware: drop-shadow colors are functions like
                // rgba(…)/hsl(…) whose ) must NOT terminate the match. A plain
                // [^)]+ stops at rgba's ), dropping the shadow entirely (bleed=0).
                for (const m of filter.matchAll(/drop-shadow\(((?:[^()]|\([^()]*\))*)\)/g)) {
                    const nums = (m[1].match(/-?\d+(?:\.\d+)?px/g) || []).map(s => parseFloat(s));
                    if (nums.length >= 3) {
                        const [ox, oy, blur] = nums;
                        const r = blur * GLOW_FALLOFF + GLOW_PAD;
                        bT = Math.max(bT, r - oy); bB = Math.max(bB, r + oy);
                        bL = Math.max(bL, r - ox); bR = Math.max(bR, r + ox);
                    }
                }
            }
            return {
                bleedL: Math.ceil(Math.max(0, bL)),
                bleedT: Math.ceil(Math.max(0, bT)),
                bleedR: Math.ceil(Math.max(0, bR)),
                bleedB: Math.ceil(Math.max(0, bB)),
            };
        }
        """
        _RESTORE_JS = r"""
        () => {
            const tagged = window.__isolateTagged || [];
            for (const t of tagged) t.el.removeAttribute(t.attr);
            window.__isolateTagged = null;
            // Restore the target's original inline style (undo backdrop backing)
            const bel = window.__isolateBackingEl;
            if (bel) {
                if (window.__isolateBackingStyle === null) bel.removeAttribute('style');
                else bel.setAttribute('style', window.__isolateBackingStyle);
            }
            window.__isolateBackingEl = null;
            window.__isolateBackingStyle = null;
        }
        """
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

        # Opaque dark backing color for glassmorphism elements (see _ISOLATE_JS).
        # First opaque candidate among root bg → body bg → sampled pixel.
        def _rgba_to_css_opaque(c: dict | None) -> str | None:
            if not c:
                return None
            return f"rgb({round(c['r']*255)}, {round(c['g']*255)}, {round(c['b']*255)})"
        backing_css = None
        for _c in (root_bg, body_bg, _sampled_bg):
            if _c and _c.get("a", 0) >= 0.99:
                # backing_css = _rgba_to_css_opaque(_c)
                break
        # Backing applies only to elements smaller than 15% of the frame area —
        # large translucent overlays (grid/glow/network) stay translucent.
        backing_max_area = 0

        def _isolated_screenshot(uid_str: str, png_path: Path, expand_bleed: bool = True):
            """Isolate target + screenshot. If expand_bleed, clip is enlarged by
            CSS ink-extent so glow/shadow aren't cut to rectangular halo.
            Returns dict {l,t,r,b} of bleed actually applied, plus the raw
            target rect (viewport-relative, matches rectOf()'s convention)
            used to build the clip, or None on failure."""
            bleed_info = page.evaluate(_ISOLATE_JS, [uid_str, backing_css, backing_max_area])
            try:
                if bleed_info is None:
                    return None
                rect = page.evaluate(_GET_BBOX_JS, uid_str)
                if rect is None or rect["w"] < 1 or rect["h"] < 1:
                    return None
                if expand_bleed:
                    bl, bt = bleed_info["bleedL"], bleed_info["bleedT"]
                    br, bb = bleed_info["bleedR"], bleed_info["bleedB"]
                else:
                    bl = bt = br = bb = 0
                clip = {
                    "x": max(0.0, rect["x"] - bl),
                    "y": max(0.0, rect["y"] - bt),
                    "width": rect["w"] + bl + br,
                    "height": rect["h"] + bt + bb,
                }
                page.screenshot(path=str(png_path), clip=clip, omit_background=True)
                return {"l": bl, "t": bt, "r": br, "b": bb, "rect": rect}
            finally:
                page.evaluate(_RESTORE_JS)

        # Tall-content guard: page.screenshot(clip=…) is bound to the current
        # viewport, so any element extending below the (default 900px) viewport is
        # cut off in its raster PNG. Grow the viewport height to cover the full
        # document before the screenshot loop so tall elements (e.g. a full-bleed
        # image taller than the viewport) are captured in full. Capped to avoid
        # runaway buffers on pathologically long pages.
        _doc_h = int(page.evaluate("Math.ceil(document.documentElement.scrollHeight)"))
        _vp = page.viewport_size
        _target_h = min(_doc_h, 16000)
        if _target_h > _vp["height"]:
            page.set_viewport_size({"width": _vp["width"], "height": _target_h})
            page.wait_for_timeout(300)  # let layout settle after resize

        # frameRoot's viewport-relative origin (matches the DOM walk's `frameRect`,
        # findFrameRoot() always returns document.body) — needed below to convert
        # _GET_BBOX_JS's absolute rect into the same frame-relative space every
        # other element's x/y already went through in the walk.
        _frame_rect = page.evaluate(
            "() => { const r = document.body.getBoundingClientRect(); return {left: r.left, top: r.top}; }")

        # Raster fallbacks — extract walk already tagged each element with data-extract-uid
        raster_targets = [r for r in raw_elements if r.get("kind") == "raster"]
        for raw in raster_targets:
            uid_str = raw["uid"]
            try:
                if page.locator(f'[data-extract-uid="{uid_str}"]').count() == 0:
                    warnings.append(f"raster: uid {uid_str} not found in DOM")
                    continue
                png_path = assets_path / f"{uid_str}.png"
                result = _isolated_screenshot(uid_str, png_path, expand_bleed=True)
                if result is None:
                    warnings.append(f"raster: uid {uid_str} isolation failed")
                    continue
                # Sync geometry to the rect ACTUALLY captured — may be larger than
                # the DOM-walk's target-only rect when a descendant (e.g. a
                # position:absolute pin icon) escapes the target's own layout box.
                # NOTE: no origin_x/origin_y subtraction here — frame sizing (and
                # the origin shift applied to every element) now runs AFTER this
                # loop, once each raster element's true post-union size is known.
                # This leaves x/y in the same frame-root-relative (not yet
                # origin-shifted) space every native element is already in.
                raw["x"] = result["rect"]["x"] - _frame_rect["left"]
                raw["y"] = result["rect"]["y"] - _frame_rect["top"]
                raw["w"] = result["rect"]["w"]
                raw["h"] = result["rect"]["h"]
                raw["_asset_filename"] = png_path.name
                raw["_bleed"] = {"l": result["l"], "t": result["t"], "r": result["r"], "b": result["b"]}
            except Exception as e:
                warnings.append(f"raster screenshot {uid_str} failed: {e}")

        # Bg-only PNGs for gradient containers: strip children + isolate, screenshot, restore.
        # Skip "page-wrapper" containers (covering ~full frame) — those are the
        # ambient scene background which user doesn't need; frame_bg solid color
        # is sufficient for overlay-on-video use case.
        grad_containers = [r for r in raw_elements if r.get("isGradientContainer")]
        for raw in grad_containers:
            if raw["w"] >= frame_w * 0.95 and raw["h"] >= frame_h * 0.95:
                # Mark so the spec emitter knows this is a transparent layout-only frame
                raw["_skip_bg"] = True
                continue
            uid_str = raw["uid"]
            try:
                # Stash innerHTML and clear children (so the bg PNG has no foreground content)
                page.evaluate("""(uid) => {
                    const el = document.querySelector(`[data-extract-uid="${uid}"]`);
                    if (!el) return;
                    window.__savedHTML = window.__savedHTML || {};
                    window.__savedHTML[uid] = el.innerHTML;
                    el.innerHTML = '';
                }""", uid_str)
                png_path = assets_path / f"{uid_str}_bg.png"
                # No bleed expand: bg PNG fills the container frame which clips overflow anyway
                _isolated_screenshot(uid_str, png_path, expand_bleed=False)
                raw["_bg_asset_filename"] = png_path.name
            except Exception as e:
                warnings.append(f"bg-only screenshot {uid_str} failed: {e}")
            finally:
                # Restore innerHTML (even on failure) so subsequent extractions are not corrupted
                page.evaluate("""(uid) => {
                    const el = document.querySelector(`[data-extract-uid="${uid}"]`);
                    if (!el || !window.__savedHTML || !window.__savedHTML[uid]) return;
                    el.innerHTML = window.__savedHTML[uid];
                    delete window.__savedHTML[uid];
                }""", uid_str)

        # Frame sizing (canvas-exact vs content-bbox+margin) — shared with
        # utils/render_html.py's QC reference via compute_frame_size(). Runs
        # HERE (after the raster loop above) so it sees each raster element's
        # true, union-corrected size/position rather than the pre-capture
        # DOM-walk measurement — see the comment left at this function's
        # original call site (right after the z-sort) for why.
        geom = compute_frame_size(raw_elements, frame_w, frame_h, canvas_mode, canvas_dims, canvas_pos)
        adj_w, adj_h = geom["adj_w"], geom["adj_h"]
        origin_x, origin_y = geom["origin_x"], geom["origin_y"]
        if raw_elements:
            for r in raw_elements:
                r["x"] -= origin_x
                r["y"] -= origin_y

        browser.close()

    # Pass 2: emit spec elements (intermediate list; not yet reordered)
    for raw in raw_elements:
        if raw.get("kind") == "raster":
            asset_name = raw.get("_asset_filename")
            if not asset_name:
                warnings.append(f"raster {raw['uid']} dropped (no asset)")
                continue
            rel_path = str(Path(assets_dir) / asset_name)
            _emit_raster_element(raw, rel_path, uid_to_id, elements_spec)
        else:
            _emit_native_element(raw, uid_to_id, elements_spec, assets_dir, warnings)

    # Pass 3: promote parent_id to nearest emitted ancestor (skip layout-only divs)
    raw_parent = {r["uid"]: r.get("parent_uid") for r in raw_elements}
    # Set of raw uids that produced at least one spec element
    emitted_raw_uids = {e["id"].split("_")[0] for e in elements_spec}
    # Map any spec_id → raw_uid (strip suffixes like _bg, _t, _bt, etc.)
    def _raw_uid_of(spec_id: str) -> str:
        return spec_id.split("_")[0]
    def _nearest_emitted_ancestor(raw_uid: str | None) -> str | None:
        cur = raw_parent.get(raw_uid) if raw_uid else None
        while cur:
            if cur in emitted_raw_uids:
                return cur
            cur = raw_parent.get(cur)
        return None
    for e in elements_spec:
        p = e.get("parent_id")
        # parent_id is set to a raw_uid (from base["parent_id"]) or to el_id directly
        # (for the BG-Gradient and per-side border lines). Only promote if parent
        # is not itself in emitted set.
        if p and p not in emitted_raw_uids and _raw_uid_of(p) not in emitted_raw_uids:
            e["parent_id"] = _nearest_emitted_ancestor(p)

    # Pass 3.5: escape unwanted Figma frame clipping. figma-mcp-go frames ALWAYS
    # clip their children and expose no API to disable it. When a parent's CSS
    # overflow is VISIBLE (clip_content=False) but a child's bbox extends beyond
    # the parent box, the browser shows that overflow while a Figma frame would
    # clip it (e.g. scene_20 .node: flex content 122px tall centered in a 100px
    # box → icon overflows the top, label overflows the bottom). Reparent such a
    # child to the nearest ancestor that geometrically contains it OR that
    # legitimately clips (overflow:hidden — the browser clips there too, so it is
    # faithful to stop). Absolute coords are preserved, so the visual position is
    # unchanged; only the clipping parent changes. This can only REVEAL overflow
    # the browser already shows, never hide anything.
    spec_by_id = {e["id"]: e for e in elements_spec}
    def _box(e):
        return (e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"])
    def _contains(p, c, tol=2):
        px0, py0, px1, py1 = _box(p)
        cx0, cy0, cx1, cy1 = _box(c)
        return cx0 >= px0 - tol and cy0 >= py0 - tol and cx1 <= px1 + tol and cy1 <= py1 + tol
    for e in elements_spec:
        parent = spec_by_id.get(e.get("parent_id"))
        if not parent or parent.get("type") != "frame":
            continue
        if parent.get("clip_content") or _contains(parent, e):
            continue
        # The child painted on top of (after) its original parent's own fill in
        # the nested DOM/Figma tree. Once promoted to a sibling of that parent,
        # Pass 4 sorts siblings by z — preserve "renders above its old container"
        # by bumping z past the original parent's, or the frame's own fill would
        # cover the now-sibling child (scene_23 .step-card swallowing its
        # overflowing "UNDERSTAND" label).
        original_parent_z = parent.get("z", 0)
        e["z"] = max(e.get("z", 0), original_parent_z) + 0.5
        cur = parent
        while True:
            gp = spec_by_id.get(cur.get("parent_id"))
            if gp is None:
                e["parent_id"] = cur.get("parent_id")  # promote to root level
                break
            if gp.get("clip_content") or _contains(gp, e):
                e["parent_id"] = gp["id"]
                break
            cur = gp

    # Pass 4: topological / DFS-sibling-by-z ordering — parents before children,
    # siblings sorted by z ASC so lower-z renders below higher-z (matches CSS).
    by_parent: dict[str | None, list[dict]] = {}
    by_id: dict[str, dict] = {}
    for e in elements_spec:
        by_id[e["id"]] = e
        by_parent.setdefault(e.get("parent_id"), []).append(e)
    for kids in by_parent.values():
        kids.sort(key=lambda e: e.get("z", 0))
    ordered: list[dict] = []
    def _visit(parent_id):
        for kid in by_parent.get(parent_id, []):
            ordered.append(kid)
            _visit(kid["id"])
    _visit(None)
    # Append any unreachable nodes (defensive — shouldn't happen)
    seen = {id(e) for e in ordered}
    for e in elements_spec:
        if id(e) not in seen:
            ordered.append(e)
    elements_spec = ordered

    frame_bg = None
    if root_bg and root_bg.get("a", 0) > 0:
        frame_bg = root_bg
    elif body_bg and body_bg.get("a", 0) > 0:
        frame_bg = body_bg
    # Fallback: use pre-sampled page corner pixel (captured before raster loop).
    if frame_bg is None and _sampled_bg is not None:
        frame_bg = _sampled_bg

    spec = {
        "version": 2,
        "frame_name": frame_name,
        "frame_width": adj_w,
        "frame_height": adj_h,
        "frame_bg": frame_bg,
        "assets_dir": assets_dir,
        "warnings": warnings,
        "noise_review": noise_review,
        "elements": elements_spec,
    }
    return spec


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract Figma spec v2 from HTML")
    parser.add_argument("--input", required=True, help="Path to HTML file")
    parser.add_argument("--viewport-width", type=int, default=None,
                        help="Browser viewport width. Omit to auto-detect from the layout's max-width.")
    parser.add_argument("--output", required=True, help="Path to write spec.json")
    parser.add_argument("--assets-dir", help="Directory for PNG asset fallbacks (default: output/assets/{frame_name})")
    args = parser.parse_args()

    spec = extract(args.input, args.viewport_width, args.assets_dir)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    n_native = sum(1 for e in spec["elements"] if e["type"] != "image")
    n_raster = sum(1 for e in spec["elements"] if e["type"] == "image")
    n_review = len(spec.get("noise_review", []))
    print(f"Saved → {out_path}  ({n_native} native + {n_raster} raster, "
          f"{len(spec['warnings'])} warnings, {n_review} to-review)",
          file=sys.stderr)
