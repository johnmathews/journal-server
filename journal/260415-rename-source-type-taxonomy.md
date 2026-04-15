# Rename source_type to descriptive taxonomy

The `source_type` field on journal entries was renamed from terse internal labels to
human-readable values that capture *how* an entry was created:

| Old value | New value             | Meaning                           |
|-----------|-----------------------|-----------------------------------|
| `ocr`     | `photo`               | Uploaded photo of handwritten page |
| `manual`  | `text_entry`          | Typed directly in the webapp       |
| `import`  | `imported_text_file`  | Uploaded .md/.txt file             |
| `voice`   | `voice`               | Recorded via webapp microphone     |
| (new)     | `imported_audio_file` | Uploaded audio file                |

## Motivation

The input method is important context for understanding an entry. A handwritten page
(`photo`) is slower and more deliberate than a spoken entry (`voice`). The old labels
(`ocr`, `manual`) described the processing pipeline rather than the user's action.

## What changed

- **Migration 0013** renames existing values in-place (`UPDATE entries SET ...`).
  No schema change needed since migration 0007 already removed the CHECK constraint.
- **Ingestion service** now sets `photo` for images, `text_entry` as the default for
  `ingest_text`, and accepts a `source_type` parameter on `ingest_voice` /
  `ingest_multi_voice` (defaults to `voice`).
- **Audio pipeline** threads `source_type` through `api.py -> jobs.py ->
  ingest_multi_voice()` so callers can distinguish live mic recordings (`voice`)
  from uploaded audio files (`imported_audio_file`).
- **API docs** updated with new values and the new `source_type` field on the
  audio ingest endpoint.
- All tests updated (1015 pass, 0 failures).

## Notes

- The webapp VoiceRecordPanel continues to use the default `voice` source_type.
  A future "Upload Audio File" panel would send `source_type: "imported_audio_file"`.
- Existing entries are migrated by the SQL migration. Must also run migration on prod.
