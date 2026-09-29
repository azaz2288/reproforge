"""Built-in, deterministic CSV quality gates with machine-readable evidence."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate(gate: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Evaluate a declared CSV; all schema/data errors become explicit violations."""
    violations: list[str] = []
    row_count = 0
    null_counts: dict[str, int] = {}
    duplicates = 0
    seen: set[tuple[str, ...]] = set()
    max_train: datetime | None = None
    min_test: datetime | None = None
    train_count = test_count = 0
    path = workspace.joinpath(*gate["input"].split("/"))
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            columns = reader.fieldnames
            if not columns or len(columns) != len(set(columns)) or any(not name for name in columns):
                raise ValueError("CSV header is empty or contains duplicate/blank names")
            required = (set(gate.get("required_columns", [])) | set(gate.get("unique_by", []))
                        if gate["type"] == "csv_quality" else
                        {gate["timestamp_column"], gate["split_column"]})
            missing = required - set(columns)
            if missing:
                raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
            null_counts = {name: 0 for name in columns}
            for row in reader:
                row_count += 1
                if None in row:
                    raise ValueError(f"Row {row_count} has extra fields")
                if any(value is None for value in row.values()):
                    raise ValueError(f"Row {row_count} has missing fields")
                for name, value in row.items():
                    if not value.strip():
                        null_counts[name] += 1
                if gate["type"] == "csv_quality":
                    keys = gate.get("unique_by", [])
                    if keys:
                        key = tuple(row[name] for name in keys)
                        if key in seen:
                            duplicates += 1
                        seen.add(key)
                else:
                    try:
                        stamp = _timestamp(row[gate["timestamp_column"]])
                    except ValueError as exc:
                        raise ValueError(f"Row {row_count} has invalid ISO timestamp") from exc
                    split = row[gate["split_column"]]
                    if split == gate["train_label"]:
                        train_count += 1
                        max_train = stamp if max_train is None else max(max_train, stamp)
                    elif split == gate["test_label"]:
                        test_count += 1
                        min_test = stamp if min_test is None else min(min_test, stamp)
                    else:
                        raise ValueError(f"Row {row_count} has unknown split label")
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        violations.append(str(exc))
    if not violations:
        if gate["type"] == "csv_quality":
            if row_count < gate.get("min_rows", 1):
                violations.append(f"Row count {row_count} is below minimum {gate.get('min_rows', 1)}")
            limit = gate.get("max_null_fraction", 1.0)
            for name, count in null_counts.items():
                if row_count and count / row_count > limit:
                    violations.append(f"Column {name} null fraction exceeds {limit}")
            if duplicates:
                violations.append(f"Found {duplicates} duplicate unique keys")
        elif not train_count or not test_count:
            violations.append("Both train and test splits must contain rows")
        elif max_train is not None and min_test is not None and max_train >= min_test:
            violations.append("Temporal leakage: latest train timestamp is not before earliest test timestamp")
    return {"version": 1, "gate": gate, "passed": not violations, "rows": row_count,
            "null_counts": null_counts, "duplicate_keys": duplicates,
            "train_rows": train_count, "test_rows": test_count,
            "max_train_timestamp": max_train.isoformat() if max_train else None,
            "min_test_timestamp": min_test.isoformat() if min_test else None,
            "violations": violations}
