"""Every tracked file must be creatable on Windows (the production platform)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def test_no_windows_reserved_or_invalid_names():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    bad = [f for f in files for part in f.split("/")
           if RESERVED.match(part) or re.search(r'[<>:"|?*\\]', part) or part.endswith((".", " "))]
    assert not bad, f"not creatable on Windows: {bad}"
    lower = [f.lower() for f in files]
    assert len(lower) == len(set(lower)), "paths that differ only by case collide on Windows"
