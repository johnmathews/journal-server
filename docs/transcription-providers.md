# Transcription Providers

Voice transcription is the most failure-prone stage of the ingestion pipeline — networks blip,
APIs rate-limit, and proper-noun accuracy depends on a glossary that the underlying API may or
may not actually consume. This document describes the multi-provider transcription stack,
how it's composed at startup, what gets retried, and how to read the operational signals it
emits.

For the OCR-side equivalent (system-prompt glossary, dual-pass reconciliation) see
[`ocr-context.md`](ocr-context.md). For the user-facing context-file format see
[`context-files.md`](context-files.md).

## Why multi-provider

The original transcription stack was OpenAI Whisper only. Two recurring problems pushed it
toward a pluggable design:

1. **Glossary priming on the OpenAI `/audio/transcriptions` endpoint is a soft bias only.** The
   endpoint accepts a `prompt` parameter capped at ~200 tokens, which Whisper treats as
   preceding text rather than as a system instruction. That's enough to nudge spellings of
   proper nouns ("Adi", "Ritsya") but not enough to actually instruct the model — there is no
   way to say "transcribe verbatim, do not paraphrase, do not invent words" through the
   transcriptions endpoint. Adding the **Gemini** adapter makes a real `system_instruction`
   path available, with the entire OCR context glossary wired in as instruction text.
2. **Single-provider outages strand ingestion.** A 5xx or rate-limit at the primary used to
   surface as a permanent ingestion failure, with no automatic retry and no fallback. Adding a
   **retry+fallback** wrapper (with `whisper-1` as the dependable fallback) absorbs transient
   API failures without operator intervention.

A **shadow** wrapper rounds out the design: when evaluating whether to switch primaries, you
can run both providers in parallel on every audio for a week, log word-level diffs, and pick
the winner from real data instead of vendor benchmarks.

## Provider stack

The stack is assembled by `build_transcription_provider()` in
`src/journal/providers/transcription.py` from environment-variable config. It composes nested
wrappers around the primary adapter:

```
Shadow(
    Retrying(
        Primary,
        fallback=OpenAITranscribeProvider(model=TRANSCRIPTION_FALLBACK_MODEL)
    ),
    Shadow
)
```

Both wrappers are **opt-in** in the sense that `Retrying` is skipped when
`TRANSCRIPTION_FALLBACK_ENABLED=false` and `Shadow` is skipped when
`TRANSCRIPTION_SHADOW_PROVIDER` is empty. With defaults you get:

```
Retrying(
    OpenAITranscribeProvider(gpt-4o-transcribe),
    fallback=OpenAITranscribeProvider(whisper-1)
)
```

The server logs the assembled stack at startup, e.g.:

```
Transcription stack: retrying(openai/gpt-4o-transcribe, fb=openai/whisper-1)
```

When shadow mode is active, you'll see something like:

```
Transcription stack: shadow(retrying(openai/gpt-4o-transcribe, fb=openai/whisper-1), gemini/gemini-2.5-pro)
```

## Selecting a primary

Two env vars control the primary adapter:

| Variable                 | Default            | Description                                                              |
| ------------------------ | ------------------ | ------------------------------------------------------------------------ |
| `TRANSCRIPTION_PROVIDER` | `openai`           | `"openai"` or `"gemini"`. Validated at startup; invalid raises.          |
| `TRANSCRIPTION_MODEL`    | per-provider       | Defaults: `gpt-4o-transcribe` (openai), `gemini-2.5-pro` (gemini).       |

**Cross-provider model leakage protection.** If `TRANSCRIPTION_PROVIDER=gemini` and
`TRANSCRIPTION_MODEL=gpt-4o-transcribe` (e.g. you swapped provider but forgot to update the
model), the factory logs an INFO and silently falls back to the provider's default. Same in
reverse. This avoids hard failures from accidentally-mismatched config.

### Supported primary models

**OpenAI** (uses `OPENAI_API_KEY`):

- `gpt-4o-transcribe` — default. Word-level logprobs; logprob-based uncertain spans.
- `gpt-4o-mini-transcribe` — same logprobs surface, ~half the cost, slightly higher WER.
- `whisper-1` — older endpoint. No logprobs surface; uncertain spans always empty. Dependable
  enough that we use it as the default fallback.

**Gemini** (uses `GOOGLE_API_KEY`):

- `gemini-2.5-pro` — default. Accepts full system instruction; uncertain spans come from the
  model self-reporting `uncertain_phrases` (model-introspective, not mechanically grounded).

## Fallback semantics

