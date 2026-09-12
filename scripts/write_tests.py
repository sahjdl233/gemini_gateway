import pathlib, json, sys
BASE = pathlib.Path(r"F:\project\gemini_gateway")
OUT = BASE / "tests" / "integration"
OUT.mkdir(parents=True, exist_ok=True)
data = json.loads((BASE / "scripts" / "test_data.json").read_text(encoding="utf-8"))
for item in data:
    p = OUT / item["name"]
    p.write_text(item["content"], encoding="utf-8")
    print("  " + item["name"] + ": " + str(p.stat().st_size) + " bytes")
