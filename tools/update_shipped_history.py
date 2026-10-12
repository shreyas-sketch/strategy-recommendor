"""Record the sha256 of every version of each shipped strategy file (git history + working tree).

Run after changing a file in config/strategies/ so deployments refresh their unedited copies:
    python tools/update_shipped_history.py
"""

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config" / "strategies"
HISTORY = SHIPPED / ".shipped_history.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    for path in sorted(SHIPPED.glob("*.y*ml")):
        if path.name.startswith("_"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        known = set(history.get(path.name, []))
        commits = subprocess.run(["git", "log", "--format=%H", "--", rel], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout.split()
        for c in commits:
            shown = subprocess.run(["git", "show", f"{c}:{rel}"], cwd=ROOT, capture_output=True)
            if shown.returncode == 0:
                known.add(sha(shown.stdout))
        known.add(sha(path.read_bytes()))
        history[path.name] = sorted(known)
    HISTORY.write_text(json.dumps(history, indent=1, sort_keys=True) + "\n")
    print(f"{HISTORY.relative_to(ROOT)}: {sum(map(len, history.values()))} versions of {len(history)} files")


if __name__ == "__main__":
    main()
