import json
from pathlib import Path

quality = json.loads(Path("quality.json").read_text(encoding="utf-8"))
time_check = json.loads(Path("time.json").read_text(encoding="utf-8"))
assert quality["passed"] and time_check["passed"]
Path("report.md").write_text(
    f"# Validated event sample\n\nRows: {quality['rows']}\n"
    f"Train end: {time_check['max_train_timestamp']}\n"
    f"Test start: {time_check['min_test_timestamp']}\n",
    encoding="utf-8",
)
