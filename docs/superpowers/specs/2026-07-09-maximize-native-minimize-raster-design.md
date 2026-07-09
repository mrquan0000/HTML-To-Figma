# Maximize Native, Minimize Raster — Design Spec

**Date:** 2026-07-09
**Topic #2 of 3** (sibling specs: skip-decorative-noise [DONE, merged 2026-07-09], multi-frame-per-phase — designed separately, built in order #3 → #2 → #1)
**Status:** Approved design, pending implementation plan

## Problem

`classify()` (`agents/html_extractor.py`) decides native vs raster per element. Today
it rasterizes several cases that figma-mcp-go can actually render natively, purely
because the current rules were written conservatively. The user imports Figma builds
into After Effects and hand-animates layers — a rasterized layer can't be
individually adjusted, recolored, or animated per-property, so **every raster that
could have been native is a loss of editability**, the top priority for this topic.

## Empirical grounding

A frequency survey across all 27 real scenes in `input/*.html` (2026-07-09) found:

| Trigger | Frequency |
|---|---|
| Gradient background | 27/27 (100%) |
| Blur filter | 26/27 (96%) |
| Inline `<svg>` | 25/27 (93%) |
| `rotate()` on a leaf | 9/27 (33%) |
| matrix3d/perspective | 5/27 (18%) |
| `mask` | 4/27 (15%) |
| brightness-only filter | 2/27 (already native) |
| `clip-path` | 1/27 (4%) |

Gradient and blur are by far the highest-volume raster triggers, so this topic
targets exactly those two.

**MCP tool capability check** (`mcp__figma-mcp-go__set_fills`, `set_effects`,
`create_effect_style`), read directly from their live schemas:

- `set_fills` accepts only a single solid hex `color` (+ opacity). No gradient
  parameter, no stops, no gradient-type tool exists anywhere in the toolset.
  **Gradient fill is a genuine, current hard limit** — not a false assumption in
  the existing CLAUDE.md docs. `figma-mcp-go` is an external npm package
  (`@vkhanhqui/figma-mcp-go`, invoked via `npx` in `.mcp.json`), not vendored in
  this repo, so adding a gradient-fill tool upstream is out of scope here.
- `set_effects` / `create_effect_style` accept `DROP_SHADOW`, `INNER_SHADOW`,
  `LAYER_BLUR`, and `BACKGROUND_BLUR` as native effect types. **Blur and glow are
  already fully supported**, just underused by the extractor.

**Existing code already proves the pattern works**, just scoped too narrowly:
- `backdrop-filter: blur()` already maps to native `BACKGROUND_BLUR`
  (`agents/html_extractor.py:1418-1421`).
- `filter: drop-shadow()`-only ("glow") already maps to native `DROP_SHADOW` via
  `_parse_filter_drop_shadows()` (line 1242) — but only wired up for **text**
  leaves (called only at line 1556; `classify()`'s `keepNative` check at line 368
  requires `hasDirectText`).
- `figma_builder.py`'s `_apply_effects()` (line 650) **already forwards
  `LAYER_BLUR`** to `set_effects` — the builder needs zero changes for this topic.
- `_is_full_frame_bg()` (line 977, `w ≥ frame_w*0.95 and h ≥ frame_h*0.95`) is an
  existing heuristic for "this element is a background, not content" — reused here
  to distinguish gradient backgrounds from gradient shapes.

## Scope — 4 concrete changes, all inside `agents/html_extractor.py`

| # | Current behavior | New behavior |
|---|---|---|
| A | `filter: blur(Npx)` → raster (falls into the "any other/combined filter" bucket) | Native: attach `LAYER_BLUR` effect. Applies to **any element**, leaf or container-with-children — a Figma frame's `LAYER_BLUR` blurs its whole rendered composite, matching CSS `filter:blur()` on a container. |
| B | `filter: drop-shadow()`-only on a **shape** (not text) → raster | Native: attach `DROP_SHADOW` effect. Extends the existing text-only glow rule to shape leaves — remove the `hasDirectText` restriction, call `_parse_filter_drop_shadows()` from the shape-effects builder too. |
| A+B | `filter: blur() drop-shadow()` combined → raster | Native: attach **both** effects (`set_effects` already accepts an array). |
| C | Gradient background covering ≥95% of frame (both dimensions) | **Unchanged — stays raster.** Atmospheric/video-like backgrounds keep their current treatment. |
| D/E | Gradient on a smaller shape, or gradient-text (`background-clip:text` + transparent fill + gradient) → raster | Native: fill/text color becomes a single approximated solid color (algorithm below). Shape uses the existing `fills: [{"type":"SOLID",...}]` schema; text uses the existing `fill_hex` field — **no spec schema change needed.** |

**Explicitly out of scope / unchanged** — real image content stays raster, because it
is genuine pixel content that cannot be redrawn as vector/shape/text:
- `<svg>` (icon vector) → still raster.
- `<img>` → still raster.
- `background-image: url(...)` (a real photo/image asset, not a gradient) → still
  raster.
- `clip-path`, `mask`, non-identity 3D transforms, >0.5° rotation on a leaf, CSS
  border-triangle pseudo, image-clipping viewport containers — **all unchanged**,
  not part of this topic.

## Algorithm: gradient → solid color approximation

Applies uniformly to any gradient type (linear/radial/conic) in `background-image`,
and to gradient-text — the algorithm only operates on the parsed color-stop list, not
gradient geometry.

