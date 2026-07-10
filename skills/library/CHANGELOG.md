# Changelog — library

- 2026-07-10 (Reading_Hub, field failure): first live handoff hit an empty Inbox and the skill had no defined move — it asked the user to place files, then rummaged Downloads/H: ad hoc. Fix: source registry in hub.json + import_batch.py --scan/--pull (title AND byte-size dedup — a renamed re-import was only caught by size), and an explicit decision tree with the prime directive "the user never moves files; ask knowledge questions, never operations."
- 2026-07-10 (reading-kit): created together with scripts/library.py and the Reading_Hub mega-directory — one session manages all registered projects; inventory renders as Artifact dashboard, SendUserFile, or plain markdown depending on channel. Handles both vault shapes (standard 正文.md and legacy flat 卷N.md).
