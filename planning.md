# Provenance Guard — Planning Document

## Architecture

A submission enters via **POST /submit** (`text`, `creator_id`). The endpoint assigns a UUID `content_id`, runs both detection signals in parallel, combines their scores into a single `confidence` value, maps that to a transparency label, writes a structured audit log entry, and returns the result.

An appeal enters via **POST /appeal** (`content_id`, `creator_reasoning`). The endpoint looks up the original entry, updates its status to `"under_review"`, appends an appeal record to the audit log, and returns a confirmation. No re-classification occurs.

### Submission Flow

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

### Appeal Flow

```
POST /appeal { content_id, creator_reasoning }
        |
[Input Validation]  →  400 / 404 if invalid
        |
[Status Updater]  →  status: "under_review"
        |
[Audit Logger]  appends appeal record
        |
Response: { status: "received", content_id, message }
```

---

## API Surface

| Method | Endpoint | Accepts | Returns |
|--------|----------|---------|---------|
| POST | /submit | `{ text, creator_id }` | `{ content_id, attribution, confidence, llm_score, stylo_score, label }` |
| POST | /appeal | `{ content_id, creator_reasoning }` | `{ status, message, content_id }` |
| GET | /log | — | `{ entries: [...] }` |

Rate limiting: **10 requests/minute, 100/day** on `/submit` only.

---

## Detection Signals

### Signal 1 — LLM Classifier (Groq / llama-3.3-70b-versatile)

**What it measures:** Semantic and stylistic coherence holistically — tone, narrative voice, hedging patterns, personal specificity. AI text overuses transition phrases, lacks genuine specificity, and has a consistent "assistant voice" that the model recognizes.

**Output:** `classify_with_groq(text) -> float` (`llm_score` ∈ [0.0, 1.0]). The prompt instructs the model to return `{"score": <float>}`; a regex fallback handles cases where the model wraps JSON in prose.

**Blind spot:** Formal academic writing, legal prose, and non-native-speaker English mimic AI's regular, hedged register. The model also cannot reliably detect output from LLMs with different training distributions.

---

### Signal 2 — Stylometric Heuristics (pure Python)

**What it measures:** Statistical uniformity — AI text has consistent sentence lengths, broad but generic vocabulary, and regular punctuation. Human writing is variable: long and short sentences mixed, repeated personal vocabulary, erratic punctuation.

**Output:** `compute_stylometric_score(text) -> dict` with `stylo_score` ∈ [0.0, 1.0] plus individual sub-metric values.

**Sub-metrics and normalization:**

| Sub-metric | Formula | AI direction |
|---|---|---|
| Sentence-length variance | `var_score = max(0, 1 - std_dev / 15)` | Low variance → higher score |
| Type-token ratio (TTR) | `ttr_score = unique_words / total_words` | Higher TTR → higher score |
| Punctuation density | `punct_score = min(punct_per_sentence / 4.0, 1.0)` | Higher density → higher score |

`stylo_score = (var_score + ttr_score + punct_score) / 3`

**Blind spot:** Formal human genres (academic, technical, legal) are structurally uniform and score high on all three sub-metrics — the same as AI text.

---

## Confidence Scoring

```
confidence = 0.6 × llm_score + 0.4 × stylo_score
```

The LLM gets the higher weight because it captures meaning; stylometrics is structural and easier to fool by genre. Both scores are logged separately before combination. If the Groq call fails, `llm_score` falls back to `stylo_score` and the entry is flagged `"llm_fallback": true`.

**Thresholds:**

| Confidence | Attribution | Rationale |
|---|---|---|
| < 0.35 | `likely_human` | Strong human signal; safe to show positive label |
| 0.35 – 0.64 | `uncertain` | Not enough evidence to commit; false accusation would be unjust |
| ≥ 0.65 | `likely_ai` | Sufficient AI signal to flag; may still be wrong |

The AI threshold sits at 0.65 (above midpoint) and the human threshold at 0.35 (below midpoint) — the system needs stronger evidence to accuse than to exonerate. A false accusation on a creative writing platform damages a creator's reputation more than a missed AI label.

