#!/usr/bin/env python3
"""Aggregate `noise_review` candidates across spec JSONs to surface NEW decorative
noise keywords worth adding to config/noise_keywords.txt.

The extractor keeps a `noise_review` list on each spec: groups that structurally
look like a decorative swarm but were NOT dropped (no keyword match, N<12, or only
within the wider size band). This tool gathers those across many specs and ranks
the class tokens it saw, so you can decide which to promote into the exclude list.

Nothing is auto-added — YOU review the ranking and edit config/noise_keywords.txt.

Usage:
    .venv/bin/python utils/review_noise.py                 # scans output/*_spec.json
    .venv/bin/python utils/review_noise.py --specs 'out/*.json'
"""
import argparse
import glob
import json
from collections import Counter, defaultdict
from pathlib import Path


def aggregate(files: list[str]) -> dict[str, dict]:
    """Tally review-candidate class tokens across the given spec files.

    Returns {token: {"groups": int, "scenes": sorted[str], "size_min": int,
    "size_max": int}} — `groups` counts how many near-miss groups the token
    appeared in; `scenes` lists the distinct scenes; sizes are the observed range."""
    groups = Counter()
    scenes: dict[str, set] = defaultdict(set)
    size_min: dict[str, float] = {}
    size_max: dict[str, float] = {}
    for f in files:
        try:
            spec = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        scene = spec.get("frame_name") or Path(f).stem
        for entry in spec.get("noise_review", []):
            smin = entry.get("size_min")
            smax = entry.get("size_max")
            for token in entry.get("classes", []):
                groups[token] += 1
                scenes[token].add(scene)
                if smin is not None:
                    size_min[token] = min(size_min.get(token, smin), smin)
                if smax is not None:
                    size_max[token] = max(size_max.get(token, smax), smax)
    return {
        token: {
            "groups": n,
            "scenes": sorted(scenes[token]),
            "size_min": size_min.get(token),
            "size_max": size_max.get(token),
        }
        for token, n in groups.items()
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--specs", default="output/*_spec.json",
                    help="glob of spec JSON files (default: output/*_spec.json)")
    args = ap.parse_args()

    files = sorted(glob.glob(args.specs))
    if not files:
        print(f"No spec files matched: {args.specs}")
        return
    tally = aggregate(files)
    if not tally:
        print(f"No noise_review candidates found across {len(files)} spec file(s).")
        return

    # Most-frequent tokens first — those recurring across scenes are the strongest
    # candidates for the exclude list.
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1]["groups"], kv[0]))
    print(f"Reviewed {len(files)} spec(s). Candidate decorative-noise tokens "
          f"(add confirmed ones to config/noise_keywords.txt):\n")
    print(f"  {'token':22s} {'groups':>6s}  {'scenes':>6s}  sizes    where")
    print(f"  {'-'*22} {'-'*6}  {'-'*6}  {'-'*7}  {'-'*20}")
    for token, info in ranked:
        smin, smax = info["size_min"], info["size_max"]
        sizes = f"{smin}-{smax}px" if smin is not None else "?"
        where = ", ".join(info["scenes"][:6]) + ("…" if len(info["scenes"]) > 6 else "")
        print(f"  {token:22s} {info['groups']:6d}  {len(info['scenes']):6d}  {sizes:7s}  {where}")


if __name__ == "__main__":
    main()
