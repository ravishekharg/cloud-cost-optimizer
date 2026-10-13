#!/usr/bin/env bash
# Scan -> fix -> rescan against a moto server seeded with waste. Needs a moto server and costopt
# installed (pip install -e .):
#   docker run -d --name moto -p 5055:5000 -e MOTO_EC2_LOAD_DEFAULT_AMIS=false motoserver/moto:5.2.4
#   bash demo/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_ENDPOINT_URL=${AWS_ENDPOINT_URL:-http://localhost:5055}
export AWS_ACCESS_KEY_ID=demo AWS_SECRET_ACCESS_KEY=demo AWS_DEFAULT_REGION=us-east-1
OUT=${OUT:-demo/out}
mkdir -p "$OUT"
# Everything in the seeded account was created a minute ago, so turn the age guards off.
FLAGS="--min-age-days 0 --snapshot-age-days 0 --stopped-days 0"

curl -sf -X POST "$AWS_ENDPOINT_URL/moto-api/reset" >/dev/null
python demo/seed.py

echo; echo "== 1. Scan"
costopt aws $FLAGS
costopt aws $FLAGS --format json --out "$OUT/findings.json"
costopt aws $FLAGS --format markdown --out "$OUT/report.md"
before=$(python -c "import json;print(json.load(open('$OUT/findings.json'))['summary']['total_monthly_saving'])")

echo; echo "== 2. Plan (dry run)"
costopt fix "$OUT/findings.json"

echo; echo "== 3. Apply the safe fixes"
costopt fix "$OUT/findings.json" --apply --audit-log "$OUT/audit.jsonl"

echo; echo "== 4. Rescan (default snapshot age, so fix's own safety snapshots aren't counted as old)"
costopt aws --min-age-days 0 --stopped-days 0 --format json --out "$OUT/after.json"
after=$(python -c "import json;print(json.load(open('$OUT/after.json'))['summary']['total_monthly_saving'])")
python - "$before" "$after" <<'EOF'
import sys
before, after = map(float, sys.argv[1:])
print(f"Found ${before:,.2f}/month; after `costopt fix --apply`: ${after:,.2f}/month left for humans "
      f"(rightsizing, stopped instance, idle LB/NAT). Fixed automatically: ${before - after:,.2f}/month.")
assert after < before, "fix should have removed the automatable findings"
EOF
