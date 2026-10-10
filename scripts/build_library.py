"""Rebuild site/library (catalog.json + skill files) from skills/library. Run before a release."""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.library import build_catalog  # noqa: E402

src, dst = ROOT / "skills" / "library", ROOT / "site" / "library"
dst.mkdir(parents=True, exist_ok=True)
for old in dst.glob("*.md"):
    old.unlink()
for f in src.glob("*.md"):
    shutil.copyfile(f, dst / f.name)
cat = build_catalog(src)
(dst / "catalog.json").write_text(json.dumps({"skills": cat}, indent=1), encoding="utf-8")
print(len(cat), "skills ->", dst)
