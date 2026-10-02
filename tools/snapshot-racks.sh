#!/usr/bin/env bash
#
# Read NetBox for several racks, one CSV per rack. Reads only.
#
# Wraps `nbrecon snapshot`, which contacts nothing but NetBox and needs only
# NBRECON_NETBOX_URL and NBRECON_NETBOX_TOKEN. No BMC credentials, no Redfish,
# no run directory, no run-history entry.
#
#     ./tools/snapshot-racks.sh F08 F09 F13
#     ./tools/snapshot-racks.sh --site austin-drt F08 F09
#     ./tools/snapshot-racks.sh --out-dir var/october --max-devices 500 F08
#
# One rack failing does not stop the others. Every failure is repeated in a
# summary at the end and sets a non-zero exit code, because a sweep that
# half-worked and said nothing is worse than one that stopped.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT_DIR="var/snapshots"
MAX_DEVICES=200
SITE=""
ENV_FILE=""

usage() {
  sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --site SLUG          NetBox site slug. Needed when a rack name exists at
                       more than one site, which NetBox allows
  --out-dir DIR        Where the CSVs go (default: var/snapshots)
  --max-devices N      Scope limit per rack (default: 200)
  --env-file FILE      Path to .env, passed through to nbrecon
  -h, --help           This message
EOF
}

# --- arguments ------------------------------------------------------------
RACKS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site)        SITE="${2:-}";        shift 2 ;;
    --out-dir)     OUT_DIR="${2:-}";     shift 2 ;;
    --max-devices) MAX_DEVICES="${2:-}"; shift 2 ;;
    --env-file)    ENV_FILE="${2:-}";    shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    --*)           echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)             RACKS="${RACKS}${1}"$'\n'; shift ;;
  esac
done

if [[ -z "$RACKS" ]]; then
  echo "no racks given" >&2
  usage >&2
  exit 2
fi

if ! [[ "$MAX_DEVICES" =~ ^[0-9]+$ ]] || [[ "$MAX_DEVICES" -lt 1 ]]; then
  echo "--max-devices must be a positive integer, got: $MAX_DEVICES" >&2
  exit 2
fi

# --- preconditions --------------------------------------------------------
# Both of these are mistakes made far more often than a bad rack name: a shell
# without the venv activated, and a missing .env. Catching them here costs one
# line each and saves a confusing failure per rack.
NBRECON="${NBRECON:-$ROOT/.venv/bin/nbrecon}"
if [[ ! -x "$NBRECON" ]]; then
  NBRECON="$(command -v nbrecon || true)"
fi
if [[ -z "$NBRECON" ]]; then
  echo "nbrecon not found. Activate the venv, or install it:" >&2
  echo "    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

CHECK_ENV="${ENV_FILE:-$ROOT/.env}"
if [[ ! -f "$CHECK_ENV" ]]; then
  echo "no env file at $CHECK_ENV" >&2
  echo "    cp .env.example .env && chmod 600 .env" >&2
  echo "Then set NBRECON_NETBOX_URL and NBRECON_NETBOX_TOKEN; nothing else is needed." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# Explicit template rather than `mktemp -t prefix`: BSD and GNU mktemp read
# that form differently, and GNU rejects a template with no X's outright.
SCOPE="$(mktemp "${TMPDIR:-/tmp}/nbrecon-scope.XXXXXX")" || {
  echo "could not create a temporary scope file" >&2
  exit 1
}
trap 'rm -f "$SCOPE"' EXIT

run() {
  if [[ -n "$ENV_FILE" ]]; then
    "$NBRECON" --env-file "$ENV_FILE" "$@"
  else
    "$NBRECON" "$@"
  fi
}

# --- the sweep ------------------------------------------------------------
ok_count=0
fail_count=0
empty_count=0
failures=""
empties=""

while IFS= read -r rack; do
  [[ -z "$rack" ]] && continue

  # Rack names are free text in NetBox and can carry characters that are not
  # safe in a filename. The scope file still gets the name exactly as typed.
  safe="$(printf '%s' "$rack" | tr -c '[:alnum:]._-' '_')"
  csv="$OUT_DIR/$safe.csv"
  log="$OUT_DIR/$safe.log"

  {
    printf 'rack: %s\n' "$rack"
    printf 'max_devices: %s\n' "$MAX_DEVICES"
    [[ -n "$SITE" ]] && printf 'site: %s\n' "$SITE"
  } > "$SCOPE"

  printf '\n\033[1;36m== %s\033[0m\n' "$rack"

  if run snapshot --scope-file "$SCOPE" --csv "$csv" --quiet >"$log" 2>&1; then
    sed 's/^/  /' "$log"
    printf '  -> %s\n' "$csv"
    ok_count=$((ok_count + 1))
    # A rack that exists but holds nothing is a success, not a failure -- and
    # also not nothing. Worth naming, because it usually means the devices are
    # racked somewhere NetBox does not know about.
    rows=$(($(wc -l < "$csv") - 1))
    if [[ "$rows" -le 0 ]]; then
      empties="${empties}  ${rack}"$'\n'
      empty_count=$((empty_count + 1))
    fi
  else
    status=$?
    sed 's/^/  /' "$log"
    printf '  \033[1;31mFAILED\033[0m (exit %d)\n' "$status"
    # Keep the last line: nbrecon's errors are one sentence, and it is the
    # part worth repeating in the summary.
    failures="${failures}  ${rack}: $(tail -n 1 "$log")"$'\n'
    fail_count=$((fail_count + 1))
    rm -f "$csv"
  fi
done <<< "$RACKS"

# --- summary --------------------------------------------------------------
printf '\n\033[1m%d rack(s) read, %d failed\033[0m\n' "$ok_count" "$fail_count"

if [[ "$empty_count" -gt 0 ]]; then
  printf '\n\033[1;33m%d rack(s) exist in NetBox but hold no devices\033[0m\n%s' \
    "$empty_count" "$empties"
fi

if [[ "$fail_count" -gt 0 ]]; then
  printf '\n\033[1;31mFailures\033[0m\n%s' "$failures"
  printf '\nA rack name that does not resolve is an error, not an empty result.\n'
  printf 'If a name matches several racks, pass --site to choose one.\n'
  exit 1
fi

printf 'CSVs in %s\n' "$OUT_DIR"
