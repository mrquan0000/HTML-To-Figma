#!/usr/bin/env python3
"""
URL → standalone HTML extractor (preprocessor for html_extractor.py).

Fetches a URL via Playwright, extracts a target element (or the full body) plus
the page's stylesheets / inline <style> / font links, and writes a self-contained
HTML file that renders identically to the live page section.

The output is consumed by agents/html_extractor.py as a normal --input file.
Pipeline becomes:

    URL  ─→  url_to_html.py        ─→  standalone .html
                                       │
    .html ─→  html_extractor.py    ─→  spec.json
                                       │
    spec  ─→  figma_builder.py     ─→  Figma

DRY guarantee:
    Every bug-fix downstream of HTML (style/layout/Figma) only needs editing
    once in html_extractor.py / figma_builder.py — both --input and --url flows
    share that pipeline. Only the "how we obtained the HTML" step differs.

Usage:
    python agents/url_to_html.py --url https://example.com --output input/page.html
    python agents/url_to_html.py --url https://site.com/page \\
        --selector ".elementor-element-2ccd89c" --output input/section.html

Then run the existing pipeline:
    .venv/bin/python agents/html_extractor.py --input input/section.html \\
        --output output/section_spec.json
    .venv/bin/python agents/figma_builder.py --spec output/section_spec.json \\
        --report output/section_report.json
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


# Render-stability override: unhide JS-animation-driven elements so they
# appear in the static standalone render. Safe no-op for non-Elementor pages.
STANDALONE_OVERRIDES = """\
<style>
/* Make JS-animation-driven elements visible in static standalone render */
.elementor-invisible{visibility:visible!important;opacity:1!important;}
.elementor-element[data-settings*="animation"]{visibility:visible!important;opacity:1!important;}
[class*="elementor"]{visibility:visible!important;}
</style>"""


def fetch_section(
    url: str,
    output: Path,
    selector: str | None = None,
    viewport: tuple[int, int] = (1280, 900),
    wait_for: str = "networkidle",
    wait_extra_ms: int = 500,
) -> dict:
    """Render URL in Chromium, extract section + page styles, write standalone HTML."""
    width, height = viewport
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=2,
        )
        page = context.new_page()
        page.goto(url, wait_until=wait_for, timeout=30000)
        page.wait_for_timeout(wait_extra_ms)

        data = page.evaluate(
            """(selector) => {
                const collect = (sel) => Array.from(document.querySelectorAll(sel));
                const stylesheets = collect('link[rel="stylesheet"]')
                    .map(l => `<link rel="stylesheet" href="${l.href}">`).join('\\n  ');
                const inlineStyles = collect('style')
                    .map(s => s.outerHTML).join('\\n');
                const fonts = collect('link[rel="preconnect"], link[rel="preload"][as="font"]')
                    .map(l => l.outerHTML).join('\\n  ');

                let bodyHtml;
                let sectionRect = null;
                if (selector) {
                    const el = document.querySelector(selector);
                    if (!el) return {error: `Selector not found: ${selector}`};
                    bodyHtml = el.outerHTML;
                    const r = el.getBoundingClientRect();
                    sectionRect = {
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height),
                    };
                } else {
                    bodyHtml = document.body.outerHTML;
                }

                return {stylesheets, inlineStyles, fonts, bodyHtml,
                        baseURI: document.baseURI, sectionRect};
            }""",
            selector,
        )

        if "error" in data:
            browser.close()
            raise ValueError(data["error"])

        html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<base href="{data['baseURI']}">
<title>Extracted from {url}</title>
{data['fonts']}
{data['stylesheets']}
{data['inlineStyles']}
{STANDALONE_OVERRIDES}
</head>
<body style="background:#fff;padding:24px;">
{data['bodyHtml']}
</body>
</html>"""

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        browser.close()

        return {
            "output": str(output),
            "size_bytes": len(html),
            "section_rect": data.get("sectionRect"),
            "selector": selector,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch URL → standalone HTML for html_extractor.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Whole page body:
  python agents/url_to_html.py --url https://example.com --output input/example.html

  # One section by CSS selector:
  python agents/url_to_html.py --url https://datcotam.com/bat-dong-san/villa-for-sale/ \\
      --selector ".elementor-element-2ccd89c" --output input/section.html

  # Mobile viewport:
  python agents/url_to_html.py --url https://... --viewport 375x812 --output ...
""",
    )
    parser.add_argument("--url", required=True, help="URL to fetch")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    parser.add_argument("--selector", help="Optional CSS selector — extract only this element")
    parser.add_argument(
        "--viewport", default="1280x900",
        help="Browser viewport WxH (default 1280x900)",
    )
    parser.add_argument(
        "--wait-for", default="networkidle",
        choices=["load", "domcontentloaded", "networkidle"],
        help="Page load state to wait for (default networkidle)",
    )
    parser.add_argument(
        "--wait-extra-ms", type=int, default=500,
        help="Extra wait after load state to stabilize render (default 500ms)",
    )

    args = parser.parse_args()

    try:
        w, h = args.viewport.lower().split("x")
        viewport = (int(w), int(h))
    except ValueError:
        sys.exit(f"Invalid --viewport '{args.viewport}'. Use WxH like 1280x900.")

    try:
        meta = fetch_section(
            url=args.url,
            output=Path(args.output),
            selector=args.selector,
            viewport=viewport,
            wait_for=args.wait_for,
            wait_extra_ms=args.wait_extra_ms,
        )
    except Exception as e:
        sys.exit(f"✗ {e}")

    print(f"✓ Wrote {meta['output']} ({meta['size_bytes']:,} bytes)")
    if meta["section_rect"]:
        r = meta["section_rect"]
        print(f"  Section: {r['w']}x{r['h']} px at ({r['x']}, {r['y']})")
    print("  → Next:")
    print(f"     .venv/bin/python agents/html_extractor.py --input {meta['output']} --output output/spec.json")


if __name__ == "__main__":
    main()