1. **Parse the CSS gradient string directly** (no render/screenshot needed) — regex
   out `{color, position}` stops from `background-image` (e.g.
   `linear-gradient(90deg, #ff6a00 0%, #ffd23f 100%)` → 2 stops). If
   `background-image` has multiple comma-separated gradient layers, pool all stops
   from all layers into one list before the next step.
2. **Convert each stop to HSL**, find the two extremes by **Lightness**: the
   stop with the lowest `L` is "darker", the stop with the highest `L` is
   "lighter". With exactly 2 stops, use them directly. With 3+ stops, still only
   use the two lightness-extreme stops — middle stops are ignored, no separate
   multi-stop logic needed.
3. **Blend 70% darker-stop + 30% lighter-stop** in RGB space → one solid color.
   This deliberately biases toward the darker/richer tone as a "base" layer, so the
   user can add their own lighter highlight/light-sweep during AE editing rather
   than getting a washed-out midpoint average.
4. Assign the result as `fills: [{"type":"SOLID","color":{...}}]` (shape) or as the
   text run's `fill_hex` (text) — both are already-supported fill representations,
   no schema version bump.
5. **Always emit one `spec["warnings"]` entry per approximation**, e.g.:
   ```
   approximated gradient fill on [card/rectangle] as solid #E0651A (native, was
   raster; darker-biased 70/30 blend of gradient stops)
   ```
   Degenerate cases (single stop, or two stops with equal lightness) fall out of
   the same formula with no special-casing — blending a color with itself yields
   that color.

## Pipeline placement

| Change | Where |
|---|---|
| A. blur → native | `classify()`'s filter-check (~line 359-370): allow blur-only or blur+drop-shadow to stay native. Shape-effects builder (where `backdropFilter` → `BACKGROUND_BLUR` is added, line 1418-1421): add a parallel `filter` → `LAYER_BLUR` regex parse. |
| B. glow → native for shapes | `classify()`: drop the `hasDirectText` condition in the `filterIsOnlyDropShadow` branch. Shape-effects builder: call `_parse_filter_drop_shadows()` (already exists, line 1242) and merge into the shape's `effects` array, mirroring the text call site at line 1556. |
| C. gradient background | No behavior change — `classify()` needs the frame's rendered width/height in scope (already available at DOM-walk time, since the page is loaded at the target viewport) to apply the same `≥95%` both-dimensions check as `_is_full_frame_bg`. |
| D/E. gradient shape/text → solid | `classify()`: the two existing `return 'raster'` gradient branches (gradient-text, leaf-with-gradient-bg) fall through to `'native'` instead, except when caught by the C check above. New `blendGradientStops()` helper (JS) implements the Part 2 algorithm; called from the shape-fill builder and the text-run color builder. |

**`figma_builder.py`: no changes** — `_apply_effects()` already forwards
`LAYER_BLUR`; `SOLID` fill is already the default fill type.

**`utils/render_html.py`: no changes** — it still renders the real HTML for the QC
reference. Two different expectations apply when comparing Figma output to this
reference:
- **Blur/glow-native (A, B):** should look **near-identical** to before — Figma's
  `LAYER_BLUR`/`DROP_SHADOW` are the same Gaussian-blur/shadow semantics as the CSS
  they replace. A visible mismatch here during Bước 3 QC **is a real bug**, worth
  flagging.
- **Gradient→solid (D/E):** color **will visibly differ** by design — the
  `spec["warnings"]` entry is the signal that this is an intentional
  editability-for-fidelity trade, not a bug, following the same "asymmetry by
  design" principle already established for topic #3's decorative-noise skip.

## Verification

Synthetic fixture tests (pattern: `tests/test_decorative_noise.py`):

1. `filter: blur(Npx)` on a solid-color leaf → native rectangle + `LAYER_BLUR
   radius=N` effect, not raster.
2. `filter: blur(Npx)` on a container with child text/shapes → native frame with
   `LAYER_BLUR`; children remain separate native layers.
3. `filter: drop-shadow(...)`-only on a non-text shape → native + `DROP_SHADOW`
   effect, not raster.
4. `filter: blur() drop-shadow()` combined → native, effects array has both types.
5. A 2-stop gradient on a shape well under 95% of frame size → `SOLID` fill with
   the exact 70/30 darker-biased blend (test with a concrete orange-dark/yellow-
   bright example), plus a warning.
6. A gradient covering ≥95% of frame (both dimensions) → still raster (regression
   guard, unchanged).
7. Gradient-text (`background-clip:text` + transparent fill + gradient) → native
   text element, correct blended `fill_hex`, `runs` present, warning emitted.
8. A 3+ stop gradient → only the lightness-extreme (darkest + lightest) stops feed
   the blend; a middle stop's hue must not shift the result.
9. Real-scene regression (dev-time-only, `tests/test_real_scenes.py` pattern) on
   scenes with known gradient cards/blur — confirm no crash, confirm the expected
   native/raster element counts shift as designed.

## Out of scope / non-goals

- `<svg>`, `<img>`, `background-image: url(...)` (real image assets) — unchanged,
  still raster.
- `clip-path`, `mask`, non-identity 3D transforms, leaf rotation >0.5°, CSS
  border-triangle pseudo, image-clipping viewport containers — unchanged, not part
  of this topic (lower-frequency triggers, could be a future topic).
- No attempt to add native gradient-fill support to `figma-mcp-go` itself (external
  dependency, confirmed via live tool schema that no such tool exists today).
- Not a spec schema change — both `SOLID` fills and `LAYER_BLUR`/`DROP_SHADOW`
  effects are already-supported shapes in spec v2.
