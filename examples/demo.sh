#!/usr/bin/env bash
# End-to-end demo against a locally running API (http://127.0.0.1:8000).
# Requires: curl, python3.  Usage:  bash examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
py() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(\"$1\"))"; }

echo "== create batch: allowed categories {cat,dog}; 3 samples =="
CREATE=$(curl -sS -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "categories": ["cat", "dog"],
  "samples": [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "cat"}
  ]
}')
echo "$CREATE" | python3 -m json.tool
BID=$(echo "$CREATE" | py "d['batch_id']")
T1=$(echo "$CREATE"  | py "d['reviewer1_token']")
T2=$(echo "$CREATE"  | py "d['reviewer2_token']")
TA=$(echo "$CREATE"  | py "d['arbiter_token']")

echo; echo "== public view before sealing (only codes + stage, no truth) =="
curl -sS "$BASE/batches/$BID" | python3 -m json.tool

echo; echo "== reviewer 1 submits (S03 = dog, intentionally disagrees) =="
curl -sS -X POST "$BASE/batches/$BID/submission" \
  -H "Authorization: Bearer $T1" -H 'Content-Type: application/json' \
  -d '{"classifications": {"S01": "cat", "S02": "dog", "S03": "dog"}}' \
  | python3 -m json.tool

echo; echo "== reviewer 1 tries to resubmit -> 409, judgement kept =="
curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$BASE/batches/$BID/submission" \
  -H "Authorization: Bearer $T1" -H 'Content-Type: application/json' \
  -d '{"classifications": {"S01": "dog", "S02": "dog", "S03": "dog"}}'

echo; echo "== reviewer 2 submits =="
curl -sS -X POST "$BASE/batches/$BID/submission" \
  -H "Authorization: Bearer $T2" -H 'Content-Type: application/json' \
  -d '{"classifications": {"S01": "cat", "S02": "dog", "S03": "cat"}}' \
  | python3 -m json.tool

echo; echo "== arbiter lists disputes (only S03; true_label never shown) =="
DISPUTES=$(curl -sS "$BASE/batches/$BID/disputes" -H "Authorization: Bearer $TA")
echo "$DISPUTES" | python3 -m json.tool

# Adjudicate every remaining dispute, choosing reviewer 1's label.
echo "$DISPUTES" | python3 -c "
import sys, json
for d in json.load(sys.stdin)['disputes']:
    print(d['code'], d['reviewer1_label'])
" | while read -r code label; do
  echo; echo "== arbiter picks '$label' for $code =="
  curl -sS -X POST "$BASE/batches/$BID/adjudications" \
    -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
    -d "{\"code\": \"$code\", \"label\": \"$label\"}" | python3 -m json.tool
done

echo; echo "== sealed, unblinded result =="
curl -sS "$BASE/batches/$BID" | python3 -m json.tool
