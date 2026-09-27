#!/usr/bin/env bash
# AwLPay FeeManager Bank gate — exactness or refuse.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CUNI="${CUNI:-$(command -v cuni || true)}"
if [[ -z "${CUNI}" ]]; then
  for cand in /workspace/cuni-repo/target/release/cuni /workspace/corpora/toll5-exactness/bin/cuni; do
    if [[ -x "$cand" ]]; then CUNI="$cand"; break; fi
  done
fi
if [[ -z "${CUNI}" || ! -x "${CUNI}" ]]; then
  echo "refuse: cuni binary not found (set CUNI=)" >&2
  exit 1
fi

RECEIPTS="$ROOT/exact/bank_receipts"
mkdir -p "$RECEIPTS"
TARGETS=(py go js sol)
PASS=0
FAIL=0
stamp="$(date '+%Y-%m-%dT%H:%M:%S%z')"

run_one() {
  local src="$1"
  local base
  base="$(basename "$src" .cuni)"
  local out="$RECEIPTS/${base}.txt"
  : >"$out"
  echo "# bank run $stamp  source=$src" >>"$out"
  local t
  for t in "${TARGETS[@]}"; do
    if line="$("$CUNI" bank paste "$src" --from cuni --to "$t" 2>&1)"; then
      echo "$line" | tee -a "$out"
      PASS=$((PASS + 1))
    else
      echo "bank: FAIL — from=cuni to=$t source=$src" | tee -a "$out"
      echo "$line" >>"$out"
      FAIL=$((FAIL + 1))
    fi
  done
}

echo "== FeeManager SoT =="
run_one "$ROOT/exact/FeeManager.cuni"

for f in "$ROOT"/exact/fixtures/*.cuni; do
  echo "== fixture $(basename "$f") =="
  run_one "$f"
done

echo
echo "bank summary: PASS=$PASS FAIL=$FAIL targets=${TARGETS[*]}"
[[ "$FAIL" -eq 0 ]]
