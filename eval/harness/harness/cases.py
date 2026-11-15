"""Case loading (EV-1): any folder of YAML cases that validate against the provided schema."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "eval" / "schema" / "case.schema.json"
TRUTH_PATH = REPO_ROOT / "data" / "ground_truth.json"


class CaseError(Exception):
    """The folder cannot be run: unreadable, schema-invalid, duplicate ids or unknown truth keys."""


def _truth_keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        if "truth_key" in node:
            yield node["truth_key"]
        for value in node.values():
            yield from _truth_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _truth_keys(value)


def load_truth(path: Path = TRUTH_PATH) -> dict[str, Any]:
    if not path.exists():
        raise CaseError(f"{path} not found. Run `make truth` first.")
    return json.loads(path.read_text("utf-8"))


def load_cases(folder: Path, truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Load, validate and return the cases in `folder` (recursive), sorted by id."""
    if not folder.is_dir():
        raise CaseError(f"{folder} is not a directory")
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text("utf-8")))
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: dict[str, Path] = {}
    for path in sorted([*folder.rglob("*.yaml"), *folder.rglob("*.yml")]):
        try:
            case = yaml.safe_load(path.read_text("utf-8"))
        except yaml.YAMLError as error:
            errors.append(f"{path}: invalid YAML: {error}")
            continue
        problems = [f"{path}: {e.json_path}: {e.message}" for e in validator.iter_errors(case)]
        if problems:
            errors.extend(problems)
            continue
        if case["id"] in seen:
            errors.append(f"{path}: id {case['id']} is also used by {seen[case['id']]}")
            continue
        seen[case["id"]] = path
        errors.extend(
            f"{path}: truth_key {key} is not in ground_truth.json"
            for key in _truth_keys(case)
            if key not in truth
        )
        cases.append(case)
    if errors:
        raise CaseError("\n".join(errors))
    if not cases:
        raise CaseError(f"No case files found in {folder}")
    return sorted(cases, key=lambda c: c["id"])
