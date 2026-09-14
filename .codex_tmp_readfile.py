import io, sys
path = sys.argv[1]
start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
count = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
data = open(path, encoding='utf-8').read()
lines = data.splitlines(True)
print('TOTAL_LINES', len(lines))
print(''.join(lines[start:start+count]), end='')
