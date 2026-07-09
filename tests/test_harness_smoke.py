from conftest import run_extract


def test_extract_returns_text_element(tmp_path):
    html = """<!doctype html><html><head><style>
      body { margin:0; width:800px; height:200px; background:#111; }
      h1 { color:#fff; font-size:40px; }
    </style></head><body><h1>HELLO WORLD</h1></body></html>"""
    spec = run_extract(html, tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert any("HELLO WORLD" in "".join(r["text"] for r in e.get("runs", []))
               for e in texts)
