# Search Results UX Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group FTS5 search results by document, show a result count bar, and add a View button that navigates the grid to the matching file card.

**Architecture:** Small backend change to surface `channel_identifier` in the search API response; all remaining changes are in the single-page frontend (`index.html`). No test framework exists for the JS — verification is done manually via the running webui.

**Tech Stack:** Python/FastAPI (`webui/app.py`), vanilla JS (`webui/static/index.html`), SQLite FTS5.

---

### Task 1: Add `channel_identifier` to `/api/search` response

**Files:**
- Modify: `webui/app.py` — `fts_search` function (two SELECT statements and the chunk dict)

The FTS5 table stores `channel_identifier` as an UNINDEXED column but the current SELECT omits it. The frontend grouping needs it to display the channel badge on each document group.

- [ ] **Step 1: Edit both SELECT statements in `fts_search`**

In `webui/app.py`, the `fts_search` function (decorated `@app.get("/api/search")`) has two SELECT branches (with/without channel filter). In **both**, change the SELECT to:

```sql
SELECT media_id, filename, page, chapter, channel_identifier,
       snippet(search_fts, 0, '<<', '>>', '...', 20) AS text
FROM search_fts
WHERE ...
ORDER BY rank LIMIT ?
```

Then add `"channel_identifier": r["channel_identifier"]` to the explicit chunk dict that is built from both branches:

```python
chunks = [
    {
        "media_id": int(r["media_id"]),
        "filename": r["filename"],
        "page": r["page"],
        "chapter": r["chapter"],
        "channel_identifier": r["channel_identifier"],
        "text": r["text"],
    }
    for r in rows
]
```

- [ ] **Step 2: Verify the API returns `channel_identifier`**

Restart the webui service and curl the endpoint (replace QUERY with a word you know is indexed):

```bash
curl -s "http://localhost:8090/api/search?q=QUERY" | python3 -m json.tool | grep channel_identifier
```

Expected: one `"channel_identifier": "@somechannel"` line per chunk.

- [ ] **Step 3: Commit**

```bash
git add webui/app.py
git commit -m "feat(webui): add channel_identifier to /api/search chunk response"
```

---

### Task 2: Add card `id` attribute to grid cards

**Files:**
- Modify: `webui/static/index.html` — `makeCard` function (~line 522)

The "View" button will use `document.getElementById('card-' + mediaId)` to find the card. Cards need a stable `id`.

- [ ] **Step 1: Add `id` to the card element in `makeCard`**

In `makeCard(f)`, immediately after the existing line `card.dataset.id = f.id;`, add:

```js
card.id = 'card-' + f.id;
```

- [ ] **Step 2: Verify in browser**

Open the webui, open DevTools, find any `.card` element in the Elements panel. Confirm it has an `id` attribute like `id="card-123"`.

- [ ] **Step 3: Commit**

```bash
git add webui/static/index.html
git commit -m "feat(webui): add id attribute to grid cards for View navigation"
```

---

### Task 3: Add highlight animation CSS + `viewCard` function + renderGrid hook

**Files:**
- Modify: `webui/static/index.html` — CSS block, state object `S`, new `viewCard` function, `renderGrid` function

- [ ] **Step 1: Add keyframe and highlight class to the `<style>` block**

Append to the `<style>` block before `</style>`:

```css
@keyframes card-flash {
  0%   { box-shadow: 0 0 0 3px var(--accent); background: var(--accent-light); }
  100% { box-shadow: none; background: var(--surface); }
}
.card-highlight { animation: card-flash 1.5s ease-out forwards; }
```

- [ ] **Step 2: Add `highlightId` to the state object `S`**

Find the line that starts `const S = { files: [], total: 0, page: 1` and add `highlightId: null` to the end of the object literal:

```js
const S = { files: [], total: 0, page: 1, perPage: 60, channel: '', language: '', hideDupes: true, selected: new Set(), highlightId: null };
```

- [ ] **Step 3: Add the `viewCard` function**

Add this function in the script block, near the other navigation helpers (e.g. after `toggleDupes`):

```js
function viewCard(mediaId) {
  document.getElementById('ch-filter').value = '';
  document.getElementById('lang-filter').value = '';
  S.channel = ''; S.language = ''; S.page = 1; S.highlightId = mediaId;
  load();
}
```

- [ ] **Step 4: Add `S.highlightId = null` to the empty-grid early-return in `renderGrid`**

In `renderGrid`, find the `if (!S.files.length)` block. Before the `return` statement, add:

```js
S.highlightId = null;
```

- [ ] **Step 5: Wire highlight check after the card-rendering loop in `renderGrid`**

Find the line `S.files.forEach(f => grid.appendChild(makeCard(f)));` near the end of `renderGrid`. Immediately after it, add:

```js
if (S.highlightId != null) {
  const target = document.getElementById('card-' + S.highlightId);
  if (target) {
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.classList.add('card-highlight');
    target.addEventListener('animationend', () => target.classList.remove('card-highlight'), { once: true });
  } else {
    toast('File found — navigate to the right page to see it');
  }
  S.highlightId = null;
}
```

- [ ] **Step 6: Manual test**

Open the webui. In browser DevTools console, call `viewCard(ID)` using a real file id visible in the grid. Confirm:
- Active filters clear
- Grid reloads to page 1
- The target card scrolls into view and flashes yellow for ~1.5 s

- [ ] **Step 7: Commit**

```bash
git add webui/static/index.html
git commit -m "feat(webui): add viewCard highlight navigation for search results"
```

---

### Task 4: Add grouped result CSS

**Files:**
- Modify: `webui/static/index.html` — CSS `<style>` block (RAG search panel section)