**False positive example:** A non-native English speaker submits a formal personal essay. Signal 1 ≈ 0.70 (formal register), Signal 2 ≈ 0.65 (consistent structure). Combined: 0.68 → `likely_ai`. The label acknowledges uncertainty and offers an appeal path. The creator appeals; status becomes `"under_review"` for human review.

---

## Transparency Labels

`{confidence_pct}` = `round(confidence × 100)` at render time.

**Likely AI-Generated (confidence ≥ 0.65)**
```
[ Likely AI-Generated Content ]

Our automated analysis found patterns associated with AI-generated writing
(AI signal strength: {confidence_pct}%). This label is generated by an
automated system and may be incorrect.

If you are the creator and wrote this yourself, you can contest this
classification by submitting an appeal. Appeals are reviewed by a human.
```

**Attribution Uncertain (0.35 ≤ confidence < 0.65)**
```
[ Attribution Uncertain ]

Our system could not confidently determine whether this content was written
by a human or produced by an AI tool (AI signal strength: {confidence_pct}%).
Both human and AI-generated text can produce results in this range.

If this classification affects you, you may submit an appeal.
```

**Likely Human-Written (confidence < 0.35)**
```
[ Likely Human-Written Content ]

Our automated analysis found patterns consistent with human-written content
(AI signal strength: {confidence_pct}%). This assessment is automated and
may not be accurate in all cases.
```

Only the AI label mentions appeals — that is where the false-positive risk is highest and a clear path to contest matters most.

---

## Appeals Workflow

Accepts: `content_id` (must match an existing submission) and `creator_reasoning` (non-empty free text).

1. Look up entry by `content_id` — return 404 if not found.
2. If status is already `"under_review"`, return current state (idempotent).
3. Update entry status from `"classified"` to `"under_review"`.
4. Append appeal record to audit log: `{ content_id, creator_reasoning, appeal_timestamp, status, original_attribution, original_confidence }`.
5. Return `{ "status": "received", "content_id": "...", "message": "Your appeal has been logged and will be reviewed by a human." }`.

Original `confidence` and `attribution` are not overwritten. A reviewer sees the original submission entry and the appeal entry side-by-side in `GET /log`.

---

## Known Edge Cases

**Poetry with deliberate repetition** — Anaphora and rhythmically uniform short lines produce low TTR and low sentence variance, both scoring as AI-like. The stylometric signal has no concept of literary form; genre detection would be needed to fix this.

**Technical/instructional writing** — Tutorials, recipes, and documentation are structurally uniform by design (numbered steps, consistent register, dense punctuation). Both signals score these as AI-like. Confidence can reach 0.70–0.80 for entirely human-written technical content. Documented as a known limitation in the README.

---

## AI Tool Plan

**M3 — Submission endpoint + Signal 1**
Provide: Signal 1 output format + submission flow diagram. Ask for: Flask app skeleton with `/submit` stub + `classify_with_groq(text) -> float`. Verify: call the function on 3 inputs and confirm a float [0,1] is returned; test JSON parsing handles prose wrapping.

**M4 — Signal 2 + confidence scoring**
Provide: Signal 2 normalization formulas + threshold table + diagram. Ask for: `compute_stylometric_score(text) -> dict` + `compute_confidence(llm, stylo) -> float`. Verify: run all 4 test inputs; clearly-AI text should score > 0.65, clearly-human casual text < 0.40; print sub-metric values if results are unexpected.

**M5 — Production layer**
Provide: label variant texts + thresholds + appeals workflow steps + diagram. Ask for: `generate_label(confidence) -> dict` + `/appeal` endpoint + `/log` endpoint. Verify: call `generate_label` at 0.20, 0.50, 0.80 and confirm all three texts differ; submit an appeal and confirm `GET /log` shows `"under_review"`; confirm 404 on unknown `content_id`; run 12-request burst and confirm 429 after request 10.
