import pathlib, json, sys
base = pathlib.Path(r'F:\project\gemini_gateway')
integ = base / 'tests' / 'integration'
integ.mkdir(parents=True, exist_ok=True)
for item in json.loads(sys.stdin.read()):
    p = integ / item['name']
    p.write_bytes(item['content'].encode('utf-8'))
    print('  ' + item['name'] + ': ' + str(p.stat().st_size) + ' bytes')
