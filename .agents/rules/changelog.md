---
trigger: always_on
description: Record each user prompt, AG feedback, and version changes in CHANGELOG.md filed with date stamp and version number.
---
Whenever completing a user request that modifies repository files, updates version state, or provides substantive technical analysis/feedback:
- Record a new entry at the top of `CHANGELOG.md` (below the file header) filed with a date stamp followed by the current version number from `version.json` (`## YYYY-MM-DD HH:MM — vX.Y.Z`), using `version_manager.append_changelog_entry(...)` or editing `CHANGELOG.md` directly.
- Include:
  - **Prompt**: concise summary or exact text of the user's request.
  - **AG Feedback**: concise summary of AG's response, findings, or verification results.
  - **Version Changes**: bulleted list of added/modified/deleted files and behavioral changes (or note `"No file changes"` for analysis-only turns).
- Skip purely trivial read-only checks (e.g. `git status` alone).
