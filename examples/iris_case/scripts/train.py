"""Deterministic nearest-centroid baseline, not a state-of-the-art model."""

import csv
import json
from collections import defaultdict
from pathlib import Path


features = ("sepal_length", "sepal_width", "petal_length", "petal_width")
gate = json.loads(Path("gate-report.json").read_text(encoding="utf-8"))
assert gate["passed"] and gate["rows"] == 150
groups = defaultdict(list)
with Path("iris.csv").open("r", encoding="utf-8", newline="") as stream:
    for row in csv.DictReader(stream):
        groups[row["species"]].append(tuple(float(row[name]) for name in features))
assert len(groups) == 3 and all(len(rows) == 50 for rows in groups.values())
centroids = {}
for label, rows in sorted(groups.items()):
    training = rows[:40]
    centroids[label] = [sum(point[index] for point in training) / len(training) for index in range(4)]
correct = 0
predictions = []
for label, rows in sorted(groups.items()):
    for point in rows[40:]:
        predicted = min(centroids, key=lambda candidate: sum(
            (point[index] - centroids[candidate][index]) ** 2 for index in range(4)))
        correct += predicted == label
        predictions.append({"actual": label, "predicted": predicted})
metrics = {"train_rows": 120, "test_rows": 30, "correct": correct,
           "accuracy": correct / 30, "split": "first 40 / last 10 rows per species",
           "note": "Demonstration split by source order; not a claim of representative model performance."}
Path("model.json").write_text(json.dumps({"centroids": centroids}, indent=2), encoding="utf-8")
Path("metrics.json").write_text(json.dumps({"metrics": metrics, "predictions": predictions}, indent=2), encoding="utf-8")
