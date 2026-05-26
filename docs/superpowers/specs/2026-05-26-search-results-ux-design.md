# Search Results UX Improvements

**Date:** 2026-05-26
**Scope:** `webui/static/index.html` only — no backend changes

## Problem

The search results panel has three UX gaps:

1. No indication of how many results were returned or how they are ordered
2. Chunks from the same document repeat the filename without grouping — hard to scan
3. No way to navigate from a search result to the document's card in the main grid

## Design

### 1. Count bar

A single line above all results:

```
5 documents · 12 passages — sorted by relevance
```

- "documents" = count of unique `media_id` values in the result set
- "passages" = total chunk count
- "sorted by relevance" is static text — BM25 rank order is always used
- Zero-results state: `"0 documents · 0 passages — sorted by relevance"` followed by the existing `"No matching content found."` message

### 2. Grouped result layout

Chunks are grouped by `media_id`, preserving first-occurrence order (= document relevance rank).

Each group renders as:

```
┌─────────────────────────────────────────────┐
│ [filename]           [#channel]   [↗ View]  │  ← .rag-group-header
├─────────────────────────────────────────────┤
│  p. 12 — ...matched text with highlights... │  ← .rag-chunk
│  p. 34 — ...another passage...              │  ← .rag-chunk
└─────────────────────────────────────────────┘
```

- `.rag-group` — wrapper div, bottom border between groups
- `.rag-group-header` — flex row: filename (bold, truncated), channel identifier (muted chip), View button (small, accent-colored)
- `.rag-chunk` — existing style, reused unchanged; page/chapter label moves to the chunk row prefix

The Ask AI answer box is unaffected — it renders above all groups, exactly as today.

### 3. "↗ View" button

Clicking `↗ View` on a group header navigates the main grid to the corresponding file card.

**Implementation:**

1. `renderGrid()` adds `id="card-{media_id}"` to each card's root element
2. On click:
   - Clear active filters (`S.channel = ''`, `S.language = ''`)
   - Reset to page 1 (`S.page = 1`)
   - Set `S.highlightId = media_id`
   - Call `load()`
3. At the end of `renderGrid()`, if `S.highlightId` is set:
   - Find `document.getElementById('card-' + S.highlightId)`
   - If found: `scrollIntoView({ behavior: 'smooth', block: 'center' })` + add `.card-highlight` CSS class (yellow background fading to normal over 1.5s via a keyframe animation), then clear `S.highlightId`
   - If not found (file is on a later page): show toast `"File found — navigate to the right page to see it"`, clear `S.highlightId`

**Side effect:** if a channel or language filter was active when the search ran, View clears it so the file is visible. This is intentional.

## Files changed

| File | Change |
|---|---|
| `webui/static/index.html` | Group logic in `_renderRagResults`; count bar; `.rag-group` + `.rag-group-header` CSS; `id` on card root in `renderGrid`; highlight state + animation; View button handler |

No changes to `webui/app.py`, `db.py`, `search/`, or any other file.

## Out of scope

- Direct download button on search results (navigation to card is sufficient)
- Multi-page scan to locate a file when it is not on page 1
- Meilisearch or any other search backend change
