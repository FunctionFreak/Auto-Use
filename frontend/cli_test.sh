#!/usr/bin/env bash
# =====================================================================
# Launch the CLI / coder-card UI test harness (frontend/cli_test.html).
# Serves the frontend/ folder over HTTP and opens the harness in your
# browser. Dev-only — it drives the real cli scripts with DUMMY data,
# no Flask/pywebview backend needed.
#
#   ./cli_test.sh            # serve on :8777 and open
#   ./cli_test.sh 9000       # use a custom port
# =====================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8777}"
URL="http://localhost:${PORT}/cli_test.html"

cd "$DIR"

echo "Serving $DIR  →  $URL"
python3 -m http.server "$PORT" >/dev/null 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' EXIT INT TERM

sleep 1
echo "Opening $URL"
open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || echo "→ open $URL manually"

echo "Server PID $SRV — press Ctrl+C to stop."
wait "$SRV"
