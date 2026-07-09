"""Unit tests for the noise_review aggregator (utils/review_noise.py)."""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "utils"))
from review_noise import aggregate  # noqa: E402


def _write_spec(path, frame_name, review):
    path.write_text(json.dumps({"frame_name": frame_name, "noise_review": review}),
                    encoding="utf-8")


def test_aggregate_tallies_tokens_across_scenes(tmp_path):
    _write_spec(tmp_path / "a.json", "scene_a", [
        {"parent": ".fx", "count": 9, "classes": ["orb", "orbit"], "size_min": 6, "size_max": 10},
    ])
    _write_spec(tmp_path / "b.json", "scene_b", [
        {"parent": ".fx", "count": 11, "classes": ["orb"], "size_min": 4, "size_max": 8},
    ])
    tally = aggregate([str(tmp_path / "a.json"), str(tmp_path / "b.json")])

    assert tally["orb"]["groups"] == 2
    assert tally["orb"]["scenes"] == ["scene_a", "scene_b"]
    assert tally["orb"]["size_min"] == 4   # min across both
    assert tally["orb"]["size_max"] == 10  # max across both
    assert tally["orbit"]["groups"] == 1
    assert tally["orbit"]["scenes"] == ["scene_a"]


def test_aggregate_ignores_missing_and_broken_files(tmp_path):
    _write_spec(tmp_path / "ok.json", "scene_ok", [
        {"parent": ".fx", "count": 8, "classes": ["speckle"], "size_min": 3, "size_max": 5},
    ])
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    tally = aggregate([
        str(tmp_path / "ok.json"),
        str(tmp_path / "broken.json"),
        str(tmp_path / "does_not_exist.json"),
    ])
    assert set(tally) == {"speckle"}
    assert tally["speckle"]["groups"] == 1


def test_aggregate_empty_when_no_review(tmp_path):
    _write_spec(tmp_path / "clean.json", "scene_clean", [])
    assert aggregate([str(tmp_path / "clean.json")]) == {}
