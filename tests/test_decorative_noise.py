from conftest import run_extract

# 20 tiny textless particle leaves inside a .particles container, plus one real
# heading (.glowing-text) — a real-world content class that must survive. It is
# never a candidate because it has direct text, so the swarm detector can't touch
# it. (A dedicated "keyword-named but text-bearing → kept" guard lives in the
# threshold tests.)
_PARTICLES = "\n".join(
    f'<div class="particle" style="top:{i*4}%;left:{(i*7)%100}%"></div>'
    for i in range(20)
)
SWARM_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:600px; background:#111; position:relative; }}
  h1 {{ color:#fff; font-size:40px; }}
  .particles {{ position:absolute; inset:0; }}
  .particle {{ position:absolute; width:4px; height:4px; border-radius:50%; background:gold; }}
</style></head><body>
  <h1 class="glowing-text">REAL HEADING</h1>
  <div class="particles">{_PARTICLES}</div>
</body></html>"""


def _tiny(elements):
    return [e for e in elements if e["width"] <= 12 and e["height"] <= 12]


def _has_text(spec, needle):
    return any(needle in "".join(r["text"] for r in e.get("runs", []))
               for e in spec["elements"] if e["type"] == "text")


def test_particle_swarm_dropped(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert _tiny(spec["elements"]) == [], "tiny particle leaves should be dropped"


def test_real_heading_survives_swarm(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert _has_text(spec, "REAL HEADING"), "glowing-text heading must be kept"


def test_swarm_drop_emits_warning(tmp_path):
    spec = run_extract(SWARM_HTML, tmp_path)
    assert any("skipped decorative swarm" in w for w in spec["warnings"])


# --- Real UI must be KEPT: small repeated leaves below the keyword threshold. ---
# 5-star rating and 6 carousel dots, each in its OWN container (.rating / .dots).
# Detection is per-parent, so each group is evaluated independently (5 and 6) —
# both below the 8 candidate floor, so nothing is dropped. (Even if they shared a
# parent: N=11 with no keyword still needs N≥12 to drop. star/dot are not vocab.)
_STARS = "\n".join(f'<i class="star" style="left:{i*12}px"></i>' for i in range(5))
_DOTS = "\n".join(f'<i class="dot" style="left:{i*12}px"></i>' for i in range(6))
REAL_UI_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .star, .dot {{ position:absolute; width:10px; height:10px; border-radius:50%;
                 background:#fff; top:20px; }}
  .dots {{ position:absolute; top:80px; }}
</style></head><body>
  <div class="rating">{_STARS}</div>
  <div class="dots">{_DOTS}</div>
</body></html>"""


def test_real_ui_kept_below_threshold(tmp_path):
    spec = run_extract(REAL_UI_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 11, "5 stars + 6 dots (N=11, no keyword) must all be kept"
    assert not any("skipped decorative swarm" in w for w in spec["warnings"])


# --- 9 keyword-less tiny leaves (8–11 range, no keyword) → KEPT. ---
_NINE = "\n".join(f'<span class="fx" style="left:{i*20}px"></span>' for i in range(9))
NINE_NOKW_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .fx {{ position:absolute; width:5px; height:5px; background:#0f0; top:20px; }}
</style></head><body>{_NINE}</body></html>"""


def test_nine_keywordless_leaves_kept(tmp_path):
    spec = run_extract(NINE_NOKW_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 9, "8–11 keyword-less tiny leaves are ambiguous → kept"


# --- 14 keyword-less tiny leaves (N≥12) → DROPPED by structure alone. ---
_FOURTEEN = "\n".join(f'<span class="fx" style="left:{i*20}px"></span>' for i in range(14))
FOURTEEN_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .fx {{ position:absolute; width:5px; height:5px; background:#0f0; top:20px; }}
</style></head><body>{_FOURTEEN}</body></html>"""


def test_fourteen_keywordless_leaves_dropped(tmp_path):
    spec = run_extract(FOURTEEN_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert tiny == [], "N≥12 tiny leaves are conclusive → dropped without keyword"


# --- 8 carousel dots with class "dot" → KEPT (dot excluded from vocab). ---
_EIGHT_DOTS = "\n".join(f'<i class="dot" style="left:{i*20}px"></i>' for i in range(8))
CAROUSEL_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:200px; background:#111; position:relative; }}
  .dot {{ position:absolute; width:8px; height:8px; border-radius:50%;
          background:#fff; top:20px; }}
</style></head><body>{_EIGHT_DOTS}</body></html>"""


def test_carousel_dots_kept(tmp_path):
    spec = run_extract(CAROUSEL_HTML, tmp_path)
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert len(tiny) >= 8, "'dot' is not a noise keyword; 8 dots (N<12) stay"


# --- Keyword-named BUT text-bearing element is KEPT (text → not a candidate). ---
# 12 tiny textless .dust leaves (would be dropped: N≥12) alongside a .dust-label
# element that CONTAINS text — the label matches the 'dust' keyword yet must
# survive because having text disqualifies it from being a swarm candidate.
_DUST = "\n".join(f'<div class="dust" style="top:{i*5}%;left:{i*7}%"></div>' for i in range(12))
KEYWORD_TEXT_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:400px; background:#111; position:relative; }}
  .dust {{ position:absolute; width:4px; height:4px; border-radius:50%; background:gold; }}
  .dust-label {{ position:absolute; top:50%; left:40%; color:#fff; font-size:24px; }}
</style></head><body>
  <div class="dust-label">DUST STORM</div>
  {_DUST}
</body></html>"""


def test_keyword_named_text_element_kept(tmp_path):
    spec = run_extract(KEYWORD_TEXT_HTML, tmp_path)
    kept = "".join(r["text"] for e in spec["elements"] if e["type"] == "text"
                   for r in e.get("runs", []))
    assert "DUST STORM" in kept, ".dust-label has text → must survive despite 'dust' name"


# ── noise_review: a diagnostic log of swarm-SHAPED groups we did NOT drop, so
#    unknown particle types / missing keywords can be discovered on real HTML. ──

# 9 tiny textless .orb leaves — not a known keyword, N<12 → KEPT, but flagged.
_ORBS = "\n".join(f'<div class="orb" style="top:{i*5}%;left:{i*7}%"></div>' for i in range(9))
ORB_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:400px; background:#111; position:relative; }}
  .orb {{ position:absolute; width:5px; height:5px; border-radius:50%; background:#fff; }}
</style></head><body>{_ORBS}</body></html>"""


def _review_classes(spec):
    return {c for e in spec.get("noise_review", []) for c in e.get("classes", [])}


def test_review_flags_keywordless_swarm(tmp_path):
    spec = run_extract(ORB_HTML, tmp_path)
    assert "orb" in _review_classes(spec), \
        f"review log should surface the 'orb' class; got {spec.get('noise_review')}"
    # It was flagged, NOT dropped.
    assert not any("skipped decorative swarm" in w for w in spec["warnings"])


# 10 textless 20px .blob leaves — too big for the ≤12px DROP net, but within the
# ≤24px REVIEW net → flagged so the size threshold can be revisited.
_BLOBS = "\n".join(f'<div class="blob" style="top:{i*6}%;left:{i*6}%"></div>' for i in range(10))
BLOB_HTML = f"""<!doctype html><html><head><style>
  body {{ margin:0; width:800px; height:600px; background:#111; position:relative; }}
  .blob {{ position:absolute; width:20px; height:20px; border-radius:50%; background:#0af; }}
</style></head><body>{_BLOBS}</body></html>"""


def test_review_flags_oversized_repeated_leaves(tmp_path):
    spec = run_extract(BLOB_HTML, tmp_path)
    assert "blob" in _review_classes(spec), \
        f"20px repeated leaves should be flagged; got {spec.get('noise_review')}"
    # 20px leaves are NOT dropped — they remain as real elements.
    assert len(spec["elements"]) >= 10


def test_dropped_swarm_not_in_review(tmp_path):
    # FOURTEEN_HTML's 14 keywordless leaves are DROPPED (N≥12) — a dropped swarm
    # must NOT also be listed for review.
    spec = run_extract(FOURTEEN_HTML, tmp_path)
    assert spec.get("noise_review", []) == [], \
        f"a dropped swarm must not appear in review; got {spec.get('noise_review')}"
    assert any("skipped decorative swarm" in w for w in spec["warnings"])


# ── The review → exclude loop: a token added to the config file makes a
#    previously-only-reviewed swarm actually drop on the next run. ──
def test_config_keyword_promotes_swarm_to_drop(tmp_path, monkeypatch):
    # Baseline: 9 .orb leaves are only REVIEWED (no keyword, N<12).
    baseline = run_extract(ORB_HTML, tmp_path)
    assert "orb" in _review_classes(baseline)
    assert not any("skipped decorative swarm" in w for w in baseline["warnings"])

    # Add 'orb' to a config file and point the loader at it → now it's a keyword,
    # so the 9-orb swarm (N≥8 && keyword) DROPS and leaves the review log.
    kw = tmp_path / "noise_keywords.txt"
    kw.write_text("# my extras\norb\n", encoding="utf-8")
    monkeypatch.setenv("NOISE_KEYWORDS_FILE", str(kw))

    after = run_extract(ORB_HTML, tmp_path)
    assert any("skipped decorative swarm" in w for w in after["warnings"]), \
        "with 'orb' configured, the swarm must now drop"
    assert "orb" not in _review_classes(after), "a dropped swarm leaves the review log"
