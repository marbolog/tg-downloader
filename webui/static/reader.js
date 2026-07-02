// In-browser PDF reader, built on vendored pdf.js (legacy ESM build).
// Single entry point: window.openReader(mediaId, { page }).
// This is the seam every future evolution (annotations, AI-on-page, EPUB) plugs
// into — keep the public surface to openReader().
//
// pdf.js version is pinned; see webui/static/vendor/pdfjs/VERSION.
import * as pdfjsLib from '/vendor/pdfjs/pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/pdf.worker.min.mjs';

const DPR = Math.min(window.devicePixelRatio || 1, 2);  // cap to bound canvas memory
const MIN_SCALE = 0.25;
const MAX_SCALE = 6;
const CROP_TARGET_PX = 1600;   // render the cropped region to ~this width for sharpness

// Per-open state. Rebuilt on each openReader() call.
const R = {
  pdf: null,
  scale: 1,
  baseWidths: [],   // page width in CSS px at scale 1, 1-indexed (index 0 unused)
  baseHeights: [],
  rendered: new Map(),  // pageNum -> canvas (currently mounted)
  observer: null,
  el: null,         // overlay root DOM (created once, reused)
  loadingId: 0,     // guards against races when reopening quickly
};

function el(tag, cls, parent) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (parent) parent.appendChild(n);
  return n;
}

function ensureOverlay() {
  if (R.el) return R.el;

  const root = el('div', 'rdr');
  root.hidden = true;

  const bar = el('div', 'rdr-bar', root);
  const close = el('button', 'rdr-close', bar); close.textContent = '✕'; close.title = 'Close';
  const title = el('span', 'rdr-title', bar);
  const pageind = el('span', 'rdr-pageind', bar); pageind.textContent = '– / –';
  const zoom = el('div', 'rdr-zoom', bar);
  const zout = el('button', null, zoom); zout.textContent = '−'; zout.title = 'Zoom out';
  const zfit = el('button', null, zoom); zfit.textContent = 'Fit'; zfit.title = 'Fit width';
  const zin = el('button', null, zoom); zin.textContent = '＋'; zin.title = 'Zoom in';
  const cropBtn = el('button', 'rdr-crop', bar); cropBtn.textContent = 'Crop'; cropBtn.title = 'Isolate an article';

  const scroll = el('div', 'rdr-scroll', root);
  const pages = el('div', 'rdr-pages', scroll);

  const error = el('div', 'rdr-error', root); error.hidden = true;

  const cropView = el('div', 'rdr-crop-view', root); cropView.hidden = true;
  const cropBar = el('div', 'rdr-crop-bar', cropView);
  const cropClose = el('button', null, cropBar); cropClose.textContent = '✕ Back'; cropClose.className = '';
  cropClose.style.cssText = 'font:inherit;color:#f1f1f1;background:#333;border:1px solid #4a4a4a;border-radius:8px;padding:8px 12px;cursor:pointer;';
  const cropScroll = el('div', 'rdr-crop-scroll', cropView);

  document.body.appendChild(root);

  // Wire controls
  close.onclick = closeReader;
  zin.onclick = () => setScale(R.scale * 1.25);
  zout.onclick = () => setScale(R.scale / 1.25);
  zfit.onclick = () => setScale(fitWidthScale());
  cropBtn.onclick = () => {
    const on = scroll.classList.toggle('cropping');
    cropBtn.classList.toggle('on', on);
  };
  cropClose.onclick = () => { cropView.hidden = true; cropScroll.innerHTML = ''; };

  // Close on Escape / browser back
  document.addEventListener('keydown', (e) => {
    if (root.hidden) return;
    if (e.key === 'Escape') { if (!cropView.hidden) { cropView.hidden = true; cropScroll.innerHTML = ''; } else closeReader(); }
  });

  // Track the current page in the toolbar as the user scrolls.
  scroll.addEventListener('scroll', () => updatePageIndicator(), { passive: true });

  setupPinch(scroll, pages);
  setupCrop(pages, scroll, cropView, cropScroll);

  R.el = root;
  R.el._refs = { title, pageind, scroll, pages, error, cropBtn, cropView, cropScroll };
  return root;
}

