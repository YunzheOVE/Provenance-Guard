import json
import os
import re
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq

load_dotenv(override=True)

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

LOG_FILE = "audit_log.json"


# ---------- Audit log helpers ----------

def load_log() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as f:
        return json.load(f)


def append_log(entry: dict) -> None:
    entries = load_log()
    entries.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(entries, f, indent=2)


# ---------- Signal 1: Groq LLM classifier ----------

GROQ_PROMPT = """\
You are an AI content detection system. Analyze the text below and estimate \
the probability it was produced by an AI writing tool rather than a human.

Return ONLY a JSON object in this exact format: {{"score": <float>}}
score must be between 0.0 (clearly human-written) and 1.0 (clearly AI-generated).

Indicators of AI text: consistent formal register, overused transition phrases \
("it is important to note", "furthermore"), lack of personal specificity, \
uniform sentence rhythm.

Indicators of human text: idiosyncratic word choice, natural digression, \
emotional inconsistency, typos or casual phrasing, genuine personal detail.

Text to analyze:
\"\"\"
{text}
\"\"\"

Return only the JSON object, nothing else."""


def classify_with_groq(text: str) -> float:
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": GROQ_PROMPT.format(text=text)}],
        temperature=0.1,
    )
    raw = response.choices[0].message.content.strip()

    # Direct JSON parse
    try:
        return max(0.0, min(1.0, float(json.loads(raw)["score"])))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        pass

    # Regex fallback: find "score": <number>
    m = re.search(r'"score"\s*:\s*([0-9]*\.?[0-9]+)', raw)
    if m:
        return max(0.0, min(1.0, float(m.group(1))))

    # Last resort: first float-looking token in the response
    m = re.search(r'\b(0\.[0-9]+|1\.0*|0\.0*)\b', raw)
    if m:
        return max(0.0, min(1.0, float(m.group(1))))

    return 0.5  # neutral fallback if all parsing fails


# ---------- Routes ----------

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "Missing required field: text"}), 400
    if not creator_id:
        return jsonify({"error": "Missing required field: creator_id"}), 400

    content_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Signal 1
    try:
        llm_score = classify_with_groq(text)
        llm_fallback = False
    except Exception as e:
        with open("debug.log", "a") as f:
            f.write(f"Groq call failed: {type(e).__name__}: {e}\n")
        llm_score = 0.5
        llm_fallback = True

    # Milestone 3: confidence = llm_score (Signal 2 added in M4)
    confidence = llm_score

    if confidence >= 0.65:
        attribution = "likely_ai"
    elif confidence < 0.35:
        attribution = "likely_human"
    else:
        attribution = "uncertain"

    # Placeholder label — replaced with real label text in M5
    label = f"[Placeholder — AI signal: {round(confidence * 100)}%]"

    append_log({
        "entry_type": "submission",
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "llm_score": round(llm_score, 4),
        "llm_fallback": llm_fallback,
        "status": "classified",
    })

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "llm_score": round(llm_score, 4),
        "label": label,
    })


@app.route("/log", methods=["GET"])
def get_log():
    limit = request.args.get("limit", 20, type=int)
    entries = load_log()
    return jsonify({"entries": entries[-limit:]})


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
