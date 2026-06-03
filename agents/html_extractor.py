#!/usr/bin/env python3
"""
HTML → Figma spec extractor.

Renders an HTML file in a headless Chromium browser (Playwright),
extracts every visible element's exact bounding box + computed styles,
and outputs a JSON spec compatible with figma-mcp-go.

Usage:
    python agents/html_extractor.py --input path/to/file.html [--viewport-width 600]

Output JSON is printed to stdout.
"""

import argparse
import json
import re
import sys
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_color(css_color: str) -> str | None:
    """Convert any CSS color string to #RRGGBB hex. Returns None if transparent."""
    if not css_color or css_color in ("none", "transparent", "rgba(0, 0, 0, 0)"):
        return None

    # rgb(r, g, b) or rgba(r, g, b, a)
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", css_color)
    if m:
        r, g, b = int(m[1]), int(m[2]), int(m[3])
        a = float(m[4]) if m[4] is not None else 1.0
        if a == 0:
            return None
        return f"#{r:02X}{g:02X}{b:02X}", a
    return None, 1.0


def _parse_color_with_alpha(css_color: str) -> tuple[str | None, float]:
    result = _parse_color(css_color)
    if isinstance(result, tuple):
        return result
    return result, 1.0


def _parse_shadow(shadow_str: str) -> dict | None:
    """Parse CSS box-shadow into Figma DROP_SHADOW params.

    Browsers report computed box-shadow as: color offsetX offsetY blur [spread]
    Example: "rgba(0, 0, 0, 0.6) 0px 15px 30px 0px"
    """
    if not shadow_str or shadow_str == "none":
        return None

    # Extract color token first (rgba/rgb/hex)
    color_m = re.search(r"(rgba?\([^)]+\)|#[\w]+)", shadow_str)
    if not color_m:
        return None
    color_token = color_m[1]
    # Remove color from string to parse numeric values
    rest = shadow_str.replace(color_token, "").strip()

    # Check for inset keyword
    inset = "inset" in rest
    rest = rest.replace("inset", "").strip()

    # Extract px values
    values = re.findall(r"(-?[\d.]+)px", rest)
    if len(values) < 3:
        return None
    offset_x = float(values[0])
    offset_y = float(values[1])
    blur = float(values[2])
    spread = float(values[3]) if len(values) > 3 else 0.0

    color_hex, alpha = _parse_color_with_alpha(color_token)
    if not color_hex:
        return None
    return {
        "type": "INNER_SHADOW" if inset else "DROP_SHADOW",
        "color": color_hex,
        "opacity": round(alpha * 100),
        "offset_x": round(offset_x),
        "offset_y": round(offset_y),
        "blur": round(blur),
        "spread": round(spread),
    }


def _parse_gradient(bg_image: str) -> dict | None:
    """Parse CSS linear-gradient into Figma gradient fill.

    Browsers report computed style as: linear-gradient(135deg, rgb(32, 15, 0), rgb(84, 42, 6))
    """
    if not bg_image or bg_image == "none":
        return None
    m = re.match(r"linear-gradient\((.+)\)\s*$", bg_image, re.DOTALL)
    if not m:
        return None
    inner = m[1]

    # Extract angle
    angle = 180  # default (to bottom)
    angle_m = re.match(r"\s*(-?\d+)deg\s*,\s*", inner)
    if angle_m:
        angle = int(angle_m[1])
        inner = inner[angle_m.end():]
    elif re.match(r"\s*to\s+", inner):
        pass  # skip "to bottom" etc., keep default

    # Extract all color stops — match rgb(), rgba(), or #hex
    stops = []
    for color_token in re.finditer(r"rgba?\([^)]+\)|#[\w]+", inner):
        color_hex, _ = _parse_color_with_alpha(color_token[0])
        if color_hex:
            stops.append(color_hex)

    if len(stops) < 2:
        return None
    return {"type": "linear_gradient", "angle": angle, "stops": stops}


# ── JS injected into the page ─────────────────────────────────────────────────

