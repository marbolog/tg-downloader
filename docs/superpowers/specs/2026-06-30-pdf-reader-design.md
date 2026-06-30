# In-browser PDF reader for the web UI — Design

**Date:** 2026-06-30
**Status:** Approved (pending spec review)

## Goal

Let users read PDF files directly inside the existing web UI (`webui/`), instead
of only downloading them. Hard requirements from the requester:

1. **Mobile-first** — genuinely convenient on smartphones and tablets.
2. **Future-proof** — built so later evolutions (annotations, AI-on-page, EPUB,
   reading-position memory) plug in without a rewrite.
3. **Zoom** in and out of PDF pages.
4. **Read portions in isolation** — view a single magazine article on its own.
   v1 scope: manual region crop (works on scanned PDFs too).

## Key constraints that shaped the design

- **iOS Safari does not reliably render a PDF inside an `<iframe>`** (blank frame
  or forced download). The native-browser-viewer approach is therefore rejected:
  it fails exactly on the mobile target. We render with **pdf.js** to `<canvas>`,
  which is consistent across mobile browsers.
- **A meaningful share of the library is scanned image PDFs** with no text layer
  (confirmed by `lang_filter.py`'s filename-heuristic fallback and the search
  heal's "textless" counter). Any isolation feature built on text extraction
  would silently fail on these. v1 isolation therefore operates on **rendered
  pixels** (manual crop), which works on scanned and text PDFs alike.
- **Project ethos:** vanilla JS, no build step, minimal dependencies. pdf.js is
  vendored as static assets — not an npm/pip dependency, no bundler.

## Approach (selected: "A")

Vendor the pdf.js **library** (not Mozilla's full viewer) and write a focused,
app-owned reader module. This is the most mobile-friendly *and* most extensible
option: ~one screen of code we control, with a clean `openReader()` seam.

Rejected alternatives:
- **Iframe Mozilla's full `viewer.html`** — less code now, but it's their UI in an
  iframe, so app-specific future features (DB-synced annotations, Ask-AI-on-page)
  are awkward to bolt on. Weaker on the "future evolution" axis.
- **Native browser iframe** — rejected (breaks on iOS, see constraints).

## Architecture & boundaries

One new backend endpoint, one self-contained frontend module, vendored pdf.js.
**No new Python dependencies, no build step, no Dockerfile change** (the existing
`COPY webui/static/ ./static/` ships the vendored assets automatically).

### Backend — `GET /api/pdf/{id}` (in `webui/app.py`)

- Serves the raw PDF **inline** (`Content-Disposition: inline`) via Starlette's
  `FileResponse`, which already honors HTTP `Range` requests — pdf.js streams big
  magazines progressively instead of fetching the whole file up front.
- Guarded exactly like the existing `/api/download/{id}`: look up the row via
  `db.get_media`; return 404 if the row is missing, the file is not on disk, or
  `ext != 'pdf'`.
- No new SQL; reuses `Database.get_media`.

### Frontend

- **Vendored pdf.js** under `webui/static/vendor/pdfjs/`: `pdf.min.mjs` +
  `pdf.worker.min.mjs`, the **legacy ESM build** (broadest mobile/older-iOS
  support). Pinned to a specific released version (recorded in `CLAUDE.md` and a
  comment at the top of `reader.js`).
- **`webui/static/reader.js`** — new ES module. The whole reader lives here,
  isolated from the grid behind a single entry point:
  `openReader(mediaId, { page } = {})`. Exposed as `window.openReader` so the
  existing classic-script `index.html` can call it.
- **`webui/static/reader.css`** — reader styles, kept out of the already-large
  `index.html`.
- **`webui/static/index.html`** — minimal changes only: a **Read** button on PDF
  cards (in `makeCard`), and a `<script type="module" src="reader.js">` include.

## The reader module (`reader.js`)

- **Full-screen overlay** created lazily on first open, reused thereafter. Slim
  toolbar: close ✕, page indicator (`12 / 80`), zoom controls (`−` `＋` `Fit`),
  and a **Crop** toggle.
- **Continuous vertical scroll.** Each page is a placeholder `<div>` sized to the
  page's aspect ratio at the current scale. An `IntersectionObserver` renders a
  page to `<canvas>` as it nears the viewport and releases canvases that scroll
  far away (a render window of a few pages around the viewport), so large scanned
  magazines never exhaust phone memory.
- **Zoom.** A `scale` state. Default **fit-width** (container width ÷ page width
  at scale 1). `−`/`＋` adjust scale and re-render visible pages; canvases render
  at `devicePixelRatio × scale` to stay crisp. **Pinch-to-zoom** on touch: live
  CSS transform during the gesture, then a debounced re-render at the settled
  scale (standard pdf.js mobile pattern).
- **Page jump.** `openReader(id, { page })` and `#page=N` URL fragments scroll the
  target page placeholder into view.
- **Error state.** Some library PDFs are malformed (per recent git history). If
  `getDocument` / page render rejects, the overlay shows an error message with a
  **Download instead** fallback (link to `/api/download/{id}`) rather than a blank
  screen.

## Isolated article reading (v1: manual crop)

- **Crop** toolbar toggle. While active, the user drags a rectangle over a page; a
  selection overlay tracks the drag (pointer events, works with touch and mouse).
- On release, the selected region is re-rendered at an **elevated scale** into a
  focused sub-view that fills the screen and is itself scroll/pinch-zoomable.
  Implementation: render the page to an offscreen canvas at high scale, copy the
  selected sub-rectangle into the focused-view canvas. Because it operates on
  rendered pixels, it works identically on scanned and text PDFs.
- The focused sub-view is the seam where future "Ask AI about this article" would
  attach — **out of scope for v1.**

## Data flow

```
PDF card → [Read] → openReader(id)
  → pdfjs.getDocument('/api/pdf/' + id)   (Range-streamed)
  → lazy page render on scroll (IntersectionObserver)
  → user: zoom (buttons / pinch) · jump (#page=N) · crop → focused sub-view
```

Search tie-in (in scope, low cost): the existing search-result **View** action
will, for a PDF hit carrying a page number, call `openReader(id, { page })` so the
reader opens at the matched page. The jump capability exists anyway for `#page=N`.

## Testing & verification

- **Backend (committed tests):** FastAPI `TestClient` against `/api/pdf/{id}`:
  - 200 + `Content-Disposition: inline` + `application/pdf` for a real, tiny PDF
    generated in-test with PyMuPDF (`fitz`).
  - 404 for an unknown id.
  - 404 for a row whose `ext` is not `pdf`.
- **Frontend (verification, not committed tests — no JS harness in the project):**
  Playwright at **desktop and mobile viewports** — open reader, confirm pages
  render on scroll, zoom in/out, perform a crop — with screenshots as evidence.
- **Docker:** confirmed no change needed; `webui/Dockerfile` copies `static/`
  wholesale.

## Files touched

| File | Change |
|---|---|
| `webui/app.py` | + `GET /api/pdf/{id}` (inline `FileResponse`) |
| `webui/static/vendor/pdfjs/pdf.min.mjs` | vendored (new) |
| `webui/static/vendor/pdfjs/pdf.worker.min.mjs` | vendored (new) |
| `webui/static/reader.js` | new reader module |
| `webui/static/reader.css` | new reader styles |
| `webui/static/index.html` | Read button on PDF cards + module include |
| `tests/test_webui_pdf.py` | new backend endpoint tests |
| `CLAUDE.md` | document the reader, the endpoint, vendored pdf.js version |

## Out of scope (explicitly, for v1)

- EPUB reading (keep the existing Download button for non-PDF).
- Text-reflow article view and AI-assisted article extraction (the crop sub-view
  is designed as the seam for these later).
- Annotations, bookmarks, reading-position persistence.