async function openReader(mediaId, opts = {}) {
  const root = ensureOverlay();
  const refs = root._refs;
  const myLoad = ++R.loadingId;

  // Reset state
  resetState(refs);
  root.hidden = false;
  document.body.classList.add('rdr-open');
  refs.error.hidden = true;
  refs.title.textContent = 'Loading…';

  try {
    const task = pdfjsLib.getDocument({ url: `/api/pdf/${mediaId}` });
    const pdf = await task.promise;
    if (myLoad !== R.loadingId) { pdf.destroy(); return; }  // superseded by a newer open
    R.pdf = pdf;

    // Measure every page at scale 1 so placeholders have correct aspect ratios.
    R.baseWidths = [0];
    R.baseHeights = [0];
    for (let n = 1; n <= pdf.numPages; n++) {
      const page = await pdf.getPage(n);
      const vp = page.getViewport({ scale: 1 });
      R.baseWidths[n] = vp.width;
      R.baseHeights[n] = vp.height;
    }
    if (myLoad !== R.loadingId) return;

    refs.title.textContent = opts.filename || `Document (${pdf.numPages} pages)`;
    R.scale = fitWidthScale();
    buildPages(refs.pages);
    observePages(refs.scroll, refs.pages);

    if (opts.page && opts.page >= 1 && opts.page <= pdf.numPages) {
      // Defer so placeholders have layout before scrolling.
      requestAnimationFrame(() => scrollToPage(refs.scroll, refs.pages, opts.page));
    }
    updatePageIndicator();
  } catch (err) {
    if (myLoad !== R.loadingId) return;
    showError(refs, mediaId, err);
  }
}

function resetState(refs) {
  if (R.observer) { R.observer.disconnect(); R.observer = null; }
  if (R.pdf) { try { R.pdf.destroy(); } catch (_) {} R.pdf = null; }
  R.rendered.clear();
  refs.pages.innerHTML = '';
  refs.pages.style.transform = '';
  refs.scroll.classList.remove('cropping');
  refs.cropBtn.classList.remove('on');
  refs.cropView.hidden = true;
  refs.cropScroll.innerHTML = '';
}

function closeReader() {
  const root = R.el;
  if (!root) return;
  R.loadingId++;  // cancel any in-flight load
  root.hidden = true;
  document.body.classList.remove('rdr-open');
  if (R.observer) { R.observer.disconnect(); R.observer = null; }
  if (R.pdf) { try { R.pdf.destroy(); } catch (_) {} R.pdf = null; }
  R.rendered.clear();
  root._refs.pages.innerHTML = '';
}

function fitWidthScale() {
  const refs = R.el._refs;
  const avail = refs.scroll.clientWidth - 8;   // small breathing room
  const w1 = R.baseWidths[1] || 600;
  return clamp(avail / w1, MIN_SCALE, MAX_SCALE);
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function buildPages(pagesEl) {
  pagesEl.innerHTML = '';
  for (let n = 1; n <= R.pdf.numPages; n++) {
    const ph = el('div', 'rdr-page', pagesEl);
    ph.dataset.n = n;
    sizePlaceholder(ph, n);
  }
}

function sizePlaceholder(ph, n) {
  ph.style.width = (R.baseWidths[n] * R.scale) + 'px';
  ph.style.height = (R.baseHeights[n] * R.scale) + 'px';
}

function observePages(scroll, pagesEl) {
  if (R.observer) R.observer.disconnect();
  // Render a window of pages around the viewport; release the rest.
  R.observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const n = +e.target.dataset.n;
      if (e.isIntersecting) renderPage(n, e.target);
      else unrenderPage(n, e.target);
    }
  }, { root: scroll, rootMargin: '600px 0px' });

  for (const ph of pagesEl.children) R.observer.observe(ph);
}

