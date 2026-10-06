"""Create local learner state from public, empty templates."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--learner-name", required=True)
    args = parser.parse_args()

    destination = ROOT / "tutors"
    if destination.exists():
        raise SystemExit(f"Refusing to overwrite existing learner state: {destination}")

    destination.mkdir()
    for subject in ("interview", "english"):
        source = ROOT / "templates" / subject
        target = destination / subject
        shutil.copytree(source, target)
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace("{{ learner_name }}", args.learner_name),
                encoding="utf-8",
            )
    print(f"Created local state for {args.learner_name!r} in {destination}")


if __name__ == "__main__":
    main()
