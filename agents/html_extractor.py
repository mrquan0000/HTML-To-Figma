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
import json
import math
import re
import sys
from pathlib import Path


# ═════════════════════════════════════════════════════════════════════════════
# JS injected into the page — extracts raw style data for every element
# ═════════════════════════════════════════════════════════════════════════════

_EXTRACT_JS = r"""
() => {
    const SKIP_TAGS = new Set(['SCRIPT','STYLE','HEAD','META','LINK','NOSCRIPT','TITLE']);
    const TEXT_TAGS = new Set(['H1','H2','H3','H4','H5','H6','P','SPAN','LABEL','LI','A','STRONG','EM','B','I','SMALL','CODE']);

    // Element gets a unique id assigned during walk
    let uidCounter = 0;
    function uid() { return 'e' + (uidCounter++); }

    function styleSnapshot(el, pseudo) {
        const cs = window.getComputedStyle(el, pseudo);
        return {
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
            backgroundClip: cs.backgroundClip || cs.webkitBackgroundClip || '',
            content: pseudo ? cs.content : '',
        };
    }

    function rectOf(el) {
        const r = el.getBoundingClientRect();
        return {x: r.left, y: r.top, w: r.width, h: r.height,
                right: r.right, bottom: r.bottom};
    }

    // Frame root = body. All top-level layout (ambient layers, main container, etc.)
    // becomes children. Body background is captured separately as frame_bg.
    function findFrameRoot() { return document.body; }

    // Extract inline text runs from a text-bearing element.
    // Walks immediate descendants: text nodes inherit element style;
    // <span>/<strong>/<em>/<b>/<i> with text children become separate runs.
    function extractRuns(el) {
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
            runs.push({
                text: txt,
                fontFamily: cs.fontFamily, fontSize: cs.fontSize,
                fontWeight: cs.fontWeight, fontStyle: cs.fontStyle,
                color: cs.color, letterSpacing: cs.letterSpacing,
                textDecorationLine: cs.textDecorationLine,
                backgroundImage: cs.backgroundImage,
                backgroundClip: cs.backgroundClip || cs.webkitBackgroundClip || '',
                webkitTextFillColor: cs.webkitTextFillColor || '',
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

    function classify(el, cs, hasDirectText, hasElemChildren) {
        if (el.tagName === 'svg')                            return 'raster';
        if (cs.filter && cs.filter !== 'none')               return 'raster';
        if (cs.clipPath && cs.clipPath !== 'none')           return 'raster';
        if (cs.mask && cs.mask !== 'none')                   return 'raster';
        if (cs.backgroundImage.includes('url('))             return 'raster';
        // background-clip:text + transparent fill → gradient text, must raster
        if ((cs.webkitTextFillColor === 'rgba(0, 0, 0, 0)' || cs.webkitTextFillColor === 'transparent')
            && cs.backgroundImage.includes('gradient'))      return 'raster';
        // Pure leaf with gradient bg (no element children) → raster whole element
        if (cs.backgroundImage.includes('gradient') && !hasElemChildren) return 'raster';
        // 3d transforms → raster
        if (cs.transform && (cs.transform.includes('matrix3d') || cs.transform.includes('perspective'))) return 'raster';
        // Leaf (no text/children to flatten) whose only visual extra is a CSS
        // border-triangle pseudo (▶ play icon) → rasterize so the pseudo renders.
        if (!hasDirectText && !hasElemChildren && hasBorderTrianglePseudo(el)) return 'raster';
        return 'native';
    }

    const frameRoot = findFrameRoot();
    const frameRect = frameRoot.getBoundingClientRect();
    const bodyCS = window.getComputedStyle(document.body);

    const out = [];
    let docOrder = 0;

    function visit(el, parentUid, parentVisualUid) {
        if (SKIP_TAGS.has(el.tagName)) return;

        const r = rectOf(el);
        if (r.w < 1 || r.h < 1) return;
        const cs = styleSnapshot(el, null);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return;

        const tag = el.tagName.toLowerCase();
        const isTextTag = TEXT_TAGS.has(el.tagName);
        const hasDirectText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
        const hasElementChildren = Array.from(el.children).some(c => !SKIP_TAGS.has(c.tagName));

        // Coordinates relative to frameRoot
        const x = Math.round(r.x - frameRect.left);
        const y = Math.round(r.y - frameRect.top);
        const w = Math.round(r.w);
        const h = Math.round(r.h);

        const myUid = uid();
        const myZ = effectiveZ(el, docOrder++);
        el.setAttribute('data-extract-uid', myUid);  // for later screenshot lookup

        const klass = classify(el, cs, hasDirectText, hasElementChildren);
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
            });
            // Don't recurse into raster element (children captured in PNG)
            return;
        }

        // Native element: collect text + style
        let runs = null;
        if (hasDirectText) {
            runs = extractRuns(el);
        }

        // Pseudo-elements: emit synthetic raster siblings if they have content
        const pseudos = [];
        for (const ps of ['::before', '::after']) {
            const psCS = styleSnapshot(el, ps);
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

        for (const child of el.children) {
            visit(child, parentUid, myUid);
        }
    }

    // Snapshot top-level children BEFORE walk — visit() mutates body by appending
    // offscreen bg-clones, and body.children is a live HTMLCollection that would
    // otherwise pick them up mid-iteration and skew normalization.
    const topLevel = Array.from(frameRoot.children);
    for (const child of topLevel) {
        visit(child, null, null);
    }
    // Also include frameRoot's own background if it has one
    const rootCS = styleSnapshot(frameRoot, null);

    return {
        elements: out,
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
    // Selectors that explicitly set BOTH width and height in px (stylesheets).
    const fixedSelectors = [];
    for (const sheet of document.styleSheets) {
        let rules;
        try { rules = sheet.cssRules; } catch (e) { continue; }
        if (!rules) continue;
        for (const rule of rules) {
            if (rule.style && rule.style.width && rule.style.height
                && rule.style.width.endsWith('px') && rule.style.height.endsWith('px')) {
                if (rule.selectorText) fixedSelectors.push(rule.selectorText);
            }
        }
    }
    let maxW = 0;
    let canvas = null, canvasArea = 0;
    for (const el of document.querySelectorAll('*')) {
        const cs = window.getComputedStyle(el);
        if (cs.maxWidth && cs.maxWidth.endsWith('px')) {
            const v = parseFloat(cs.maxWidth);
            if (isFinite(v) && v > maxW) maxW = v;
        }
        // Explicit fixed px width+height (inline style or matched rule)?
        let fixed = (el.style.width.endsWith('px') && el.style.height.endsWith('px'));
        if (!fixed) {
            for (const sel of fixedSelectors) {
                try { if (el.matches(sel)) { fixed = true; break; } } catch (e) {}
            }
        }
        if (fixed) {
            const r = el.getBoundingClientRect();
            // ≥600px wide qualifies as a design canvas (skip small fixed boxes/icons).
            if (r.width >= 600 && r.width * r.height > canvasArea) {
                canvas = {width: Math.round(r.width), height: Math.round(r.height)};
                canvasArea = r.width * r.height;
            }
        }
    }
    return {canvas, maxWidth: maxW || null};
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

def _build_text_runs(raw_runs: list[dict]) -> list[dict]:
    """Convert JS-extracted runs into spec TextRun list."""
    out = []
    for r in raw_runs or []:
        if r.get("_br"):
            out.append({"text": "\n", "_br": True})
            continue
        text = r.get("text", "")
        if not text:
            continue
        # Detect background-clip:text (gradient text fill)
        bg = r.get("backgroundImage", "")
        text_fill_transparent = r.get("webkitTextFillColor", "") in ("rgba(0, 0, 0, 0)", "transparent")
        bg_clip = r.get("backgroundClip", "")
        fills = None
        if text_fill_transparent and "gradient" in bg and bg_clip == "text":
            grad = _parse_gradient(bg)
            if grad:
                fills = [grad]
        if fills is None:
            color = _color_to_rgba(r.get("color", "")) or {"r": 0, "g": 0, "b": 0, "a": 1.0}
            fills = [{"type": "SOLID", "color": color}]
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


def _emit_native_element(raw: dict, uid_to_id: dict[str, str], elements_out: list[dict], assets_dir: str) -> None:
    """Convert one raw native element → 0, 1, or many spec elements."""
    cs = raw["cssText"]
    x, y, w, h = raw["x"], raw["y"], raw["w"], raw["h"]
    runs = _build_text_runs(raw.get("runs")) if raw.get("runs") else None
    has_text = bool(runs)
    el_id = uid_to_id[raw["uid"]]
    parent_id = uid_to_id.get(raw.get("parent_uid"))

    # ─── Fills ──────────────────────────────────────────────────────────────
    # Gradient bg is rasterized into a bg-only PNG child (handled below);
    # at this level, only SOLID bg color reaches `fills`.
    fills = []
    bg_color = _color_to_rgba(cs.get("backgroundColor", ""))
    has_gradient_bg = bool(raw.get("_bg_asset_filename"))
    if not has_gradient_bg and bg_color:
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
    bf = cs.get("backdropFilter", "")
    bm = re.search(r"blur\(([\d.]+)px\)", bf)
    if bm:
        effects.append({"type": "BACKGROUND_BLUR", "radius": float(bm[1])})

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

    # ─── Emit shape (if any visual) ─────────────────────────────────────────
    if has_visual:
        # Rectangles and ellipses in Figma cannot have children. Anything with
        # element children OR a bg-only child PNG MUST become a frame.
        needs_children = raw.get("hasElementChildren") or has_gradient_bg
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
            "clip_content": clip_content or has_gradient_bg,
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
        text_elem = {
            **base,
            "id": el_id + ("_t" if has_visual else ""),
            "type": "text",
            "name": f"[{name_hint}/Text-{preview or 'empty'}]",
            "runs": runs,
            "text_align": text_align,
            "line_height": line_height,
        }
        elements_out.append(text_elem)


def _emit_raster_element(raw: dict, asset_path: str, uid_to_id: dict[str, str], elements_out: list[dict]) -> None:
    el_id = uid_to_id[raw["uid"]]
    parent_id = uid_to_id.get(raw.get("parent_uid"))
    name_hint = raw.get("id") or (raw.get("className", "").split()[0] if raw.get("className") else raw["tag"])
    cs = raw.get("cssText", {})
    rotation = _parse_transform_rotation(cs.get("transform", ""))
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
        "rotation": rotation,
        "opacity": round(raw.get("opacity", 1.0), 3),
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
    PROBE_WIDTH = 1600
    DEFAULT_WIDTH = 600
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
            canvas = design.get("canvas")
            maxw = design.get("maxWidth")
            chosen_h = 900
            if canvas:
                # Fixed canvas → render at its exact size; frame = canvas (no margin).
                chosen = int(min(canvas["width"], 1920))
                chosen_h = int(min(canvas["height"], 1920))
                canvas_mode = True
                canvas_dims = (chosen, chosen_h)
                warnings.append(f"auto canvas {chosen}×{chosen_h}px (fixed design canvas)")
            elif maxw and maxw > DEFAULT_WIDTH:
                chosen = int(min(maxw, 1920))
                warnings.append(f"auto viewport width = {chosen}px (detected design max-width {int(maxw)}px)")
            else:
                chosen = DEFAULT_WIDTH
            if chosen != init_width or chosen_h != 900:
                page.set_viewport_size({"width": chosen, "height": chosen_h})
                page.wait_for_timeout(400)  # let layout reflow
            viewport_width = chosen

        # Freeze CSS animations/transitions so element screenshots don't time out
        # on "element is not stable". We disable both the animation property and
        # the transform/box-shadow that animations mutate.
        page.add_style_tag(content="""
            *, *::before, *::after {
                animation-duration: 0s !important;
                animation-delay: 0s !important;
                animation-iteration-count: 1 !important;
                animation-play-state: paused !important;
                transition: none !important;
            }
        """)
        page.wait_for_timeout(200)

        result = page.evaluate(_EXTRACT_JS)
        raw_elements = result["elements"]
        frame_w = result["frameWidth"]
        frame_h = result["frameHeight"]
        body_bg = _color_to_rgba(result.get("bodyBg", ""))
        root_bg = _color_to_rgba(result.get("rootBg", ""))

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

        # Frame sizing — two modes:
        #  • canvas_mode: a fixed design canvas was detected → frame = canvas size
        #    EXACTLY (no margin). Origin = the canvas element's top-left.
        #  • card-mode: frame = real content bbox + PAD margin on all sides.
        #    Full-frame ambient overlays are excluded so they don't pin the frame
        #    to the viewport size.
        PAD = 100
        if raw_elements and canvas_mode and canvas_dims:
            cw, ch = canvas_dims
            # Locate the canvas element by size match; fall back to (0,0) origin.
            cands = [r for r in raw_elements if abs(r["w"] - cw) <= 3 and abs(r["h"] - ch) <= 3]
            if cands:
                origin_x, origin_y = cands[0]["x"], cands[0]["y"]
            else:
                origin_x, origin_y = min(r["x"] for r in raw_elements), min(r["y"] for r in raw_elements)
            for r in raw_elements:
                r["x"] -= origin_x
                r["y"] -= origin_y
            adj_w, adj_h = cw, ch
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
            for r in raw_elements:
                r["x"] -= origin_x
                r["y"] -= origin_y
            adj_w = (max_x - min_x) + 2 * PAD
            adj_h = (max_y - min_y) + 2 * PAD
        else:
            adj_w, adj_h = frame_w, frame_h

        # Sort by effective z (stable doc order built in)
        raw_elements.sort(key=lambda r: r["z"])

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
            const shadow = cs.boxShadow;
            if (shadow && shadow !== 'none') {
                for (const p of shadow.split(/,(?![^()]*\))/)) {
                    if (p.includes('inset')) continue;
                    const nums = (p.match(/-?\d+(?:\.\d+)?px/g) || []).map(s => parseFloat(s));
                    if (nums.length < 3) continue;
                    const [ox, oy, blur, spread = 0] = nums;
                    const r = blur + spread;
                    bT = Math.max(bT, r - oy); bB = Math.max(bB, r + oy);
                    bL = Math.max(bL, r - ox); bR = Math.max(bR, r + ox);
                }
            }
            const filter = cs.filter;
            if (filter && filter !== 'none') {
                const blurM = filter.match(/blur\(([\d.]+)px\)/);
                if (blurM) {
                    const r = parseFloat(blurM[1]) * 3;
                    bT = Math.max(bT, r); bB = Math.max(bB, r);
                    bL = Math.max(bL, r); bR = Math.max(bR, r);
                }
                for (const m of filter.matchAll(/drop-shadow\(([^)]+)\)/g)) {
                    const nums = (m[1].match(/-?\d+(?:\.\d+)?px/g) || []).map(s => parseFloat(s));
                    if (nums.length >= 3) {
                        const [ox, oy, blur] = nums;
                        bT = Math.max(bT, blur - oy); bB = Math.max(bB, blur + oy);
                        bL = Math.max(bL, blur - ox); bR = Math.max(bR, blur + ox);
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
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
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
                backing_css = _rgba_to_css_opaque(_c)
                break
        # Backing applies only to elements smaller than 15% of the frame area —
        # large translucent overlays (grid/glow/network) stay translucent.
        backing_max_area = 0.15 * adj_w * adj_h

        def _isolated_screenshot(uid_str: str, png_path: Path, expand_bleed: bool = True):
            """Isolate target + screenshot. If expand_bleed, clip is enlarged by
            CSS ink-extent so glow/shadow aren't cut to rectangular halo.
            Returns dict {l,t,r,b} of bleed actually applied, or None on failure."""
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
                return {"l": bl, "t": bt, "r": br, "b": bb}
            finally:
                page.evaluate(_RESTORE_JS)

        # Raster fallbacks — extract walk already tagged each element with data-extract-uid
        raster_targets = [r for r in raw_elements if r.get("kind") == "raster"]
        for raw in raster_targets:
            uid_str = raw["uid"]
            try:
                if page.locator(f'[data-extract-uid="{uid_str}"]').count() == 0:
                    warnings.append(f"raster: uid {uid_str} not found in DOM")
                    continue
                png_path = assets_path / f"{uid_str}.png"
                bleed = _isolated_screenshot(uid_str, png_path, expand_bleed=True)
                if bleed is None:
                    warnings.append(f"raster: uid {uid_str} isolation failed")
                    continue
                raw["_asset_filename"] = png_path.name
                raw["_bleed"] = bleed
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
            _emit_native_element(raw, uid_to_id, elements_spec, assets_dir)

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
    print(f"Saved → {out_path}  ({n_native} native + {n_raster} raster, {len(spec['warnings'])} warnings)",
          file=sys.stderr)
