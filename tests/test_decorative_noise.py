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
