from pathlib import Path


def load_brand_colors(path: str) -> list[str]:
    lines = Path(path).read_text().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("# ")]


def ensure_dirs(**dirs: str) -> None:
    for path in dirs.values():
        Path(path).mkdir(parents=True, exist_ok=True)