When the retry+fallback wrapper is enabled (default), the primary adapter is called with retry
on transient errors. After `TRANSCRIPTION_RETRY_MAX_ATTEMPTS` attempts (default 3), the wrapper
logs a warning and routes the request to the fallback adapter — always an OpenAI adapter
running `TRANSCRIPTION_FALLBACK_MODEL` (default `whisper-1`).

Backoff is exponential with a cap:

```
sleep_n = min(BASE_DELAY * 2^n, MAX_DELAY)
# defaults: 1s, 2s, 4s … capped at 30s
```

Tunables:

| Variable                              | Default | Description                                            |
| ------------------------------------- | ------- | ------------------------------------------------------ |
| `TRANSCRIPTION_FALLBACK_ENABLED`      | `true`  | Wrap with retry+fallback at all.                       |
| `TRANSCRIPTION_FALLBACK_MODEL`        | `whisper-1` | OpenAI model for the fallback adapter.             |
| `TRANSCRIPTION_RETRY_MAX_ATTEMPTS`    | `3`     | Total attempts at the primary.                         |
| `TRANSCRIPTION_RETRY_BASE_DELAY`      | `1.0`   | Seconds for the first retry sleep.                     |
| `TRANSCRIPTION_RETRY_MAX_DELAY`       | `30.0`  | Upper cap on the retry sleep.                          |

### What counts as transient

The retry classifier (`_is_transient` in `providers/transcription.py`) returns `True` for:

- **OpenAI**: `APITimeoutError`, `APIConnectionError`, `RateLimitError`, `InternalServerError` (5xx).
- **OpenAI non-transient (no retry)**: `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`,
  `BadRequestError`, `UnprocessableEntityError` — these surface immediately to the caller.
- **Gemini**: `ServerError` (any 5xx); `ClientError` only when `code == 429` (rate limit).
- **httpx low-level**: `TimeoutException`, `ConnectError` — Gemini occasionally surfaces these
  for network issues.

Anything not on the transient list raises immediately, skipping retries and the fallback. This
is deliberate — falling back to `whisper-1` on a `BadRequestError` (e.g. unsupported audio
format) would just convert one error into a different one.

### Failure modes

- All retries exhausted, no fallback configured: `PrimaryExhaustedError` is raised, ingestion
  fails, the job runner records the failure.
- All retries exhausted, fallback raises: the fallback's exception propagates. There is no
  second-level retry on the fallback — `whisper-1` is treated as the last line of defence.
- Primary raises a non-transient error on the first attempt: it propagates immediately.
  The fallback is **not** invoked for non-transient errors.

## Shadow mode

Shadow mode runs the primary and a second provider in parallel via a `ThreadPoolExecutor`,
returns the **primary's** result to the caller, and logs a structured word-level diff. The
caller never sees the shadow output — it exists purely to populate offline analytics for
provider evaluation.

> **Operational note: shadow mode is opt-in via `TRANSCRIPTION_SHADOW_PROVIDER`. It DOUBLES the
> per-audio cost, because every request hits both providers. Do not enable it permanently.
> Enable it for a finite evaluation window (e.g. 1-2 weeks of typical audio), then disable.**

Tunables:

| Variable                          | Default | Description                                                |
| --------------------------------- | ------- | ---------------------------------------------------------- |
| `TRANSCRIPTION_SHADOW_PROVIDER`   |         | Empty disables. `"openai"` or `"gemini"` enables.          |
| `TRANSCRIPTION_SHADOW_MODEL`      |         | Empty = the shadow provider's own default model.           |

### What gets logged

A single INFO log per request, structured (extra fields), with the message
`transcription_shadow_diff`. The log carries:

- `primary_chars` / `shadow_chars` — character lengths.
- `similarity_ratio` — `difflib.SequenceMatcher` ratio over full text (0.0-1.0).
- `primary_uncertain_count` / `shadow_uncertain_count` — number of uncertain spans each side reported.
- `diffs` — list of word-level disagreement chunks from `SequenceMatcher.get_opcodes()`,
  filtered to only the `replace` / `insert` / `delete` operations (`equal` runs are dropped).
  Each diff entry has `op`, `primary` (joined words), `shadow` (joined words).
- `shadow_label` — e.g. `"gemini/gemini-2.5-pro"`.

**Full transcripts are never logged.** Only disagreeing chunks. This is deliberate — emitting
two complete transcripts per request would balloon log volume and risk leaking entry content
into log aggregation systems that aren't part of the journal trust boundary.

If the shadow adapter raises, the exception is caught and logged as a warning — the diff log
is skipped, but the primary's result is returned normally so the user-visible request is
unaffected.

## Reading shadow diff logs

In the deployed setup, logs flow through Docker Compose's logger:

