# Generator script for TASK-005 test files
import pathlib
import json
import sys
import os

base = pathlib.Path(r"F:\project\gemini_gateway")
integ = base / "tests" / "integration"
integ.mkdir(parents=True, exist_ok=True)

# Read JSON mapping of filename -> content from a data file
data_path = base / "scripts" / "test_data.json"
data = json.loads(data_path.read_text(encoding="utf-8"))
for fname, fcontent in data.items():
    p = integ / fname
    p.write_text(fcontent, encoding="utf-8")
    print(f"  {fname}: {p.stat().st_size} bytes")