_EXTRACT_JS = """
() => {
    const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'HEAD', 'META', 'LINK', 'NOSCRIPT']);
    const root = document.body;

    // Fix #1: use first visible child of body as coordinate origin and frame dimensions.
    // HTML cards are typically a single root div inside body with margin:auto centering.
    // Using body as origin would include margin offsets and viewport empty space.
    let frameEl = null;
    for (const child of root.children) {
        const r = child.getBoundingClientRect();
        if (r.width > 10 && r.height > 10) {
            const cs = window.getComputedStyle(child);
            if (cs.display !== 'none' && cs.visibility !== 'hidden') {
                frameEl = child;
                break;
            }
        }
    }
    const frameRect = frameEl ? frameEl.getBoundingClientRect() : root.getBoundingClientRect();

    function extractElement(el) {
        if (SKIP_TAGS.has(el.tagName)) return null;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return null;
        if (rect.bottom < 0 || rect.right < 0) return null;

        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return null;

        // Coordinates relative to frame element, not body
        const x = Math.round(rect.left - frameRect.left);
        const y = Math.round(rect.top - frameRect.top);
        const w = Math.round(rect.width);
        const h = Math.round(rect.height);

        const isText = ['H1','H2','H3','H4','H5','H6','P','SPAN','LABEL','LI','A','STRONG','EM','B','I'].includes(el.tagName);
        const tag = el.tagName.toLowerCase();

        let textContent = '';
        if (isText || tag === 'div') {
            const directText = Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .join(' ').trim();
            textContent = directText;
        }

        // Fix #2: extract per-side border widths instead of shorthand.
        // cs.borderWidth shorthand misreads "1px 0px 0px 0px" (border-top only) as 1px uniform.
        const borderTopWidth    = parseFloat(cs.borderTopWidth)    || 0;
        const borderRightWidth  = parseFloat(cs.borderRightWidth)  || 0;
        const borderBottomWidth = parseFloat(cs.borderBottomWidth) || 0;
        const borderLeftWidth   = parseFloat(cs.borderLeftWidth)   || 0;

        return {
            tag, x, y, w, h, textContent,
            bgColor:    cs.backgroundColor,
            bgImage:    cs.backgroundImage,
            borderTopWidth, borderRightWidth, borderBottomWidth, borderLeftWidth,
            borderTopColor:    cs.borderTopColor,
            borderRightColor:  cs.borderRightColor,
            borderBottomColor: cs.borderBottomColor,
            borderLeftColor:   cs.borderLeftColor,
            borderRadius: parseFloat(cs.borderRadius) || 0,
            boxShadow:    cs.boxShadow,
            opacity:      parseFloat(cs.opacity),
            color:        cs.color,
            fontSize:     parseFloat(cs.fontSize) || 0,
            fontWeight:   cs.fontWeight,
            textAlign:    cs.textAlign,
            backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter || '',
            id: el.id || '',
            className: el.className || '',
        };
    }

    function walk(el) {
        const results = [];
        if (SKIP_TAGS.has(el.tagName)) return results;
        const data = extractElement(el);
        if (!data) return results;

        // Mixed content: element has both direct text nodes AND child elements.
        // Use full innerText and skip children to keep emoji+label as one text layer.
        const hasDirectText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
        const hasElemChildren = el.children.length > 0;
        if (hasDirectText && hasElemChildren) {
            data.textContent = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            results.push(data);
            return results;
        }

        results.push(data);
        for (const child of el.children) {
            results.push(...walk(child));
        }
        return results;
    }

    const allElements = walk(root);
    return {
        elements: allElements,
        frameWidth:  Math.round(frameRect.width),
        frameHeight: Math.round(frameRect.height),
    };
}
"""


# ── main extraction ───────────────────────────────────────────────────────────

_TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "label", "li", "a", "strong", "em", "b", "i"}


def _make_text_entry(el: dict, text_content: str) -> dict:
    text_color_hex, _ = _parse_color_with_alpha(el.get("color", ""))
    font_size = round(el.get("fontSize", 14))
    try:
        fw = int(el.get("fontWeight", "400"))
    except (ValueError, TypeError):
        fw_raw = el.get("fontWeight", "400")
        fw = 700 if fw_raw in ("bold", "bolder") else 400
    font_weight = "black" if fw >= 800 else ("bold" if fw >= 500 else "regular")
    return {
        "type": "text",
        "name": f"[{el['tag']}/Text-{text_content[:20].replace(' ', '_')}]",
        "x": el["x"], "y": el["y"], "width": el["w"], "height": el["h"],
        "text_content": text_content,
        "font_size": font_size,
        "font_weight": font_weight,
        "text_color": text_color_hex,
        "text_align": el.get("textAlign", "left"),
        "opacity": round(el.get("opacity", 1.0) * 100),
    }


