#!/usr/bin/env bash
set -euo pipefail

SRT_FILE="${1:?Käyttö: ./analyze_episode.sh <tiedosto.srt> [malli]}"
MODEL="${2:-gemini-4-argon}"

BASE_NAME=$(basename "$SRT_FILE" .srt)
EP_NUM=$(echo "$BASE_NAME" | grep -oE '^[0-9]+' || echo "XX")
OUT_MD="jakso${EP_NUM}_analyysi.md"
OUT_JSON="chapters_${EP_NUM}.json"

echo "Ajetaan malli: $MODEL | Tiedosto: $SRT_FILE"
llm -m "$MODEL" -s "$(cat prompt_chapters.md)" < "$SRT_FILE" > "$OUT_MD"

python3 - "$OUT_MD" "$OUT_JSON" "$EP_NUM" <<'PY_JSON_EOF'
import json, pathlib, re, sys

md_path = pathlib.Path(sys.argv[1])
json_path = pathlib.Path(sys.argv[2])
ep_num = sys.argv[3]

text = md_path.read_text(encoding="utf-8")
m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
if not m:
    print("VAROITUS: JSON-lohkoa ei löytynyt vastauksesta.", file=sys.stderr)
    sys.exit(1)

parsed = json.loads(m.group(1))
if ep_num != "XX":
    parsed["title"] = f"Rane Raitsikka ja Tokela – Jakso {ep_num}"

json_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Tallennettu: {md_path} ja {json_path} ({len(parsed.get('chapters', []))} lukua)")
PY_JSON_EOF

python3 - <<'PY_COST_EOF'
import datetime, json, pathlib, sqlite3, subprocess

db_path = pathlib.Path(subprocess.check_output(["llm", "logs", "path"], text=True).strip())
if not db_path.exists():
    raise SystemExit(0)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
table = "turns" if "turns" in tables else ("responses" if "responses" in tables else None)
if not table:
    raise SystemExit(0)

row = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()
if not row:
    raise SystemExit(0)

data = dict(row)
model_full = data.get("model") or data.get("model_id") or ""
model_key = model_full.split("/")[-1]
inp = data.get("input_tokens") or 0
out = data.get("output_tokens") or 0
details = data.get("token_details")
if isinstance(details, str) and details:
    details = json.loads(details)

if inp == 0 and out == 0 and data.get("response_json"):
    rj = json.loads(data["response_json"]) if isinstance(data["response_json"], str) else data["response_json"]
    usage = (rj or {}).get("usageMetadata") or {}
    inp = usage.get("promptTokenCount") or 0
    cand = usage.get("candidatesTokenCount") or 0
    thoughts = usage.get("thoughtsTokenCount") or 0
    out = cand + thoughts
    if thoughts and not details:
        details = {"candidates": cand, "thoughts": thoughts}

after_nov = datetime.date.today() >= datetime.date(2026, 12, 1)
argon_rate = (4.00, 20.00) if after_nov else (2.00, 10.00)
PRICES = {
    "gemini-4-argon": argon_rate,
    "gemini-4-carbon": argon_rate,
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-neon": (3.00, 15.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}

in_rate, out_rate = PRICES.get(model_key, (0.0, 0.0))
cost_usd = (inp / 1_000_000) * in_rate + (out / 1_000_000) * out_rate

print("\n--- Token-kulutus ja kustannus ---")
print(f"Malli:          {model_full}")
print(f"Input-tokenit:  {inp:,}")
print(f"Output-tokenit: {out:,}")
if details:
    print(f"Lisätiedot:     {json.dumps(details, ensure_ascii=False)}")
print(f"Yhteensä:       {inp + out:,} tokenia")
if in_rate > 0:
    print(f"Arvioitu hinta: ${cost_usd:.4f} USD")
PY_COST_EOF