async function renderPage(n, ph) {
  if (R.rendered.has(n) || !R.pdf) return;
  const page = await R.pdf.getPage(n);
  const vp = page.getViewport({ scale: R.scale });
  const canvas = document.createElement('canvas');
  canvas.width = Math.floor(vp.width * DPR);
  canvas.height = Math.floor(vp.height * DPR);
  const ctx = canvas.getContext('2d');
  R.rendered.set(n, canvas);
  ph.appendChild(canvas);
  try {
    await page.render({
      canvasContext: ctx,
      viewport: vp,
      transform: DPR !== 1 ? [DPR, 0, 0, DPR, 0, 0] : null,
    }).promise;
  } catch (_) {
    // Render cancelled (e.g. scrolled away / rescaled) — drop it.
    if (R.rendered.get(n) === canvas) { R.rendered.delete(n); canvas.remove(); }
  }
}

function unrenderPage(n, ph) {
  const c = R.rendered.get(n);
  if (c) { c.remove(); R.rendered.delete(n); }
}

function setScale(next) {
  next = clamp(next, MIN_SCALE, MAX_SCALE);
  if (Math.abs(next - R.scale) < 0.001) return;
  const refs = R.el._refs;

  // Keep the current top page stable across the rescale.
  const anchor = currentPage(refs.scroll, refs.pages);
  R.scale = next;

  // Drop all rendered canvases and resize placeholders, then re-render visible.
  for (const [n, c] of R.rendered) c.remove();
  R.rendered.clear();
  for (const ph of refs.pages.children) sizePlaceholder(ph, +ph.dataset.n);
  refs.pages.style.transform = '';

  observePages(refs.scroll, refs.pages);
  if (anchor) scrollToPage(refs.scroll, refs.pages, anchor);
  updatePageIndicator();
}

function currentPage(scroll, pagesEl) {
  const mid = scroll.scrollTop + scroll.clientHeight / 2;
  let acc = parseFloat(getComputedStyle(pagesEl).paddingTop) || 0;
  for (const ph of pagesEl.children) {
    const h = ph.offsetHeight + 12;
    if (acc + h >= scroll.scrollTop && acc <= mid + h) {
      if (acc + h >= scroll.scrollTop) return +ph.dataset.n;
    }
    acc += h;
  }
  return 1;
}

function scrollToPage(scroll, pagesEl, n) {
  const ph = pagesEl.querySelector(`.rdr-page[data-n="${n}"]`);
  if (ph) scroll.scrollTop = ph.offsetTop - 12;
}

function updatePageIndicator() {
  if (!R.pdf || !R.el) return;
  const refs = R.el._refs;
  const n = currentPage(refs.scroll, refs.pages);
  refs.pageind.textContent = `${n} / ${R.pdf.numPages}`;
}

function showError(refs, mediaId, err) {
  console.warn('PDF reader error:', err);
  refs.error.innerHTML = '';
  const p = el('p', null, refs.error);
  p.textContent = 'This PDF could not be displayed.';
  const a = el('a', null, refs.error);
  a.href = `/api/download/${mediaId}`;
  a.textContent = 'Download it instead';
  a.setAttribute('download', '');
  refs.error.hidden = false;
}