- [ ] **Step 1: Append group CSS to the `<style>` block**

Add the following after the existing `.rag-answer-text` rule, before `</style>`:

```css
.rag-count {
  font-size: .78rem; color: var(--muted); padding: 2px 0 6px;
}
.rag-group {
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
}
.rag-group + .rag-group { margin-top: 8px; }
.rag-group-header {
  display: flex; align-items: center; gap: 8px;
  background: var(--surface); padding: 8px 12px;
}
.rag-group-header-name {
  font-size: .8rem; font-weight: 600; color: var(--text);
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rag-group-header-channel {
  font-size: .72rem; color: var(--muted); flex-shrink: 0;
}
.rag-group-header-view { font-size: .72rem; padding: 3px 8px; flex-shrink: 0; }
.rag-group .rag-chunk {
  border: none; border-radius: 0;
  border-top: 1px solid var(--border);
}
```

- [ ] **Step 2: Commit**

```bash
git add webui/static/index.html
git commit -m "feat(webui): add CSS for grouped search result layout"
```

---

### Task 5: Rewrite `_renderRagResults` with document grouping and count bar

**Files:**
- Modify: `webui/static/index.html` — `_renderRagResults` function (~lines 383–420)

- [ ] **Step 1: Replace the entire `_renderRagResults` function**

Find the function definition `function _renderRagResults(chunks, container, answer) {` and replace the whole function body with the following. The function signature stays the same; only the body changes:

```js
function _renderRagResults(chunks, container, answer) {
  container.replaceChildren();
  container.style.display = 'block';

  // AI answer box (unchanged)
  if (answer) {
    const box = document.createElement('div');
    box.className = 'rag-answer';
    const lbl = document.createElement('div');
    lbl.className = 'rag-answer-label';
    lbl.textContent = 'Answer';
    const txt = document.createElement('div');
    txt.className = 'rag-answer-text';
    txt.textContent = answer;
    box.appendChild(lbl);
    box.appendChild(txt);
    container.appendChild(box);
  }

  // Count bar
  const docCount = new Set(chunks.map(c => c.media_id)).size;
  const countBar = document.createElement('p');
  countBar.className = 'rag-count';
  countBar.textContent =
    docCount + ' document' + (docCount !== 1 ? 's' : '') +
    ' · ' +
    chunks.length + ' passage' + (chunks.length !== 1 ? 's' : '') +
    ' — sorted by relevance';
  container.appendChild(countBar);

  if (!chunks.length) {
    const p = document.createElement('p');
    p.style.cssText = 'color:var(--muted);font-size:.85rem;padding:4px 0';
    p.textContent = 'No matching content found.';
    container.appendChild(p);
    return;
  }

  // Group chunks by media_id, preserving first-occurrence (relevance) order
  const groups = new Map();
  for (const chunk of chunks) {
    if (!groups.has(chunk.media_id)) {
      groups.set(chunk.media_id, {
        mediaId: chunk.media_id,
        filename: chunk.filename,
        channel: chunk.channel_identifier || '',
        chunks: [],
      });
    }
    groups.get(chunk.media_id).chunks.push(chunk);
  }

  for (const group of groups.values()) {
    const groupDiv = document.createElement('div');
    groupDiv.className = 'rag-group';

    // Header row
    const header = document.createElement('div');
    header.className = 'rag-group-header';

    const nameEl = document.createElement('span');
    nameEl.className = 'rag-group-header-name';
    nameEl.title = group.filename;
    nameEl.textContent = group.filename;
    header.appendChild(nameEl);

    if (group.channel) {
      const chanEl = document.createElement('span');
      chanEl.className = 'rag-group-header-channel';
      chanEl.textContent = group.channel;
      header.appendChild(chanEl);
    }

    const viewBtn = document.createElement('button');
    viewBtn.className = 'btn rag-group-header-view';
    viewBtn.textContent = '↗ View';
    viewBtn.onclick = () => viewCard(group.mediaId);
    header.appendChild(viewBtn);
    groupDiv.appendChild(header);

    // Passages
    for (const chunk of group.chunks) {
      const div = document.createElement('div');
      div.className = 'rag-chunk';
      const loc = chunk.page != null ? 'p. ' + chunk.page : (chunk.chapter || '');
      if (loc) {
        const src = document.createElement('div');
        src.className = 'rag-chunk-source';
        src.textContent = loc;
        div.appendChild(src);
      }
      const txt = document.createElement('div');
      txt.className = 'rag-chunk-text';
      renderSnippet(txt, chunk.text || '');
      div.appendChild(txt);
      groupDiv.appendChild(div);
    }

    container.appendChild(groupDiv);
  }
}
```

- [ ] **Step 2: Verify search results display**

Open the webui, type a query and click **Search**. Confirm:
- Count bar shows: `"N documents · M passages — sorted by relevance"`
- Results are grouped by document (each group: header with filename, optional channel, View button; then passage rows)
- Highlighted keywords appear with `<mark>` styling inside passages
- Zero-results query shows count `"0 documents · 0 passages"` and the no-results message

- [ ] **Step 3: Verify View button**

Click **View** on a search result group. Confirm:
- Channel and language filters reset to "All"
- Grid reloads to page 1
- The matching card flashes yellow and scrolls into view

- [ ] **Step 4: Verify Ask AI still works** (if `ANTHROPIC_API_KEY` is configured)

Type a query and click **Ask AI**. Confirm the answer box renders above the grouped chunks.

- [ ] **Step 5: Final commit**

```bash
git add webui/static/index.html
git commit -m "feat(webui): group search results by document with count bar and View button"
```
