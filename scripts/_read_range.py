import pathlib, sys
path = sys.argv[1]
start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
end = int(sys.argv[3]) if len(sys.argv) > 3 else None
lines = pathlib.Path(path).read_text(encoding='utf-8').splitlines()
sl = lines[start:end] if end else lines[start:]
for i, ln in enumerate(sl, start=start + 1):
    print(f"{i:4d}| {ln}")
