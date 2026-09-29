"""Explicit, pinned download of the UCI Iris dataset for the real-data case."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from urllib.request import Request, urlopen


URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/iris/iris.data"
SHA256 = "6f608b71a7317216319b4d27b4d9bc84e6abd734eda7872b71a458569e2656c0"
HEADER = ["sepal_length", "sepal_width", "petal_length", "petal_width", "species"]


def main() -> None:
    request = Request(URL, headers={"User-Agent": "ReproForge-Iris-Example/1.0"})
    with urlopen(request, timeout=20) as response:
        raw = response.read(100_000)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"Source bytes changed: expected {SHA256}, received {digest}")
    rows = [row for row in csv.reader(io.StringIO(raw.decode("utf-8"))) if row]
    if len(rows) != 150 or any(len(row) != 5 for row in rows):
        raise SystemExit("Unexpected Iris dataset shape")
    target = Path(__file__).parent / "data" / "iris.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows to {target} (raw SHA-256 {digest})")


if __name__ == "__main__":
    main()
