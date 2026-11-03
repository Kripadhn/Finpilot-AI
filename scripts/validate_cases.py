"""Validate case folders against the schema, and each truth_key against the truth file."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "eval" / "schema" / "case.schema.json"
TRUTH_PATH = REPO_ROOT / "data" / "ground_truth.json"


def truth_keys(node) -> Iterator[str]:
    if isinstance(node, dict):
        if "truth_key" in node:
            yield node["truth_key"]
        for value in node.values():
            yield from truth_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from truth_keys(value)


def validate(folders: list[Path], truth_path: Path) -> list[str]:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text("utf-8")))
    truth = json.loads(truth_path.read_text("utf-8"))
    errors: list[str] = []
    ids: dict[str, Path] = {}

    for folder in folders:
        for path in sorted([*folder.rglob("*.yaml"), *folder.rglob("*.yml")]):
            case = yaml.safe_load(path.read_text("utf-8"))
            for error in validator.iter_errors(case):
                errors.append(f"{path}: {error.json_path}: {error.message}")
            if not isinstance(case, dict):
                continue
            case_id = case.get("id")
            if case_id in ids:
                errors.append(f"{path}: id {case_id} is also used by {ids[case_id]}")
            ids[case_id] = path
            for key in truth_keys(case):
                if key not in truth:
                    errors.append(f"{path}: truth_key {key} is not in {truth_path.name}")
    if not ids:
        errors.append(f"No case files found in: {', '.join(str(f) for f in folders)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--truth", type=Path, default=TRUTH_PATH)
    args = parser.parse_args()

    errors = validate(args.folders, args.truth)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"Cases are valid: {', '.join(str(f) for f in args.folders)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
