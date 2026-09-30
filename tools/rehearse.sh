#!/usr/bin/env bash
#
# Run the whole pipeline against the mock lab, on one server, start to finish.
#
# This is a rehearsal: no real NetBox, no real BMC, no real Jira. Use it to see
# what a run looks like, and to re-check the safety behaviour after changing
# config/ownership.yaml.
#
#     ./tools/rehearse.sh            # full pipeline, then tear the mocks down
#     ./tools/rehearse.sh --safety   # assert the safety invariants instead
#     ./tools/rehearse.sh --keep     # leave the mocks running afterwards
#
# The mock BMC has to hold port 443, because Redfish is addressed as
# https://<bmc-ip>/redfish/v1 with no port component, so this asks for sudo.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KEEP=0
SAFETY=0
for arg in "$@"; do
  case "$arg" in
    --keep)   KEEP=1 ;;
    --safety) SAFETY=1 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^#//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

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

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "python3 not found on PATH." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "The mock lab needs python3 3.8 or newer; $PYTHON is $("$PYTHON" -V)." >&2
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
# Settle sudo now, in the foreground. The BMC is started in the background with
# its output redirected, so a prompt raised there would be written to a log file
# and the script would just look like it had hung.
#
# `sudo -n true` rather than `sudo -v`: -v insists on a password even where
# running a command would not have needed one.
if sudo -n true 2>/dev/null; then
  :
elif [[ -t 0 ]]; then
  say "The mock BMC needs port 443, so sudo will ask for your password"
  sudo true
else
  echo "The mock BMC needs sudo to bind port 443, but there is no terminal" >&2
  echo "to prompt on. Run this from an interactive shell." >&2
  exit 1
fi

python3 "$ROOT/tools/mock_lab.py" --serve-netbox --port 8000 \
  --pid-file "$WORK/mock-netbox.pid" >"$NETBOX_LOG" 2>&1 &
# Resolve python3 here rather than letting sudo re-resolve it: sudo resets
# PATH, so `sudo python3` can be a different interpreter than this shell's.
sudo "$PYTHON" "$ROOT/tools/mock_lab.py" --serve-bmc --port 443 \
  --pid-file "$BMC_PID_FILE" >"$BMC_LOG" 2>&1 &

say "Waiting for the mock lab to come up"
UP=0
for _ in $(seq 1 40); do
  if curl -fsS -m 1 -H "Authorization: Token rehearsal-netbox-token" \
       http://127.0.0.1:8000/api/status/ >/dev/null 2>&1 \
     && curl -fsSk -m 1 -u rehearsal:rehearsal \
       https://127.0.0.1/redfish/v1/ >/dev/null 2>&1; then
    UP=1
    break
  fi
  sleep 0.25
done

if [[ $UP == 0 ]]; then
  echo >&2
  echo "The mock lab did not come up. Logs:" >&2
  echo "--- mock NetBox ($NETBOX_LOG) ---" >&2
  tail -n 20 "$NETBOX_LOG" >&2 2>/dev/null || true
  echo "--- mock BMC ($BMC_LOG) ---" >&2
  tail -n 20 "$BMC_LOG" >&2 2>/dev/null || true
  echo >&2
  echo "Most common causes: port 8000 or 443 already in use, or openssl" >&2
  echo "could not generate the mock BMC certificate." >&2
  exit 1
fi

run() { "$NBRECON" --env-file "$ENV_FILE" --config-dir "$CONFIG" "$@"; }

NB_API="http://127.0.0.1:8000/api/dcim/devices/101/"
NB_AUTH="Authorization: Token rehearsal-netbox-token"

collect_one() {
  run collect --scope-file "$ROOT/examples/rehearsal/scope.yaml" \
    --ansible-facts "$ROOT/examples/rehearsal/ttsmi-facts.json" >/dev/null 2>&1
}
latest_run() { ls -1 "$WORK/runs" | tail -1; }
nb_field() { curl -fsS -H "$NB_AUTH" "$NB_API" | "$PYTHON" -c "import json,sys;print(json.load(sys.stdin)$1)"; }
nb_patch() {
  curl -fsS -X PATCH -H "$NB_AUTH" -H "Content-Type: application/json" \
    -d "$1" "$NB_API" >/dev/null
}

