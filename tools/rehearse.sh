#!/usr/bin/env bash
#
# Run the whole pipeline against the mock lab, on one server, start to finish.
#
# This is a rehearsal: no real NetBox, no real BMC, no real Jira. Use it to see
# what a run looks like, and to re-check the safety behaviour after changing
# config/ownership.yaml.
#
#     ./tools/rehearse.sh            # full pipeline, then tear the mocks down
#     ./tools/rehearse.sh --keep     # leave the mocks running afterwards
#
# The mock BMC has to hold port 443, because Redfish is addressed as
# https://<bmc-ip>/redfish/v1 with no port component, so this asks for sudo.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

WORK="$ROOT/var/rehearsal"
CONFIG="$WORK/config"
ENV_FILE="$WORK/env"
NETBOX_LOG="$WORK/mock-netbox.log"
BMC_LOG="$WORK/mock-bmc.log"

NBRECON="${NBRECON:-$ROOT/.venv/bin/nbrecon}"
if [[ ! -x "$NBRECON" ]]; then
  NBRECON="$(command -v nbrecon || true)"
fi
if [[ -z "$NBRECON" ]]; then
  echo "nbrecon not found. Install it first:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

BMC_PID_FILE="$WORK/mock-bmc.pid"

# Shut down by PID, not by pattern: `pkill -f mock_lab.py` also matches the
# command line of whatever shell invoked it.
cleanup() {
  if [[ $KEEP == 1 ]]; then
    say "Mocks left running. Stop them with: kill \$(cat $WORK/mock-netbox.pid) && sudo kill \$(cat $BMC_PID_FILE)"
    return
  fi
  [[ -f "$WORK/mock-netbox.pid" ]] && kill "$(cat "$WORK/mock-netbox.pid")" 2>/dev/null || true
  [[ -f "$BMC_PID_FILE" ]] && sudo kill "$(cat "$BMC_PID_FILE")" 2>/dev/null || true
}
trap cleanup EXIT

# --- fixture config -------------------------------------------------------
# The ownership matrix is taken from the real config/ so a rehearsal always
# exercises the rules that are actually in force. Only the two placeholder
# files are overlaid.
say "Building rehearsal config in $CONFIG"
rm -rf "$WORK"
mkdir -p "$CONFIG"
cp "$ROOT/config/ownership.yaml" "$CONFIG/"
cp "$ROOT/examples/rehearsal/netbox_fields.yaml" "$CONFIG/netbox_fields.yaml"
cp "$ROOT/examples/rehearsal/sku_map.yaml" "$CONFIG/sku_map.yaml"

cat > "$ENV_FILE" <<'EOF'
NBRECON_NETBOX_URL=http://127.0.0.1:8000
NBRECON_NETBOX_TOKEN=rehearsal-netbox-token
NBRECON_BMC_USER=rehearsal
NBRECON_BMC_PASSWORD=rehearsal
NBRECON_BMC_VERIFY_TLS=false
NBRECON_BMC_TIMEOUT=10
NBRECON_MAX_BATCH=5
NBRECON_ARTIFACT_DIR=var/rehearsal/runs
NBRECON_RUN_HISTORY_DB=var/rehearsal/run-history.db
EOF
chmod 600 "$ENV_FILE"

# --- mock lab -------------------------------------------------------------
say "Starting the mock NetBox and mock BMC"
python3 "$ROOT/tools/mock_lab.py" --serve-netbox --port 8000 \
  --pid-file "$WORK/mock-netbox.pid" >"$NETBOX_LOG" 2>&1 &
sudo python3 "$ROOT/tools/mock_lab.py" --serve-bmc --port 443 \
  --pid-file "$BMC_PID_FILE" >"$BMC_LOG" 2>&1 &

for _ in $(seq 1 40); do
  if curl -fsS -m 1 -H "Authorization: Token rehearsal-netbox-token" \
       http://127.0.0.1:8000/api/status/ >/dev/null 2>&1 \
     && curl -fsSk -m 1 -u rehearsal:rehearsal \
       https://127.0.0.1/redfish/v1/ >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

run() { "$NBRECON" --env-file "$ENV_FILE" --config-dir "$CONFIG" "$@"; }

# --- the pipeline ---------------------------------------------------------
say "1/6  preflight - can we reach everything, and what is configured?"
run preflight --show-custom-fields || true

say "2/6  collect - read NetBox, Redfish and the tt-smi export (read-only)"
run collect \
  --scope-file "$ROOT/examples/rehearsal/scope.yaml" \
  --ansible-facts "$ROOT/examples/rehearsal/ttsmi-facts.json"

say "3/6  plan - correlate and diff. Still writes nothing."
run plan --show-all

say "4/6  approve - answering 'all' to approve every eligible field"
printf 'all\n' | run approve --approver "rehearsal@tenstorrent.com"

say "5/6  apply - the first and only stage that writes"
run apply --yes

say "6/6  verify - read the values back out of NetBox"
run verify

say "Run history"
run history --limit 5

say "Artifacts"
find "$WORK/runs" -type f | sort | sed 's/^/  /'
