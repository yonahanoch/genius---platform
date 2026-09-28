#!/bin/bash
# Run one browser suite against a fresh backend and a fresh copy of the site.
#
#   ./browser-tests/run.sh e2e.js        the full desktop walk-through
#   ./browser-tests/run.sh mobile.js     the 375px phone layout
#   ./browser-tests/run.sh firstrun.js   what a brand-new store owner sees
#
# Refuses to start if ports 8080/8000 are already taken, so a suite can never
# silently test a server left over from an earlier run.
set -u
TEST="${1:-e2e.js}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="$HERE/.work"

cleanup() {
  for f in "$WORK/api.pid" "$WORK/web.pid"; do
    [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
    rm -f "$f"
  done
}
trap cleanup EXIT

cleanup
sleep 1
for port in 8080 8000; do
  if curl -s -o /dev/null "localhost:$port/"; then
    echo "ABORT: port $port is already in use — stop that server first"; exit 2
  fi
done

rm -rf "$WORK"; mkdir -p "$WORK/data" "$WORK/site"

# a throwaway copy of the site, pointed at the local backend instead of the
# deployed one, so the suite never talks to production by accident
cp -r "$ROOT/docs/." "$WORK/site/"
python3 - "$WORK/site/index.html" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s, n = re.subn(r'window\.GENIUS_API_URL = "[^"]*"',
               'window.GENIUS_API_URL = "http://127.0.0.1:8080"', s)
if not n:
    raise SystemExit("could not find GENIUS_API_URL in index.html")
open(p, "w", encoding="utf-8").write(s)
PY

cd "$ROOT/backend"
GENIUS_DATA_DIR="$WORK/data" ADMIN_TOKEN=e2e-admin nohup python3 main.py > "$WORK/api.log" 2>&1 &
echo $! > "$WORK/api.pid"

cd "$WORK/site"
nohup python3 -m http.server 8000 --bind 127.0.0.1 > "$WORK/web.log" 2>&1 &
echo $! > "$WORK/web.pid"

for _ in $(seq 1 20); do curl -s -o /dev/null localhost:8080/ && break; sleep 0.5; done
kill -0 "$(cat "$WORK/api.pid")" 2>/dev/null || { echo "ABORT: backend did not start"; cat "$WORK/api.log"; exit 2; }
echo "backend pid $(cat "$WORK/api.pid")  ·  data dir $WORK/data"

cd "$HERE"
NODE_PATH="$(npm root -g)" timeout 300 node "$TEST"
rc=$?
exit $rc
