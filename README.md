# Provenance Guard

A backend API that classifies submitted text as human-written or AI-generated, scores confidence in that classification, surfaces a transparency label, and handles creator appeals. Built with Flask, Groq (LLaMA 3.3 70B), and stylometric heuristics.

---

## Architecture Overview

A piece of text takes the following path through the system:

1. **POST /submit** receives `text` and `creator_id`
2. **Input validation** — returns 400 if either field is missing or empty
3. **ID Generator** — assigns a UUID `content_id` for tracking
4. **Signal 1 (Groq LLM)** — sends the text to LLaMA 3.3 70B with a structured prompt; receives a float score 0–1
5. **Signal 2 (Stylometrics)** — computes sentence-length variance, type-token ratio, and punctuation density in pure Python; outputs a float score 0–1
6. **Confidence Scorer** — combines both signals: `0.6 × llm_score + 0.4 × stylo_score`
7. **Label Generator** — maps the combined score to one of three transparency label variants
8. **Audit Logger** — writes a structured JSON entry with all scores and metadata
9. **Response** — returns `content_id`, `attribution`, `confidence`, both signal scores, and the full label text

For appeals: **POST /appeal** receives a `content_id` and `creator_reasoning`, updates the original entry's status to `"under_review"`, appends an appeal record to the audit log, and returns a confirmation. No re-classification occurs.

```
POST /submit { text, creator_id }
        |
[Input Validation]  →  400 if invalid
        |
[ID Generator]  ──────────── content_id (UUID)
        |
   ┌────┴────┐
   ↓         ↓
[Signal 1      [Signal 2
 Groq LLM]     Stylometrics]
   |               |
 llm_score     stylo_score
   └────┬────┘
        ↓
[Confidence Scorer]  0.6×llm + 0.4×stylo
        |
[Label Generator]  maps score → 1 of 3 labels
        |
[Audit Logger]  writes structured JSON entry
        |
Response: { content_id, attribution, confidence, llm_score, stylo_score, label }
```

---

## Detection Signals

### Signal 1 — LLM Classifier (Groq / llama-3.3-70b-versatile)

**What it measures:** Semantic and stylistic coherence holistically — tone, narrative voice, hedging patterns, personal specificity. AI text overuses transition phrases ("it is important to note", "furthermore"), lacks genuine specificity, and has a consistent "assistant voice."

**Why I chose it:** An LLM understands meaning, not just structure. It can detect subtle patterns — like hollow hedging or unnaturally balanced sentence rhythm — that rule-based systems miss entirely.

**Output:** A float in [0.0, 1.0]. The prompt instructs the model to return `{"score": <float>}`; the code includes a regex fallback in case the model wraps JSON in prose.

**What it misses:** Formal academic writing, legal prose, and writing by non-native English speakers all mimic AI's regular, hedged register. The model also cannot reliably detect output from LLMs with different training distributions than its own.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Statistical uniformity. AI text has consistent sentence lengths, broad but generic vocabulary, and regular punctuation. Human writing is variable: long and short sentences mixed, repeated personal vocabulary, erratic punctuation.

**Why I chose it:** Completely independent of Signal 1 — structural vs. semantic. The two signals capture genuinely different properties, which makes the combination more informative than either alone.

**Sub-metrics:**

| Sub-metric | Formula | AI direction |
|---|---|---|
| Sentence-length variance | `max(0, 1 − std_dev / 15)` | Low variance → higher score |
| Type-token ratio (TTR) | `unique_words / total_words` | Higher diversity → higher score |
| Punctuation density | `min(punct_per_sentence / 4.0, 1.0)` | Higher density → higher score |

`stylo_score = (var_score + ttr_score + punct_score) / 3`

**What it misses:** Formal human genres (academic, technical, legal) are structurally uniform by design and score high on all three sub-metrics — indistinguishable from AI text to this signal alone.

---

## Confidence Scoring

```
confidence = 0.6 × llm_score + 0.4 × stylo_score
```

The LLM receives a higher weight because it captures meaning; stylometrics is structural and easier to fool by genre. Both scores are logged separately before combination.

**Thresholds:**

| Confidence | Attribution | Rationale |
|---|---|---|
| < 0.35 | `likely_human` | Strong human signal; safe to show positive label |
| 0.35 – 0.64 | `uncertain` | Not enough evidence to commit either way |
| ≥ 0.65 | `likely_ai` | Sufficient AI signal to flag |

The AI threshold sits at 0.65 (above midpoint) and the human threshold at 0.35 (below midpoint). The system needs stronger evidence to accuse than to exonerate — a false accusation on a creative writing platform damages a creator's reputation more than a missed AI label.

**Validation — two example submissions with noticeably different scores:**

| Input | LLM score | Stylo score | Combined confidence | Attribution |
|---|---|---|---|---|
| Formal AI-sounding text ("It is important to note that artificial intelligence represents a transformative paradigm shift...") | 0.90 | 0.62 | **0.79** | `likely_ai` |
| Casual human text ("ok so i finally tried that new ramen place downtown and honestly? underwhelming...") | 0.10 | 0.52 | **0.27** | `likely_human` |

The 0.52-point gap in combined confidence (0.79 vs 0.27) demonstrates the scoring is not stuck at a constant — it varies meaningfully across clearly different inputs.

---

## Transparency Label

All three variants are shown below with the exact text the system returns. `{pct}` is replaced at runtime with `round(confidence × 100)`.

**High-confidence AI (confidence ≥ 0.65)**
```
[ Likely AI-Generated Content ]

Our automated analysis found patterns associated with AI-generated writing
(AI signal strength: {pct}%). This label is generated by an automated system
and may be incorrect.

If you are the creator and wrote this yourself, you can contest this
classification by submitting an appeal. Appeals are reviewed by a human.
```