/* ── Pinch-to-zoom ── */
function setupPinch(scroll, pages) {
  const pts = new Map();
  let startDist = 0;
  let startScale = 1;

  scroll.addEventListener('touchstart', (e) => {
    if (e.touches.length === 2) {
      startDist = dist(e.touches[0], e.touches[1]);
      startScale = R.scale;
    }
  }, { passive: true });

  scroll.addEventListener('touchmove', (e) => {
    if (e.touches.length === 2 && startDist) {
      e.preventDefault();
      const ratio = dist(e.touches[0], e.touches[1]) / startDist;
      // Live visual feedback via transform; committed on touchend.
      pages.style.transform = `scale(${clamp(ratio, 0.5, 2)})`;
      pages._pendingScale = clamp(startScale * ratio, MIN_SCALE, MAX_SCALE);
    }
  }, { passive: false });

  scroll.addEventListener('touchend', (e) => {
    if (startDist && e.touches.length < 2) {
      startDist = 0;
      if (pages._pendingScale) { const s = pages._pendingScale; pages._pendingScale = 0; setScale(s); }
    }
  });

  function dist(a, b) { return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
}

/* ── Manual region crop ("isolate an article") ── */
function setupCrop(pages, scroll, cropView, cropScroll) {
  let active = null;   // { ph, n, startX, startY, sel }

  const down = (ev) => {
    if (!scroll.classList.contains('cropping')) return;
    const ph = ev.target.closest('.rdr-page');
    if (!ph) return;
    ev.preventDefault();
    const rect = ph.getBoundingClientRect();
    const sel = el('div', 'rdr-sel', ph);
    active = { ph, n: +ph.dataset.n, x0: ev.clientX - rect.left, y0: ev.clientY - rect.top, sel, rect };
  };

  const move = (ev) => {
    if (!active) return;
    ev.preventDefault();
    const rect = active.ph.getBoundingClientRect();
    const x1 = clamp(ev.clientX - rect.left, 0, rect.width);
    const y1 = clamp(ev.clientY - rect.top, 0, rect.height);
    const x = Math.min(active.x0, x1), y = Math.min(active.y0, y1);
    const w = Math.abs(x1 - active.x0), h = Math.abs(y1 - active.y0);
    Object.assign(active.sel.style, { left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px' });
    active._frac = { x: x / rect.width, y: y / rect.height, w: w / rect.width, h: h / rect.height };
  };

  const up = async (ev) => {
    if (!active) return;
    const a = active; active = null;
    a.sel.remove();
    if (!a._frac || a._frac.w < 0.02 || a._frac.h < 0.02) return;  // ignore tiny/accidental
    await renderCrop(a.n, a._frac, cropView, cropScroll);
    // leave crop mode after a successful selection
    scroll.classList.remove('cropping');
    R.el._refs.cropBtn.classList.remove('on');
  };

  // Pointer events cover mouse + touch uniformly.
  pages.addEventListener('pointerdown', down);
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

async function renderCrop(n, frac, cropView, cropScroll) {
  if (!R.pdf) return;
  const page = await R.pdf.getPage(n);
  // Choose a scale so the cropped width maps to ~CROP_TARGET_PX device pixels.
  const w1 = R.baseWidths[n];
  const scale = clamp(CROP_TARGET_PX / (w1 * frac.w), 0.5, 8);
  const vp = page.getViewport({ scale });
  const full = document.createElement('canvas');
  full.width = Math.floor(vp.width);
  full.height = Math.floor(vp.height);
  await page.render({ canvasContext: full.getContext('2d'), viewport: vp }).promise;

  const sx = Math.floor(frac.x * full.width);
  const sy = Math.floor(frac.y * full.height);
  const sw = Math.floor(frac.w * full.width);
  const sh = Math.floor(frac.h * full.height);
  const out = document.createElement('canvas');
  out.width = sw; out.height = sh;
  out.getContext('2d').drawImage(full, sx, sy, sw, sh, 0, 0, sw, sh);

  cropScroll.innerHTML = '';
  const img = new Image();
  img.src = out.toDataURL('image/png');
  cropScroll.appendChild(img);
  cropView.hidden = false;
}

// Re-fit on orientation change / resize so it stays comfortable on rotate.
let resizeT = 0;
window.addEventListener('resize', () => {
  if (!R.el || R.el.hidden || !R.pdf) return;
  clearTimeout(resizeT);
  resizeT = setTimeout(() => setScale(fitWidthScale()), 200);
});

window.openReader = openReader;
