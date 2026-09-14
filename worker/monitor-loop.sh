#!/bin/bash
# piso-monitor-loop — the project-manager's wake loop (monitor role only).
#
# Runs in piso-worker-monitor. On each wake it asks pi to read the activity
# feed, judge per MONITOR.md, and emit any pokes/reminders/notes via
# piso-informant. The loop gives the cadence; pi's session (scoped to the
# monitor's own agent volume) keeps the judge's state across wakes.
#
# Detailed machine log: every wake prints a CLEARLY DELIMITED block with
#   1) the exact feed the monitor acts on (raw GET, ground truth — not what
#      pi re-narrates), and
#   2) the decision record pi prints per MONITOR.md (verbatim) — required on
#      EVERY wake, including "stay silent" ones, and
#   3) the informant lines (actual POST results: 201 = stored, spooled = r
#      buffered because the gateway was unreachable).
# That way `docker logs piso-worker-monitor` shows input → decision →
# outcome as one auditable unit. `piso-monitor-loop --once` runs a single
# wake and exits (for manual debugging).
#
# Design notes
# - pi -p runs a single-shot task and exits; the shell loop is the scheduler.
# - GATEWAY_URL points at the worker API (:8083). The feed is scrubbed; the
#   monitor never touches the control plane.
# - The monitor's model calls go through the MITM with its SCOPED key
#   (placeholder piso_monitor_…, Workers: [monitor]) — only it can substitute.
# - MONITOR.md ships at /opt/piso/MONITOR.md and is ALSO loaded via
#   settings.prompts; passing it explicitly here ensures the frame even before
#   pi's session has it in memory.
set -u

gw="${GATEWAY_URL:-}"
worker="${PISO_WORKER_NAME:-}"
slug="${PISO_WORKER_SLUG:-}"
if [ -z "$gw" ] || [ -z "$worker" ] || [ "$slug" != "monitor" ]; then
  echo "piso-monitor-loop: monitor role only (GATEWAY_URL, PISO_WORKER_NAME, slug=monitor)" >&2
  exit 2
fi

WAKE_SECS="${PISO_MONITOR_WAKE_SECS:-600}"   # 10 min default cadence
FEED_LIMIT="${PISO_MONITOR_FEED_LIMIT:-500}" # max feed rows dumped per wake
PROMPT="/opt/piso/MONITOR.md"
ONCE=""
if [ "${1:-}" = "--once" ]; then
  ONCE=1
fi

if [ ! -f "$PROMPT" ]; then
  echo "piso-monitor-loop: missing $PROMPT" >&2
  exit 2
fi
piBin="$(command -v pi 2>/dev/null || echo /usr/local/bin/pi)"

# The monitor has NO project mount and a read-only rootfs; the default workdir
# /workspace is unwritable, so pi's project-local extension state (cwd/.pi)
# fails to create. Use a persistent workdir inside the agent VOLUME (not
# tmpfs) so extension state AND pi's project identity survive restarts. The
# monitor's session lives here too (/root/.pi/agent/sessions/<cwd-key>).
MONITOR_HOME="/root/.pi/agent/monitor-work"
mkdir -p "$MONITOR_HOME" 2>/dev/null && cd "$MONITOR_HOME" 2>/dev/null || cd /tmp 2>/dev/null || true

FEED_URL="${gw}/api/v1/worker/activities?worker=${worker}"
FEED_FILE="/tmp/piso-monitor-feed.json"

# Divider helpers — pure ASCII, grep-able, no unicode deps.
HDR='=============================================================='
SEP='--------------------------------------------------------------'

# dump_feed prints the raw feed the monitor acts on: fetch status line,
# totals, per-project counts, then every row (one line each, text capped so
# one row cannot flood the log — full text lives in the activity store).
dump_feed() {
  local code
  echo "-- input: activity feed (what this wake acts on) ----------------"
  code=$(curl -sS --max-time 20 -o "$FEED_FILE" -w '%{http_code}' \
    "${FEED_URL}&limit=${FEED_LIMIT}" 2>/dev/null || echo 000)
  if [ "$code" != "200" ] || [ ! -s "$FEED_FILE" ]; then
    echo "  fetch failed: HTTP ${code} (pi will retry its own read; if the"
    echo "  gateway was just down, informant posts may be spooled, not lost)"
    return 0
  fi
  python3 - "$FEED_FILE" "$FEED_LIMIT" <<'PY'
import json, sys, time
path, limit = sys.argv[1], int(sys.argv[2])
try:
    rows = json.load(open(path))
except Exception as e:
    print(f"  feed: unparseable json ({e})")
    sys.exit(0)
if not isinstance(rows, list):
    print(f"  feed: unexpected shape: {type(rows).__name__}")
    sys.exit(0)
rows = [a for a in rows[:limit] if isinstance(a, dict)]

def fmt_ts(v):
    # activity ts is unix millis (int); tolerate ISO strings too
    if isinstance(v, (int, float)):
        ms = v if v > 10_000_000_000 else v * 1000  # ms vs s
        v = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))
    return str(v)[:19]

by, order = {}, []
for a in rows:
    s = a.get("slug") or "?"
    if s not in by:
        by[s] = []
        order.append(s)
    by[s].append(a)
order.sort(key=lambda s: -len(by[s]))
print(f"  activities : {len(rows)} rows")
print(f"  by project : " + " · ".join(f"{s}={len(by[s])}" for s in order))
for s in order:
    print(f"  [{s}] ({len(by[s])})")
    for a in by[s]:
        kind = (a.get("kind") or "?").ljust(10)
        tgt = ""
        if a.get("targetSlug"):
            tgt = (">" + str(a["targetSlug"])).ljust(12)
        text = str(a.get("text") or "").replace("\n", " ")[:140]
        print(f"    {fmt_ts(a.get('ts'))}  {kind} {tgt} {text}")
PY
}

# wake runs one full pass: dump the feed, run the judge, show the decision
# record and any actual informant emissions.
wake() {
  local out emissions
  echo "$HDR"
  echo "== MONITOR WAKE  $(date -u '+%Y-%m-%d %H:%M:%S') UTC =="
  if [ -n "$ONCE" ]; then echo "== (single-pass mode: --once) =="; else echo "== next wake in ${WAKE_SECS}s =="; fi
  echo "$HDR"
  echo "monitor     : ${worker} (slug=${slug})"
  echo "feed        : ${FEED_URL}&limit=${FEED_LIMIT}"
  dump_feed
  echo "$SEP"
  echo "-- decision (pi, per MONITOR.md — printed every wake, even silent) -"
  out="$("$piBin" -p "Run your project-manager routine now: read the activity feed per $(basename "$PROMPT"), decide, emit any pokes/reminders/notes via piso-informant, and ALWAYS print your Wake Report to stdout (same shape every wake, including when you stay silent). Be conservative: usually the right call is NO new events." 2>&1)"
  printf '%s\n' "$out"
  echo "$SEP"
  echo "-- informant emissions (actual POST results) --------------------"
  emissions="$(printf '%s\n' "$out" | sed -n 's/^informant /informant /p')"
  if [ -n "$emissions" ]; then
    printf '%s\n' "$emissions"
  else
    echo "  (none)"
  fi
  echo "-- end of wake ----------------------------------------------------"
}

while true; do
  wake
  if [ -n "$ONCE" ]; then
    exit 0
  fi
  sleep "$WAKE_SECS"
done