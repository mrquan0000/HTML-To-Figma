# Skip Decorative Noise Elements — Design Spec

**Date:** 2026-07-09
**Topic #3 of 3** (sibling specs: maximize-native/minimize-raster, multi-frame-per-phase — designed separately, built in order #3 → #2 → #1)
**Status:** Approved design, pending implementation plan

## Problem

Many scene HTML files include purely-decorative "artistic noise" — swarms of tiny
particle/mote/dust dots scattered for atmosphere. Today these all get emitted into
the Figma output (as dozens of tiny native ellipse layers), cluttering the file. The
user imports Figma into After Effects and only hand-animates the MAIN CONTENT; the
noise swarms are never individually animated and just create "cả đống layer dot"
(piles of dot layers) that make the build hard to work with.

**Goal:** detect and SKIP these decorative particle swarms during extraction — don't
emit them into the spec at all — so the Figma output contains only content worth
building/animating.

## Scope (explicit decisions)

**IN scope — Tier 1 only: tiny scattered particle swarms.**
Confirmed patterns in the real library (grep evidence 2026-07-08): 194 `particle`,
42 `dust`, 34 `glow`, plus mote/bokeh/grain hits. Concrete structures verified:
- scene_9: `.particles` container wrapping 26 `.particle` leaves, each 3–6px, no text.
- scene_24: `.dust-field` container wrapping 20 `.dust` leaves, similar.

**OUT of scope — Tier 2: full-bleed atmospheric overlays** (vignette, ambient-glow,
dust-field-as-visual, grain). Decision: **KEEP these.** Each is only 1 raster layer
(not a layer pile), and they define the composition's mood (vignette darkens edges,
glow adds atmosphere). Removing them changes the visual more than removing 3px dots.
The user chose the conservative option: only kill the layer-pile swarms.

Note: a Tier-1 swarm's *container* (e.g. `.dust-field`) may share a name with a
Tier-2 overlay. The container is handled by the "empty container" rule below — it is
kept only if it has its OWN visual (background/border/blur); a bare transparent
`inset:0` positioning wrapper contributes nothing once its swarm children are dropped.

## Detection strategy: hybrid (structural primary, keyword booster)

Rejected alternatives:
- **Pure keyword blocklist** — fragile: needs an explicit exception for `glowing-text`
  (a real gradient-text content class, 9 hits), and won't generalize to differently
  named templates.
- **Pure structural** — risks false-positiving real repeated UI (e.g. an 8-icon grid).

Chosen: **structure decides, keyword only raises confidence, keyword never fires alone.**
This keeps `glowing-text` safe automatically — it has text content, so it fails the
"no text" candidate filter at step 1 regardless of its name.

## Algorithm

Runs inside `_EXTRACT_JS` (the Chromium DOM-walk in `agents/html_extractor.py`),
evaluated when processing an element E's direct children — BEFORE any raster capture.
Skipped elements never enter the spec and are never screenshot-captured (saves work).

For each element E, examine its direct-child group:

1. **Candidate filter — "is a noise particle":** a child C qualifies if it is a
   *leaf* (no element children), contains *no text*, and is *tiny* —
   `max(width, height) ≤ 12px`. (Real particles are 3–6px; real UI content is
   almost always ≥16px, so 12px has large safety headroom.)

2. **Group by near-equal size:** among E's candidate children, group those with
   near-identical size (dimension delta ≤ 2px). Count the largest such group N.

3. **Decision (hybrid):**
   - `N ≥ 8` **AND** (E's class OR the group's shared class matches the noise
     vocabulary) → **SKIP the whole group.**
   - `N ≥ 12` (structure alone is conclusive; no keyword needed) → **SKIP the group.**
   - Otherwise (8–11 candidates, no keyword match) → **KEEP** (could be real UI;
     err on the side of keeping).

4. **Empty-container cleanup:** after dropping a swarm, if E is now childless AND E
   has no visual of its own (no background, border, box-shadow, or filter) → E is
   also dropped. If E has its own visual (a Tier-2 overlay) → KEEP E, drop only the
   swarm inside it.

**Noise vocabulary (booster only — never fires without structural match):**
`particle, mote, dust, spark, bokeh, snow, ember, fleck, speck, twinkle`.
Matched case-insensitively as a class-name substring on E or the group's children.

Deliberately EXCLUDED and why:
- `star`, `dot` — too collision-prone with real UI (star-ratings, carousel/stepper
  dot indicators). Including them would drop an 8-dot carousel (`N≥8 + keyword`). A
  genuinely decorative starfield/dotfield of ≥12 tiny leaves is still caught by the
  structural-only path (`N≥12`), so nothing decorative is lost by omitting them.
- `grain`, `smoke` — name Tier-2 single-element overlays we KEEP; they'd never match
  the swarm structure anyway.

Because keyword never fires alone and every candidate must be textless, `glowing-text`
(has text) and any keyword-named-but-real element cannot be dropped by keyword alone.

## Pipeline placement

- **`agents/html_extractor.py` `_EXTRACT_JS`** — the only place that changes. The
  swarm check happens during the child-walk so dropped elements are never emitted and
  never rastered.
- **`agents/figma_builder.py`** — no change. It consumes the already-filtered spec.
- **`utils/render_html.py`** — **no change, intentionally.** The QC reference renders
  the true HTML (noise included). So the QC image WILL show particles while the Figma
  build will NOT. **This asymmetry is by design** — QC compares whether the real
  CONTENT matches, not whether decorative dots were reproduced. This must be
  remembered during Bước 3 validation so absent particles aren't false-flagged as a
  bug. (See [[faithful-to-html-render]] — same spirit, inverted: here Figma
  deliberately omits something the render shows.)

## Reporting

Every skipped swarm emits one warning into the spec's `warnings[]` (surfaced in the
build report), e.g.:

```
skipped decorative swarm: 26 leaves under .particles (match: structural+keyword)
skipped decorative swarm: 14 leaves under <div> (match: structural-only, N=14)
```

This is insurance against a false-positive: if a real UI group ever gets dropped, the
user sees exactly what and why, rather than an element silently vanishing.

## Verification

1. **Swarm removal:** re-run `html_extractor.py` on particle-heavy scenes
   (9, 10, 12, 24, 7, 8, 18) → the emitted spec contains **zero** particle/dust/mote
   elements; the corresponding `[particle/ellipse]` / `[m/ellipse]` layers are gone.
2. **Content preserved:** every `text` element and real shape remains — diff the
   pre/post spec for each scene and confirm only particle-class leaves disappeared.
   Special attention: `glowing-text` nodes survive.
3. **Synthetic false-positive guard test:** a fixture with a 5-element star-rating and
   a 6-element stepper-dot row (both small, textless, repeated) must be **KEPT**
   (N < 8, no keyword) — proves the threshold protects real UI.
4. **Synthetic true-positive test:** a fixture with a `.particles` container of 20
   tiny textless leaves must be **DROPPED**, with a warning emitted.
5. **No regression on single-content scenes** (scene_11 etc.): frame geometry and all
   non-particle elements unchanged vs current baseline.

## Out of scope / non-goals

- Not touching Tier-2 atmospheric overlays (kept as-is).
- Not a general raster-reduction change (that is sibling spec #2).
- No user-facing toggle/flag — the skip is always on (with warnings for visibility).
- Not changing `utils/render_html.py` — QC reference stays faithful to raw HTML.
