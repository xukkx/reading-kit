---
name: organize-vault
description: Analyze the Obsidian vault's knowledge graph — find orphan notes, suggest and add missing links, clean up tags, update MOCs. Use when the user wants to organize, clean up, or curate the vault or graph.
---

# Vault Organization & Graph Curation

The Obsidian vault is the `vault/` folder at this project's root.

## Process

1. **Map the graph**: glob `vault/**/*.md` (skip `_templates/`, `.obsidian/`); grep `\[\[([^\]|#]+)` for links; build the in/out picture.
2. **Diagnose**: orphans (no links either way) · dead links · unlinked mentions (a note's title/alias as plain text elsewhere) · tag drift (near-duplicates) · MOC coverage gaps · inbox backlog · **unfiled topic-folder notes** — files sitting directly in `10-Notes/`, `20-Literature/`, or `Books/` instead of one level down in a topic folder (these silently land in the RAG index's "default" partition instead of their real topic; see `reading-system`).
3. **Propose, then apply.** Small obvious fixes: apply directly. Sweeping changes (mass renames, moves, merges, deletions): show the plan, get confirmation. Never delete note content without explicit approval.
4. **Apply**: convert genuine unlinked mentions to `[[links]]`; add Related links with reasons; update MOCs and Home; renames must update every incoming link.
5. **Report** before/after stats and what changed.

## Rules

- A useful graph, not a maximally dense one — don't force links.
- Organizing means connecting and filing, not rewriting the user's words.
