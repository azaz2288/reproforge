import json
from pathlib import Path


metrics = json.loads(Path("metrics.json").read_text(encoding="utf-8"))["metrics"]
Path("report.md").write_text(
    "# UCI Iris baseline\n\n"
    f"Train: {metrics['train_rows']} rows; test: {metrics['test_rows']} rows.\n\n"
    f"Accuracy: {metrics['correct']}/{metrics['test_rows']} ({metrics['accuracy']:.1%}).\n\n"
    f"Split: {metrics['split']}.\n\n{metrics['note']}\n",
    encoding="utf-8",
)