if [[ $SAFETY == 1 ]]; then
  PASSED=0
  FAILED=0
  ok()   { printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; PASSED=$((PASSED + 1)); }
  bad()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; FAILED=$((FAILED + 1)); }
  # Assert on a literal substring rather than a pattern, so a message that
  # changes shape fails loudly instead of matching by accident.
  expect() {
    local label=$1 needle=$2 haystack=$3
    if [[ "$haystack" == *"$needle"* ]]; then ok "$label"; else
      bad "$label"
      printf '        expected to find: %s\n' "$needle"
      printf '        in: %s\n' "$(printf '%s' "$haystack" | tr '\n' ' ' | cut -c1-400)"
    fi
  }

  say "Safety check 1/5  a drifted server produces a plan, and applying it verifies"
  collect_one
  RUN=$(latest_run)
  expect "plan proposes the drifted fields" "Proposed writes     │     9" "$(run plan --run-id "$RUN" 2>&1)"
  printf 'all\n' | run approve --run-id "$RUN" --approver safety >/dev/null 2>&1
  expect "apply writes them" "Applied 9 field(s)" "$(run apply --run-id "$RUN" --yes 2>&1)"
  expect "verify reads them back" "all match" "$(run verify --run-id "$RUN" 2>&1)"

  say "Safety check 2/5  reconciliation is idempotent"
  collect_one
  expect "a second run proposes nothing" "Proposed writes     │     0" \
    "$(run plan --run-id "$(latest_run)" 2>&1)"

  say "Safety check 3/5  a plan edited after approval cannot be applied"
  nb_patch '{"custom_fields":{"flash_version":"0.0.1"}}'   # re-introduce drift
  collect_one
  RUN=$(latest_run)
  run plan --run-id "$RUN" >/dev/null 2>&1
  printf 'all\n' | run approve --run-id "$RUN" --approver safety >/dev/null 2>&1
  "$PYTHON" - "$WORK/runs/$RUN/plan.json" <<'PY'
import json, sys
path = sys.argv[1]
plan = json.load(open(path))
for proposal in plan["proposals"]:
    if proposal["field_key"] == "fw_flash":
        proposal["proposed"] = "9.9.9-tampered"
json.dump(plan, open(path, "w"), indent=2)
PY
  expect "apply refuses the edited plan" "approval does not match this plan" \
    "$(run apply --run-id "$RUN" --yes 2>&1 || true)"
  expect "and nothing was written" "0.0.1" "$(nb_field "['custom_fields']['flash_version']")"

  say "Safety check 4/5  a human editing NetBox mid-run wins"
  collect_one
  RUN=$(latest_run)
  run plan --run-id "$RUN" >/dev/null 2>&1
  printf 'all\n' | run approve --run-id "$RUN" --approver safety >/dev/null 2>&1
  nb_patch '{"custom_fields":{"assignment":"edited-by-a-human"}}'
  APPLY_OUT=$(run apply --run-id "$RUN" --yes 2>&1 || true)
  expect "the device is skipped whole" "Applied 0 field(s)" "$APPLY_OUT"
  expect "and the reason is reported" "edited since the snapshot" "$APPLY_OUT"
  expect "the human edit survives" "edited-by-a-human" "$(nb_field "['custom_fields']['assignment']")"

  say "Safety check 5/5  a failed collection never clears anything"
  BEFORE=$(nb_field "['custom_fields']['topology']")
  [[ -f "$BMC_PID_FILE" ]] && sudo kill "$(cat "$BMC_PID_FILE")" 2>/dev/null || true
  sleep 1
  expect "the host is recorded as unreachable" "0 reachable, 1 failed" \
    "$(run collect --scope-file "$ROOT/examples/rehearsal/scope.yaml" \
         --ansible-facts "$ROOT/examples/rehearsal/ttsmi-facts.json" 2>&1)"
  PLAN_OUT=$(run plan --run-id "$(latest_run)" 2>&1)
  expect "nothing is proposed" "Proposed writes     │     0" "$PLAN_OUT"
  expect "the failure is reported" "Collection failures │     1" "$PLAN_OUT"
  expect "populated values are untouched" "$BEFORE" "$(nb_field "['custom_fields']['topology']")"

  printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASSED" "$FAILED"
  [[ $FAILED == 0 ]] || exit 1
  exit 0
fi

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
