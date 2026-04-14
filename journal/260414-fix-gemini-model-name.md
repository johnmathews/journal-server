# Fix Gemini OCR and improve error UX

## Gemini model name fix

The default Gemini OCR model was set to `gemini-3-pro`, which is not a valid model ID —
Google deprecated "Gemini 3 Pro Preview" and shut it down on 2026-03-09. Ingestion via
Gemini OCR was completely broken (404 NOT_FOUND from the Google API).

Changed the default to `gemini-2.5-pro`, which is the current stable Gemini Pro model.
The model remains overridable via the `OCR_MODEL` environment variable.

Updated cost estimates in `docs/external-services.md` to reflect `gemini-2.5-pro` pricing
($1.25/$10 per MTok vs $2/$12), which drops OCR cost from ~$0.042 to ~$0.032 per 3-page
entry and total pipeline cost from ~$0.10 to ~$0.09.

## Docker Compose env var passthrough

Added `OCR_PROVIDER`, `OCR_MODEL`, and `GOOGLE_API_KEY` to the docker-compose.yml
`environment:` section. These were being configured on the VM but weren't listed in the
repo's compose file, so the container wasn't receiving them.

## Friendly error messages for job failures

Added `_friendly_error()` in `services/jobs.py` that maps raw external-service exceptions
to user-friendly messages. Instead of showing raw dumps like `"503 UNAVAILABLE. {'error': ...}"`
in the webapp notifications, users now see messages like "Google's OCR service is temporarily
overloaded. Please wait a minute and try again."

Covers: Google 503 (overload), 429 (rate limit), 404 (model not found), OpenAI rate limits,
and Anthropic overloaded errors. Unknown errors still pass through unchanged. The raw
exception is always logged server-side for debugging.

## Deployment notes

- `gemini-2.5-pro` requires paid billing on the Google AI project — the free tier has a
  quota of 0 for this model. Billing was enabled 2026-04-14.
- `gemini-2.5-flash` has a free tier and can be used as a fallback via `OCR_MODEL=gemini-2.5-flash`.

## Files changed

- `src/journal/providers/ocr.py` — default param + `_DEFAULT_MODELS` dict
- `src/journal/config.py` — comment
- `src/journal/services/jobs.py` — `_friendly_error()` + updated all `mark_failed` calls
- `docker-compose.yml` — added OCR env vars
- `tests/test_providers/test_ocr.py` — test fixture model name
- `tests/test_services/test_jobs_runner.py` — tests for `_friendly_error`
- `docs/configuration.md` — two model name references
- `docs/external-services.md` — all model references + pricing + cost totals
