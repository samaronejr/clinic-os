"""Write the passing upgrade verdict from the completed seed receipt."""

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def completed_seeds(markers: "Mapping[str, object]") -> list[str]:
    """Report only histories whose fixture loader returned markers."""
    seeds = ["core"]
    for name in ("inbox", "ai"):
        if name in markers:
            if not isinstance(markers[name], dict) or not markers[name]:
                message = f"missing completed upgrade history: {name}"
                raise ValueError(message)
            seeds.append(name)
    if "seeds" in markers and markers["seeds"] != seeds:
        message = "declared upgrade seeds disagree with completed histories"
        raise ValueError(message)
    return seeds


def main() -> None:
    """Emit the PASS receipt after the verifier and migration drift check."""
    seed_file, base_sha, upgrade_db, verified_file, recreated = sys.argv[1:]
    path = Path(seed_file)
    markers = json.loads(path.read_text(encoding="utf-8"))
    verdict = {
        "schema_version": 1,
        "result": "PASS",
        "base_sha": base_sha,
        "upgrade_db": upgrade_db,
        "seeds": completed_seeds(markers),
        "database_recreated": recreated == "1",
    }
    verified = json.loads(Path(verified_file).read_text(encoding="utf-8"))
    if verdict != verified:
        message = "upgrade verdict disagrees with the verified base histories"
        raise RuntimeError(message)
    pending = path.with_name("verdict.pending")
    pending.write_text(json.dumps(verdict) + "\n", encoding="utf-8")
    pending.replace(path.with_name("verdict.json"))


if __name__ == "__main__":
    main()
