import json
import os
import re
import statistics
import string
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


# ---------- Signal 2: Stylometric heuristics ----------

def compute_stylometric_score(text: str) -> dict:
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    if not sentences:
        return {"stylo_score": 0.5, "var_score": 0.5, "ttr_score": 0.5, "punct_score": 0.5}

    # Sentence-length variance: low variance → uniform → more AI-like → higher score
    lengths = [len(s.split()) for s in sentences]
    std_dev = statistics.stdev(lengths) if len(lengths) > 1 else 0.0
    var_score = max(0.0, 1.0 - std_dev / 15.0)

    # Type-token ratio: broader vocabulary diversity → more AI-like → higher score
    words = re.findall(r'\b\w+\b', text.lower())
    ttr_score = len(set(words)) / len(words) if words else 0.5

    # Punctuation density: AI text uses more punctuation per sentence → higher score
    punct_count = sum(1 for c in text if c in string.punctuation)
    punct_per_sentence = punct_count / len(sentences)
    punct_score = min(punct_per_sentence / 4.0, 1.0)

    stylo_score = (var_score + ttr_score + punct_score) / 3.0

    return {
        "stylo_score": round(stylo_score, 4),
        "var_score": round(var_score, 4),
        "ttr_score": round(ttr_score, 4),
        "punct_score": round(punct_score, 4),
    }


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

    # Signal 2: stylometrics (always runs, no external dependency)
    stylo = compute_stylometric_score(text)
    stylo_score = stylo["stylo_score"]

    # Signal 1: Groq LLM
    try:
        llm_score = classify_with_groq(text)
        llm_fallback = False
    except Exception as e:
        with open("debug.log", "a") as f:
            f.write(f"Groq call failed: {type(e).__name__}: {e}\n")
        llm_score = stylo_score  # fall back to stylometrics rather than blind 0.5
        llm_fallback = True

    # Combined confidence: LLM weighted higher (semantic > structural)
    confidence = round(0.6 * llm_score + 0.4 * stylo_score, 4)

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
        "confidence": confidence,
        "llm_score": round(llm_score, 4),
        "stylo_score": stylo_score,
        "var_score": stylo["var_score"],
        "ttr_score": stylo["ttr_score"],
        "punct_score": stylo["punct_score"],
        "llm_fallback": llm_fallback,
        "status": "classified",
    })

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": round(llm_score, 4),
        "stylo_score": stylo_score,
        "label": label,
    })


@app.route("/log", methods=["GET"])
def get_log():
    limit = request.args.get("limit", 20, type=int)
    entries = load_log()
    return jsonify({"entries": entries[-limit:]})


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
