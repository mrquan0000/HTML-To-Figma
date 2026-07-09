"""Slower integration check against real particle-heavy scenes in input/.

These are DEV-TIME checks: input/ holds transient scene files (gitignored, wiped
by utils/clean_project.py), so on a fresh clone or CI without those files each
case SKIPS rather than fails. Durable structural coverage lives in the synthetic
fixtures of test_decorative_noise.py; these add "works on real production HTML"
confidence while the scene library is present during active development."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from agents.html_extractor import extract  # noqa: E402


def _spec_for(scene, tmp_path):
    html = _ROOT / "input" / f"{scene}.html"
    if not html.exists():
        pytest.skip(f"{scene}.html not present")
    return extract(str(html), assets_dir=str(tmp_path / "assets"))


@pytest.mark.parametrize("scene", ["scene_9", "scene_24"])
def test_swarm_dropped_in_real_scene(scene, tmp_path):
    spec = _spec_for(scene, tmp_path)
    # The swarm-skip warning must be present (proves the drop fired)...
    assert any("skipped decorative swarm" in w for w in spec["warnings"]), \
        f"{scene}: expected a swarm-skip warning, got {spec['warnings']}"
    # ...and no tiny particle/dust leaf survives. Kept Tier-2 atmosphere layers
    # (e.g. [dust-mist/Image], [vignette/Image]) are large images, not tiny
    # leaves, so they legitimately remain and don't trip this check.
    tiny = [e for e in spec["elements"] if e["width"] <= 12 and e["height"] <= 12]
    assert tiny == [], \
        f"{scene}: no tiny swarm leaf should remain, got {[e.get('name') for e in tiny]}"
    # No [particle...] layer leaked by name either.
    assert not any(e.get("name", "").lower().startswith("[particle")
                   for e in spec["elements"]), f"{scene}: particle layers should be gone"


def test_real_content_survives_in_scene_9(tmp_path):
    spec = _spec_for("scene_9", tmp_path)
    # Only the particle SWARM should be removed — scene_9's real content layers
    # (focal-wrapper, question-text, ambient overlays) must all still be emitted.
    # (Historically scene_9 had no NATIVE text: its heading used gradient-clip
    # styling that rasterized by design. Since the maximize-native-minimize-
    # raster change (topic #2), a simple linear/radial gradient-clip heading
    # goes native instead — see test_real_scene_gradient_text_becomes_native
    # below — so this check still asserts real *content* layers survive by
    # name, without assuming raster-only.)
    names = " ".join(e.get("name", "").lower() for e in spec["elements"])
    assert "focal-wrapper" in names or "question-text" in names, \
        f"scene_9 real content must survive, got names: {names}"
    assert len(spec["elements"]) >= 5, "scene_9 should retain its real content layers"


def test_real_scene_gains_native_blur_or_glow_effects(tmp_path):
    # scene_9 has several filter:blur()/drop-shadow() decorative elements that
    # used to rasterize — after the maximize-native change they should carry
    # native LAYER_BLUR/DROP_SHADOW effects instead.
    spec = _spec_for("scene_9", tmp_path)
    effect_types = {fx.get("type") for e in spec["elements"] for fx in e.get("effects", [])}
    assert effect_types & {"LAYER_BLUR", "DROP_SHADOW"}, \
        f"scene_9: expected at least one native LAYER_BLUR/DROP_SHADOW effect, got {effect_types}"


def test_real_scene_gradient_text_becomes_native(tmp_path):
    # scene_9's heading uses background-clip:text with a gradient — this used
    # to force it to raster; it should now survive as an editable native text
    # element with an approximated solid fill, plus a warning documenting the
    # trade-off.
    spec = _spec_for("scene_9", tmp_path)
    texts = [e for e in spec["elements"] if e["type"] == "text"]
    assert texts, "scene_9's gradient-clip heading should now be a native text element"
    assert any("approximated gradient text fill" in w for w in spec["warnings"])
