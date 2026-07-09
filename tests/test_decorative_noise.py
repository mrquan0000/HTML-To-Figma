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
# 5-star rating + 6 carousel dots. Same 10px size → they group together (N=11),
# but there's NO keyword match (star/dot are NOT in the vocab) so the drop needs
# N≥12 — 11 is kept.
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
