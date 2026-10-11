import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_obs(data_dir: Path) -> list[dict]:
    rows = []
    for f in sorted((Path(data_dir) / "obs").glob("*.ndjson")):
        for line in f.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows
