---
name: note
description: Turn raw input (thoughts, chat logs, article text, ideas) into well-structured atomic Obsidian notes with wikilinks, tags, and MOC updates. Use whenever the user wants to capture, save, or organize knowledge into the vault.
---

# Smart Note Creation & Linking

The Obsidian vault is the `vault/` folder at this project's root.

## Process

1. **Understand the input.** Split multiple distinct ideas — one idea per note (Zettelkasten).
2. **Find link candidates first.** Glob `vault/**/*.md` for related filenames; Grep bodies and `aliases:` for the topic's keywords (search the user's languages). Every new note should link to at least one existing note when anything related exists.
3. **Write each note** to `vault/10-Notes/<Title>.md` (`00-Inbox/` for rough captures):
   - Title = a claim or concept phrase. Filename = title; avoid `\ / : * ? " < > | [ ] # ^`.
   - Frontmatter: `created` (today), `tags` (2–4, lowercase-kebab, reuse existing — grep `tags:` first), `aliases` (synonyms/translations).
   - Body: restate the idea clearly, weave `[[wikilinks]]` inline, "Related" section with reasons, "Source" section.
4. **Update the graph**: add the note to its topic MOC in `vault/30-MOCs/` (create the MOC if a topic has ≥5 notes); new MOCs get linked from `vault/Home.md`.
5. **Report** notes created and links added.

## Rules

- Never overwrite an existing note with the same title — extend it or pick a more specific title.
- Keep notes under ~300 words; longer means it's several notes.
- Links are the organization; folders are just storage.
