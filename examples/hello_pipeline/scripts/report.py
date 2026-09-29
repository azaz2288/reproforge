import json
import sys
from pathlib import Path


source, destination = (Path(value) for value in sys.argv[1:3])
summary = json.loads(source.read_text(encoding="utf-8"))
destination.write_text(f"{summary['orders']} orders; total {summary['total']}\n", encoding="utf-8")