**Uncertain (0.35 ≤ confidence < 0.65)**
```
[ Attribution Uncertain ]

Our system could not confidently determine whether this content was written
by a human or produced by an AI tool (AI signal strength: {pct}%).
Both human and AI-generated text can produce results in this range.

If this classification affects you, you may submit an appeal.
```

**High-confidence human (confidence < 0.35)**
```
[ Likely Human-Written Content ]

Our automated analysis found patterns consistent with human-written content
(AI signal strength: {pct}%). This assessment is automated and may not be
accurate in all cases.
```

Only the AI and uncertain labels mention appeals — that is where false-positive risk is highest and a clear path to contest matters most.

---

## Rate Limiting

**Limits:** 10 requests per minute and 100 requests per day, applied to `POST /submit` only.

**Reasoning:**
- A real creator submitting their own work would rarely need more than a few submissions per session. 10/minute is generous for legitimate use.
- An adversary trying to flood the system with probe requests would hit the limit quickly without disrupting legitimate creators.
- 100/day caps sustained abuse while leaving room for a creator who submits multiple drafts throughout the day.
- `/appeal` and `/log` are not rate-limited — contesting a classification should never be gated.

**Evidence — rate limit triggering at request 11:**
```
Request 1:  200 OK
Request 2:  200 OK
...
Request 10: 200 OK
Request 11: 429 Too Many Requests (10 per 1 minute)
Request 12: 429 Too Many Requests (10 per 1 minute)
```

---

## Audit Log

Every attribution decision and appeal is written to `audit_log.json`. Sample entries (three entries shown):

```json
[
  {
    "entry_type": "submission",
    "content_id": "8d35ddb0-0c1b-4ae0-b610-5d7970aa29a1",
    "creator_id": "test-user-1",
    "timestamp": "2026-06-29T04:29:05.617622+00:00",
    "attribution": "likely_ai",
    "confidence": 0.7875,
    "llm_score": 0.9,
    "stylo_score": 0.6188,
    "var_score": 0.5561,
    "ttr_score": 0.8837,
    "punct_score": 0.4167,
    "llm_fallback": false,
    "status": "classified"
  },
  {
    "entry_type": "submission",
    "content_id": "2e74a367-7fba-411c-8d73-7f3c61605dbb",
    "creator_id": "test-user-2",
    "timestamp": "2026-06-29T04:29:06.057009+00:00",
    "attribution": "likely_human",
    "confidence": 0.2696,
    "llm_score": 0.1,
    "stylo_score": 0.5239,
    "var_score": 0.4989,
    "ttr_score": 0.8727,
    "punct_score": 0.2,
    "llm_fallback": false,
    "status": "classified"
  },
  {
    "entry_type": "appeal",
    "content_id": "659e9e99-6081-4fe6-8c2b-9ed8e6829fec",
    "appeal_timestamp": "2026-06-29T04:35:49.240877+00:00",
    "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
    "status": "under_review",
    "original_attribution": "likely_ai",
    "original_confidence": 0.7875
  }
]
```

View the live log at any time: `GET /log`

---

## Known Limitations

**Technical/instructional writing:** Tutorials, recipes, API documentation, and numbered-step guides are structurally uniform by design — consistent register, dense punctuation, low sentence variance. Both signals score these as AI-like. Confidence can reach 0.70–0.80 for entirely human-written technical content. This is a fundamental limitation of the stylometric signal: it has no concept of genre, so it cannot distinguish "uniform because it's a recipe" from "uniform because it's AI-generated."

**Poetry with deliberate repetition:** Anaphora and rhythmically uniform short lines produce low TTR and low sentence variance, both scoring as AI-like. The LLM signal partially compensates (it may recognize literary form), but the stylometric signal has no such awareness.

---

## Spec Reflection

**One way the spec helped:** Defining the three label variants in `planning.md` before writing any code forced a concrete decision about what 0.6 means to a user — not just what threshold to use, but what the label actually says. This made `generate_label()` straightforward to implement because the text already existed; the code just selected between three pre-written strings.

**One way implementation diverged from the spec:** The spec originally described both signals running "in parallel." In practice, they run sequentially — stylometrics first (pure Python, instant), then Groq (external API call, 1–3 seconds). The reorder was intentional: if Groq fails, the system uses the stylometric score as the LLM fallback instead of a blind 0.5, which produces a more meaningful result. This wasn't in the original plan but emerged naturally when wiring the two signals together.

---

## AI Usage

**Instance 1 — Flask app skeleton and Signal 1:** I provided the detection signals section and architecture diagram from `planning.md` and asked the AI to generate the Flask app skeleton with a `POST /submit` stub and the `classify_with_groq()` function. The AI produced a working function, but it used a single `json.loads()` call with no fallback for cases where the LLM wraps JSON in prose. I added two regex fallback layers — one targeting `"score": <number>` and one targeting any float-looking token — because LLMs occasionally add explanation text around the JSON object.

**Instance 2 — Stylometric signal and confidence scoring:** I provided the sub-metric formulas and threshold table from `planning.md` and asked the AI to generate `compute_stylometric_score()` and the confidence combination logic. The AI implemented the formulas correctly, but initially placed the Groq call before stylometrics, which meant Groq failures fell back to a blind 0.5. I revised the order — stylometrics runs first, then Groq uses the stylo score as its fallback — so the system always has at least one meaningful signal even when the external API is unavailable.

---

## Running the Project

```bash
# Clone and set up
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt

# Add your Groq API key to .env
# GROQ_API_KEY=your_key_here

# Start the server
python app.py
```

The server runs at `http://localhost:5000`. Test endpoints with PowerShell `Invoke-RestMethod` or any HTTP client.
