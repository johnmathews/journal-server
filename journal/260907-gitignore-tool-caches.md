# Ignoring the tool caches, and what CI turned up on the way

**Date:** 2026-09-07
**Branch:** `chore/tidy-gitignore` → PR #78

## The change

Added `.pytest_cache/`, `.ruff_cache/` and `.superpowers/` to `.gitignore`.
Part of a cross-repo tidy that also removed ~21 MB of cruft from this
working tree: `htmlcov/` (18 MB), `.coverage`, and those same three cache
directories.

## Why they needed a rule

`git check-ignore -v` said **NOT IGNORED** for all three:

```
$ for d in .pytest_cache .ruff_cache .superpowers; do
    git check-ignore -v "$d" || echo "NOT IGNORED"; done
.pytest_cache      NOT IGNORED
.ruff_cache        NOT IGNORED
.superpowers       NOT IGNORED
```

They never showed up in `git status` anyway, which is exactly why nobody
noticed. pytest and ruff each write a `.gitignore` containing `*` **inside
their own cache directory**. The repo looked clean because two third-party
tools happened to be tidy, not because this repo asserted anything. If
either stops doing that, ~712 KB of cache becomes untracked-file noise and
a candidate for an accidental `git add -A`.

Worth internalising: **"it doesn't show up in `git status`" is not the same
as "it's ignored".** `git check-ignore -v` is the question that actually
answers it, and it names the file and line responsible.

`*.png` and the rest of the personal-content block were left untouched.
Over-ignoring is the safe direction there — the sibling webapp repo had the
opposite problem (an unanchored `*.png` swallowing legitimate assets), and
the same pattern was right in one repo and wrong in the other.

## What CI surfaced

The PR went red, and not because of the `.gitignore`. Lint passed, and so
did the suite — **3270 passed, 11 deselected**. The failing step was
`pip-audit`:

```
Found 5 known vulnerabilities in 2 packages
cryptography 49.0.0  PYSEC-2026-3552  fix: 50.0.0
pyasn1       0.6.3   PYSEC-2026-3456  fix: 0.6.4
pyasn1       0.6.3   PYSEC-2026-3457  fix: 0.6.4
pyasn1       0.6.3   PYSEC-2026-3455  fix: 0.6.4
```

Entirely pre-existing — any PR opened against `main` right now hits it. It
had gone unseen because CI only runs on push to `main` and on PRs, and
neither had happened since 2026-07-20.

`pyasn1` is transitive (via `pyasn1_modules`) and bumps cleanly with
`uv lock --upgrade-package pyasn1` — lockfile-only. `cryptography` is
pinned `>=49.0,<50` in `pyproject.toml`, so its fix version sits outside
the declared range and needs the bound widened; it resolves to 50.0.1 with
no other package moving. That matters here because `cryptography` is not
incidental — it is the Fernet implementation behind saved Garmin
credentials (`services/fitness/credentials.py`).

Both bumps were tried locally: `cryptography` 49.0.0 -> 50.0.1 and `pyasn1`
0.6.3 -> 0.6.4, no other package moving, and `uv run pytest -m "not
integration"` gave **3270 passed** — identical to the pre-bump run. Left
uncommitted pending a call on whether a major bump of the Fernet library
belongs in a tidy-up PR.

## Note for next time

A dependency-vulnerability gate that only runs on PRs to `main` will hold a
finding silently for as long as nobody opens one. Seven weeks passed here.
A scheduled run would have surfaced it the week it landed.
