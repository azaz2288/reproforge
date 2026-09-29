import csv
import json
import sys
from decimal import Decimal
from pathlib import Path


source, destination = (Path(value) for value in sys.argv[1:3])
with source.open("r", encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
total = sum((Decimal(row["amount"]) for row in rows), Decimal("0"))
destination.write_text(json.dumps({"orders": len(rows), "total": str(total)}, sort_keys=True) + "\n", encoding="utf-8")
