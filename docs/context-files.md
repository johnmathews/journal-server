# Context Files

Context files are local markdown files describing the proper nouns, places, recurring topics, and
domain-specific terms that show up in your handwritten and dictated journal entries. The server
loads them at startup and uses them to prime two pipelines:

1. **OCR** — injected as part of the system prompt so the vision model prefers known spellings
   when the handwriting is ambiguous. See [`ocr-context.md`](ocr-context.md) for the full
   mechanism, cache strategy, and failure modes.
2. **Voice transcription** — stripped of markdown, truncated to ~200 tokens, and passed to
   OpenAI Whisper as the `prompt` parameter so it biases toward the same spellings. Added
   2026-04-28.

A single set of files drives both pipelines.

## Where they live

```
journal-server/
  context/
    README.md       <- shipped, gitignored otherwise
    people.md       <- you create
    places.md       <- you create
    topics.md       <- you create
    glossary.md     <- you create
```

Configured via `OCR_CONTEXT_DIR` in your `.env` file. The directory is local-only — `.gitignore`
keeps everything except `README.md` out of the repo.

## File format

Plain markdown. Bullet points with bold names work best. Filenames become category labels in the
OCR system prompt, so pick descriptive stems.

```markdown
# People

- **Adi** — close friend (sometimes mis-spelled "Addy" or "Eddie")
- **Dr. Patel** — physiotherapist
- **Ritsya** — daughter (also "Ritzya")
```

```markdown
# Places

- **Hampstead Heath** — park I run on
- **Blue Bottle** — café in North London
- **Old Street** — London Underground station
```

```markdown
# Topics

- **journal-server** — the Python backend in this monorepo
- **Mosaic** — the admin UI template the webapp is derived from
- **dual-pass OCR** — Anthropic + Gemini reconciliation
```

```markdown
# Glossary

- **PRAGMA user_version** — SQLite migration version mechanism
- **HRV** — heart-rate variability
- **microcycle** — a 7-10 day training block
```

## Does this help voice transcription?

Yes — for proper nouns in particular. Whisper's `prompt` parameter is documented to bias the
model toward correct spellings of the words it contains. So names like `Adi` or `Ritsya` that
Whisper would otherwise spell phonetically should now come back correctly.

The `prompt` is **not** a system instruction; it's a piece of preceding text that Whisper
treats as if it were the speaker's previous utterance. That's why dense lists of names work
better than long-form prose — there's no time spent "understanding" the prompt, only spelling
priors.

### What works best for transcription

- **Proper nouns** — names, places, brands, neighbourhoods.
- **Unusual words** — domain jargon, technical terms, foreign words.
- **Spellings of homophones** — e.g. "Hampstead" vs "Hampsted".

### What doesn't help

- Long-form descriptions of relationships ("Adi is my best friend from Berlin").
- Paragraphs of context — the 200-token cap is tight; anything beyond proper nouns and key
  terms gets truncated.

The builder strips markdown structure (headings, bullets, bold) before sending to Whisper, so
you can keep your files human-readable without paying for the syntax in token budget.

## Token budget

| Pipeline                       | Cap                   | Notes                                                            |
|--------------------------------|-----------------------|------------------------------------------------------------------|
| OCR (Anthropic)                | ~no practical limit   | Cached per session via `cache_control`. See ocr-context.         |
| OCR (Gemini)                   | ~no practical limit   | No caching, but generally fast.                                  |
| Transcription (OpenAI)         | **200 tokens**        | Hard truncation at a token boundary. Whisper-style spelling bias.|
| Transcription (Gemini)         | ~no practical limit   | Full glossary becomes the system instruction (not a `prompt`).   |

The 200-token cap **only applies to the OpenAI transcription adapters** (`gpt-4o-transcribe`,
`gpt-4o-mini-transcribe`, `whisper-1`), because the OpenAI `/audio/transcriptions` endpoint
exposes spelling bias only via the `prompt` parameter — which has a documented hard cap. If
the composed context exceeds 200 tokens, the OpenAI prompt truncates first and the remaining
content (deeper into alphabetically-ordered files) is silently dropped from the OpenAI voice
pipeline only.

The Gemini transcription adapter is different: it accepts a `system_instruction` and the full
markdown glossary is wired into it verbatim, so it sees the entire context regardless of size.
This is one of the reasons to prefer Gemini when the glossary is large enough that OpenAI
truncation is biting (see `docs/transcription-providers.md`). The OCR pipeline always sees the
full set.

If you're hitting the cap, reorder by importance — name files like `01-people.md`,
`02-places.md` to control which entries survive the truncation.

## Reloading after edits

Both pipelines load context files **once at startup** and then cache them in memory. To pick up
edits without a full container restart, hit the admin-only reload endpoints (admin user, session
cookie or API key):

```bash
curl -X POST -b "session_id=$ADMIN_SESSION" http://localhost:8400/api/admin/reload/ocr-context
curl -X POST -b "session_id=$ADMIN_SESSION" http://localhost:8400/api/admin/reload/transcription-context
```

Each endpoint rebuilds the relevant provider stack against the on-disk files. The webapp's
Admin Server tab also surfaces these as buttons (closed roadmap item 41).

A full restart still works as a fallback:

```bash
ssh media
docker compose restart journal-server
```

## Toggling the feature

Two independent toggles control the two pipelines:

- `OCR_CONTEXT_DIR` — must be set for either pipeline to use context files. Unset = no context.
- `TRANSCRIPTION_CONTEXT_ENABLED` (default `true`) — set to `false` if you want OCR priming
  but no transcription priming. Affects whichever transcription provider is active.

Both are also editable at runtime through the admin settings UI without a server restart for
the **toggle**. Edits to the **files themselves** are picked up by the reload endpoints
documented above (`POST /api/admin/reload/{ocr-context,transcription-context}`); only edits
to runtime-static config like provider model names still require a container restart.

## Hallucination warning

Both pipelines include explicit anti-hallucination instructions. The OCR system prompt tells
Claude to only prefer a glossary spelling when the handwritten token is **visually consistent**
with it. The Whisper prompt does not have a system-instruction equivalent, so it's a softer
bias — but Whisper already has a strong language-model prior, and proper-noun context only
nudges it.

Spot-check the first ~20 entries after enabling either feature to confirm you're getting
accuracy gains, not plausible-sounding fabrications. If a name keeps getting auto-corrected
**toward** an entry that wasn't actually said/written, remove it from the file.

## Related docs

- [`ocr-context.md`](ocr-context.md) — full mechanism, cache strategy, and risk analysis for OCR.
- `context/README.md` — quick-start guide for editing the files.