```bash
# Tail recent shadow diffs (last 1000 lines, JSON-encoded by python's structured logger)
ssh media
docker compose logs --tail 1000 journal-server | grep transcription_shadow_diff
```

When the deployed setup uses a structured-log shipper (e.g. Loki via Promtail), the `extra`
fields land as queryable labels:

```bash
# Loki / LogQL — find shadows where primary and shadow disagreed at all.
{service="journal-server"} |= "transcription_shadow_diff" | json | diffs_count > 0

# Loki — find low-similarity diffs (significant disagreements).
{service="journal-server"} |= "transcription_shadow_diff" | json | similarity_ratio < 0.95
```

For local development with plain stdout logs, parse with `jq` after a structured-log adapter:

```bash
# Adapt to whatever shape your logger emits — the canonical fields are listed in
# "What gets logged" above.
docker compose logs journal-server --since 1h --no-color \
  | grep transcription_shadow_diff \
  | jq 'select(.diffs | length > 0)'
```

## Operational notes

- **Server restart required to change provider config.** Env vars are read at startup; the
  factory is invoked once and the assembled stack is held by the MCP server / API for the
  process lifetime. This matches OCR's behaviour and the context-files behaviour.
  `docker compose restart journal-server` after edits.
- **The webapp `/settings` page surfaces the active stack but does not edit it.** The settings
  endpoint at `/api/settings` returns the resolved provider name, model, retry/fallback flags,
  and shadow configuration. Editing requires changing env vars and restarting.
- **Shadow doubles cost.** See the warning above. The factory logs the assembled stack at
  startup so it's easy to spot a shadow that was left enabled accidentally.
- **Retry+fallback is the cheapest insurance available.** Defaults are enabled because the
  worst case (3 retries to `gpt-4o-transcribe` then a `whisper-1` fallback) caps the per-audio
  cost at ~4× the single-success cost — well below the operational cost of an ingestion
  failure that the user has to retry by hand.

## Caveats

- **Gemini's `uncertain_phrases` is model-introspective.** The provider asks Gemini to return
  phrases it's uncertain about as part of the structured response. Unlike OpenAI's logprob
  surface (which is mechanically grounded in token-level confidence), Gemini's introspection
  is just the model judging itself. It's useful as a Review-toggle hint but should be
  evaluated against ground truth before being trusted as a primary uncertainty signal.
- **Gemini is more expensive per-call than gpt-4o-transcribe**, but cheaper per minute of
  audio for long files thanks to per-token pricing on Flash-tier and 2.5-Pro models. The
  break-even depends on average audio length — see `docs/external-services.md` for current
  numbers (and the disclaimed pricing-page discrepancy).
- **No cost meter for the shadow path.** If you enable shadow mode, the cost dashboard does
  not reflect the shadow's spend separately. Estimate it as 1× the primary's per-audio cost
  on the same volume, modulo provider price differences.
- **Whisper-1 emits no uncertain spans.** When the retry chain falls through to the fallback,
  the resulting entry will have an empty `uncertain_spans` list. The webapp's Review toggle
  will show no highlights, even if the audio was hard. This is a known-acceptable trade-off
  for the dependability gain.

## Files to read if you change this

- `src/journal/providers/transcription.py` — Protocol, all three adapters
  (`OpenAITranscribeProvider`, `GeminiTranscribeProvider`), both wrappers
  (`RetryingTranscriptionProvider`, `ShadowTranscriptionProvider`), the transient classifier
  (`_is_transient`), and the `build_transcription_provider` factory.
- `src/journal/services/transcription_context.py` — `build_whisper_prompt` (200-token capped
  prompt for OpenAI) and `build_full_context_instruction` (full system instruction for Gemini).
- `src/journal/config.py` — all `transcription_*` env-var fields (`transcription_provider`,
  `transcription_model`, `transcription_fallback_enabled`, `transcription_fallback_model`,
  `transcription_retry_max_attempts`, `transcription_retry_base_delay`, `transcription_retry_max_delay`,
  `transcription_shadow_provider`, `transcription_shadow_model`, plus the context-priming pair
  `transcription_context_enabled`, `transcription_confidence_threshold`) with `__post_init__` validation.
- `src/journal/api/settings.py` — the `/api/settings` block that surfaces the resolved transcription
  config to the webapp. (The single-file `api.py` was split into the `src/journal/api/` package on 2026-05-07.)
- `tests/test_providers/test_transcription.py` — adapter and wrapper unit tests.
- `tests/test_providers/test_transcription_factory.py` — end-to-end stack composition,
  retry/fallback, and shadow diffing.