def _build_spec(raw_elements: list[dict], frame_width: int, frame_height: int, frame_name: str) -> dict:
    elements = []

    # Calculate actual frame bounds from all elements to prevent overflow
    min_x, min_y = float('inf'), float('inf')
    max_x, max_y = float('-inf'), float('-inf')

    for el in raw_elements:
        tag = el["tag"]
        x, y, w, h = el["x"], el["y"], el["w"], el["h"]
        text_content = (el.get("textContent") or "").strip()
        opacity = round(el.get("opacity", 1.0) * 100)

        bg_color_hex, bg_alpha = _parse_color_with_alpha(el.get("bgColor", ""))
        if bg_color_hex and bg_alpha < 1.0:
            opacity = round(bg_alpha * 100)

        gradient = _parse_gradient(el.get("bgImage", ""))
        shadow = _parse_shadow(el.get("boxShadow", ""))

        backdrop = el.get("backdropFilter", "")
        blur_radius = None
        blur_m = re.search(r"blur\(([\d.]+)px\)", backdrop)
        if blur_m:
            blur_radius = float(blur_m[1])

        # Per-side border widths (Fix #2)
        btop    = el.get("borderTopWidth", 0)
        bright  = el.get("borderRightWidth", 0)
        bbottom = el.get("borderBottomWidth", 0)
        bleft   = el.get("borderLeftWidth", 0)

        # Uniform border: all 4 sides equal and > 0 → stroke on rect
        uniform_border = (btop == bright == bbottom == bleft) and btop > 0
        border_color_hex, _ = _parse_color_with_alpha(el.get("borderTopColor", ""))

        has_bg = bool(bg_color_hex) or bool(gradient)

        # --- Main rect (background + uniform border) ---
        if has_bg or uniform_border:
            cls_parts = (el.get("className") or "").split()
            name_part = el.get("id") or (cls_parts[0] if cls_parts else "") or tag
            rect_entry = {
                "type": "rectangle",
                "name": f"[{name_part}/Rect]",
                "x": x, "y": y, "width": w, "height": h,
                "fill_color": bg_color_hex if not gradient else None,
                "gradient": gradient,
                "stroke_color": border_color_hex if uniform_border else None,
                "stroke_width": round(btop) if uniform_border else 0,
                "corner_radius": round(el.get("borderRadius", 0)),
                "opacity": opacity,
                "shadow": shadow,
                "backdrop_blur": round(blur_radius) if blur_radius else None,
            }
            elements.append(rect_entry)
            # Track bounds
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + w)
            max_y = max(max_y, y + h)

        # --- One-side border → thin separator rect (Fix #2) ---
        if not uniform_border:
            for side, bw, color_key in [
                ("top",    btop,    "borderTopColor"),
                ("bottom", bbottom, "borderBottomColor"),
            ]:
                if bw <= 0:
                    continue
                sep_color_hex, sep_alpha = _parse_color_with_alpha(el.get(color_key, ""))
                if not sep_color_hex:
                    continue
                sep_y = y if side == "top" else (y + h - max(1, round(bw)))
                sep_h = max(1, round(bw))
                border_entry = {
                    "type": "rectangle",
                    "name": f"[div/Line-{side.capitalize()}Border]",
                    "x": x, "y": sep_y, "width": w, "height": sep_h,
                    "fill_color": sep_color_hex,
                    "gradient": None,
                    "stroke_color": None,
                    "stroke_width": 0,
                    "corner_radius": 0,
                    "opacity": round(sep_alpha * 100),
                    "shadow": None,
                    "backdrop_blur": None,
                }
                elements.append(border_entry)
                # Track bounds
                min_x = min(min_x, x)
                min_y = min(min_y, sep_y)
                max_x = max(max_x, x + w)
                max_y = max(max_y, sep_y + sep_h)

        # --- Text layer ---
        if text_content and (tag in _TEXT_TAGS or tag == "div"):
            text_entry = _make_text_entry(el, text_content)
            elements.append(text_entry)
            # Track bounds
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + w)
            max_y = max(max_y, y + h)

    # Adjust frame to fit all elements
    if min_x != float('inf') and max_x != float('-inf'):
        # Calculate actual frame dimensions from element bounds
        adjusted_frame_width = max(max_x - min_x, 1)
        adjusted_frame_height = max(max_y - min_y, 1)

        # Normalize all element coordinates relative to min_x, min_y
        for elem in elements:
            elem["x"] = round(elem["x"] - min_x)
            elem["y"] = round(elem["y"] - min_y)
    else:
        # No elements, use original frame dimensions
        adjusted_frame_width = frame_width
        adjusted_frame_height = frame_height

    return {
        "frame_name": frame_name,
        "frame_width": adjusted_frame_width,
        "frame_height": adjusted_frame_height,
        "elements": elements,
    }


def extract(html_path: str, viewport_width: int = 600) -> dict:
    from playwright.sync_api import sync_playwright

    html_file = Path(html_path).resolve()
    frame_name = html_file.stem

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": viewport_width, "height": 900})
        page.goto(f"file://{html_file}")
        page.wait_for_load_state("networkidle")

        # Let layout settle
        page.wait_for_timeout(500)

        result = page.evaluate(_EXTRACT_JS)
        browser.close()

    raw_elements = result["elements"]
    frame_width  = result["frameWidth"]
    frame_height = result["frameHeight"]

    return _build_spec(raw_elements, frame_width, frame_height, frame_name)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract Figma spec from HTML file")
    parser.add_argument("--input", required=True, help="Path to HTML file")
    parser.add_argument("--viewport-width", type=int, default=600, help="Browser viewport width")
    parser.add_argument("--output", help="Save JSON to file instead of stdout")
    args = parser.parse_args()

    spec = extract(args.input, args.viewport_width)
    out = json.dumps(spec, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Saved → {args.output}  ({len(spec['elements'])} elements)", file=sys.stderr)
    else:
        print(out)
