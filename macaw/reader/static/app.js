/* Books Mini App: a library shelf and a reader, after Apple Books. Plain JS, no build step. */
(() => {
'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const icon = (id, cls = '') => `<svg class="${cls}"><use href="#i-${id}"/></svg>`;
const wait = ms => new Promise(r => setTimeout(r, ms));
const haptic = (kind = 'light') => { try { tg.HapticFeedback.impactOccurred(kind); } catch (e) { /* not in Telegram */ } };

const THEMES = {
  original: { name: 'Original', light: ['#ffffff', '#000000'], dark: ['#1a191b', '#e6e6e6'] },
  quiet: { name: 'Quiet', light: ['#4a4a4d', '#d2d2d4'], dark: ['#121212', '#8e8e93'] },
  paper: { name: 'Paper', light: ['#eeede8', '#1f1f1f'], dark: ['#232323', '#cbcbcb'] },
  bold: { name: 'Bold', light: ['#ffffff', '#000000'], dark: ['#000000', '#ffffff'], bold: true },
  calm: { name: 'Calm', light: ['#f3e9d6', '#4b3a28'], dark: ['#3a3226', '#e3d7c1'] },
  focus: { name: 'Focus', light: ['#fffcf2', '#141414'], dark: ['#1e1c17', '#ebe5d3'] },
};
const FONTS = {
  original: ['Original', 'Charter, "Iowan Old Style", Georgia, serif'],
  athelas: ['Athelas', 'Athelas, Georgia, serif'],
  charter: ['Charter', 'Charter, Georgia, serif'],
  georgia: ['Georgia', 'Georgia, serif'],
  iowan: ['Iowan', '"Iowan Old Style", Georgia, serif'],
  newyork: ['New York', '"New York", ui-serif, Georgia, serif'],
  palatino: ['Palatino', 'Palatino, "Palatino Linotype", "Book Antiqua", serif'],
  sf: ['San Francisco', '-apple-system, system-ui, Roboto, sans-serif'],
  seravek: ['Seravek', 'Seravek, "Gill Sans", system-ui, sans-serif'],
  times: ['Times New Roman', '"Times New Roman", Times, serif'],
};
const SIZES = [14, 15, 16, 17, 18, 19, 21, 23, 25, 28, 31, 35, 40];
const DEFAULTS = { theme: 'original', size: 21, font: 'original', spacing: 1.68, margins: 39, justify: false, bold: false, scroll: true, sort: 'recent', view: 'grid' };

let token = '';
let prefs = { ...DEFAULTS };
let books = [];
let tab = 'library';

/* ================= Telegram, look ================= */

function scheme() {
  return tg && tg.colorScheme === 'light' ? 'light' : 'dark';
}

function applyScheme() {
  document.documentElement.dataset.scheme = scheme();
  const bg = scheme() === 'light' ? '#ffffff' : '#000000';
  try {
    tg.setHeaderColor(R.open ? readerColors()[0] : bg);
    tg.setBackgroundColor(R.open ? readerColors()[0] : bg);
    if (tg.setBottomBarColor) tg.setBottomBarColor(bg);
  } catch (e) { /* older Telegram */ }
}

function setupTelegram() {
  if (!tg) return;
  tg.ready();
  tg.expand();
  try { tg.disableVerticalSwipes(); } catch (e) { /* older Telegram */ }
  tg.onEvent('themeChanged', () => { applyScheme(); if (R.open) applyReaderLook(true); });
  tg.BackButton.onClick(back);
}

/* ================= server ================= */

async function api(path, opts = {}) {
  const headers = { Authorization: 'Bearer ' + token, ...(opts.headers || {}) };
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch('api/' + path, { ...opts, headers });
  if (!res.ok) {
    let msg = 'Something went wrong.';
    try { const j = await res.json(); msg = j.error || msg; } catch (e) { try { msg = (await res.text()) || msg; } catch (e2) { /* keep */ } }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

const LINK_KEY = 'reader-token';
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* private mode */ } },
};

async function login() {
  const initData = tg && tg.initData;
  if (initData) {
    const res = await fetch('api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData }) });
    if (!res.ok) throw new Error((await res.text()) || 'Open the library from the bot in Telegram.');
    token = (await res.json()).token;
    return;
  }
  // In Safari or another browser: the personal link from /books carries the sign-in after #t=.
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (m) {
    store.set(LINK_KEY, decodeURIComponent(m[1]));
    history.replaceState(null, '', location.pathname + location.search);
  }
  token = store.get(LINK_KEY) || '';
  if (!token) throw new Error('Open the library from the bot in Telegram, or send /books to the bot and tap "Open in browser".');
  const res = await fetch('api/books', { headers: { Authorization: 'Bearer ' + token } });
  if (res.status === 401) {
    store.set(LINK_KEY, null);
    throw new Error('This link has expired. Send /books to the bot and tap "Open in browser" again.');
  }
}

let prefsTimer = 0;
function savePrefs() {
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => api('prefs', { method: 'PUT', json: prefs }).catch(() => {}), 600);
}

/* ================= small UI pieces ================= */

let toastTimer = 0;
function toast(text, ms = 2600) {
  const t = $('#toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

const stack = []; // open sheets, menus, alerts: the back button closes the top one

function syncBack() {
  if (!tg) return;
  if (stack.length || R.open) tg.BackButton.show(); else tg.BackButton.hide();
}

function back() {
  if (stack.length) stack[stack.length - 1].close();
  else if (R.menu) closeReaderMenu();
  else if (R.open) closeReader();
}

function layer(el, { scrim = true, onClose, clear = false } = {}) {
  const root = $('#layer');
  const s = scrim ? document.createElement('div') : null;
  if (s) { s.className = 'scrim'; if (clear) s.style.background = 'transparent'; root.appendChild(s); }
  root.appendChild(el);
  const entry = {
    el,
    close() {
      const i = stack.indexOf(entry);
      if (i < 0) return;
      stack.splice(i, 1);
      el.classList.remove('in');
      if (s) s.classList.remove('in');
      setTimeout(() => { el.remove(); if (s) s.remove(); }, 320);
      syncBack();
      if (onClose) onClose();
    },
  };
  if (s) s.addEventListener('click', () => entry.close());
  stack.push(entry);
  syncBack();
  requestAnimationFrame(() => requestAnimationFrame(() => { el.classList.add('in'); if (s) s.classList.add('in'); }));
  return entry;
}

function sheet(title, bodyHtml, { tall = false, onClose, head = '' } = {}) {
  const el = document.createElement('div');
  el.className = 'sheet' + (tall ? ' tall' : '');
  el.innerHTML = `<div class="sheet-head"><h3>${esc(title)}</h3>${head}<button class="round" data-x aria-label="Close">${icon('x')}</button></div><div class="sheet-body">${bodyHtml}</div>`;
  if (R.open) {
    el.style.setProperty('--sheet-bg', R.dark ? '#1c1c1e' : '#f2f2f7');
    el.style.color = R.dark ? '#fff' : '#000';
    el.dataset.scheme = R.dark ? 'dark' : 'light';
  }
  const entry = layer(el, { onClose });
  $('[data-x]', el).addEventListener('click', () => entry.close());
  return entry;
}

function menu(anchor, items) {
  const el = document.createElement('div');
  el.className = 'menu';
  el.innerHTML = items.map((it, i) => {
    if (it === '-') return '<hr>';
    if (it.title) return `<div class="menu-title">${esc(it.title)}</div>`;
    return `<button data-i="${i}" class="${it.danger ? 'danger' : ''}"><span>${esc(it.label)}</span>${it.check !== undefined ? (it.check ? icon('check', 'tick') : '<span class="tick"></span>') : (it.icon ? icon(it.icon) : '')}</button>`;
  }).join('');
  const r = anchor.getBoundingClientRect();
  // Opens under the button, kept on screen.
  if (r.right - 250 < 10) el.style.left = Math.max(10, r.left) + 'px', el.style.transformOrigin = 'top left';
  else el.style.right = Math.max(10, innerWidth - r.right) + 'px';
  const below = r.bottom + 8;
  if (below + 320 > innerHeight) {
    el.style.bottom = (innerHeight - r.top + 8) + 'px';
    el.style.transformOrigin = el.style.left ? 'bottom left' : 'bottom right';
  } else {
    el.style.top = below + 'px';
  }
  const entry = layer(el, { clear: true });
  el.addEventListener('click', e => {
    const b = e.target.closest('button[data-i]');
    if (!b) return;
    entry.close();
    const it = items[+b.dataset.i];
    setTimeout(() => it.run(), 60);
  });
  return entry;
}

function confirmBox(title, text, okLabel, danger = true) {
  return new Promise(resolve => {
    const el = document.createElement('div');
    el.className = 'alert';
    el.innerHTML = `<b>${esc(title)}</b><p>${esc(text)}</p><div class="acts"><button data-no>Cancel</button><button data-ok class="${danger ? 'danger' : ''}">${esc(okLabel)}</button></div>`;
    let answer = false;
    const entry = layer(el, { onClose: () => resolve(answer) });
    $('[data-no]', el).onclick = () => entry.close();
    $('[data-ok]', el).onclick = () => { answer = true; entry.close(); };
  });
}

/* ================= covers ================= */

function coverHtml(b) {
  if (b.cover) return `<img class="cover" src="api/books/${b.id}/cover?t=${encodeURIComponent(token)}" alt="" loading="lazy" draggable="false">`;
  const longest = Math.max(...b.title.split(/\s+/).map(w => w.length));
  const small = longest > 9 || b.title.length > 28 ? ' small' : '';
  return `<div class="cover made-cover${small}"><div class="t">${esc(b.title)}</div><div class="a">${esc(b.author)}</div></div>`;
}

function progressLabel(b) {
  if (b.finished) return 'Finished';
  if (!b.opened_at) return '<span class="new">NEW</span>';
  return `${Math.max(1, Math.floor(b.percent))} %`;
}

function bookTile(b) {
  return `<div class="book" data-id="${b.id}">
    <button class="cover-box" data-open="${b.id}" aria-label="${esc(b.title)}">${coverHtml(b)}</button>
    <div class="info"><b>${esc(b.title)}</b><span>${esc(b.author)}</span></div>
    <div class="meta"><span>${progressLabel(b)}</span><button class="dots" data-more="${b.id}" aria-label="More">${icon('more')}</button></div>
  </div>`;
}

/* ================= tabs ================= */

function sorted() {
  const list = [...books];
  if (prefs.sort === 'title') list.sort((a, b) => a.title.localeCompare(b.title));
  else if (prefs.sort === 'author') list.sort((a, b) => (a.author || '~').localeCompare(b.author || '~') || a.title.localeCompare(b.title));
  else list.sort((a, b) => (b.opened_at || b.added_at).localeCompare(a.opened_at || a.added_at));
  return list;
}

function renderLibrary() {
  const shelf = $('#shelf');
  shelf.className = prefs.view === 'list' ? 'list' : 'grid';
  $('#btn-view use').setAttribute('href', prefs.view === 'list' ? '#i-grid' : '#i-lines');
  const pending = $$('.book.loading', shelf);
  if (!books.length && !pending.length) {
    shelf.className = '';
    shelf.innerHTML = `<div class="empty">${icon('book')}<b>No books yet</b>Tap + to add an EPUB, FB2 or TXT book. You can also send a book file to the bot.<br><button class="btn" data-add>${icon('plus')}Add a Book</button></div>`;
    return;
  }
  shelf.innerHTML = sorted().map(bookTile).join('');
  pending.forEach(p => shelf.prepend(p));
}

function renderHome() {
  const body = $('#home-body');
  if (!books.length) {
    body.innerHTML = `<div class="empty">${icon('book')}<b>Nothing to read yet</b>Add a book in your Library.<br><button class="btn" data-add>${icon('plus')}Add a Book</button></div>`;
    return;
  }
  const reading = books.filter(b => b.opened_at && !b.finished).sort((a, b) => b.opened_at.localeCompare(a.opened_at));
  const recent = [...books].sort((a, b) => b.added_at.localeCompare(a.added_at)).slice(0, 12);
  let html = '';
  if (reading.length) {
    html += `<div class="h2">Continue</div><div class="continue">` + reading.map(b => `
      <button class="cont-card" data-open="${b.id}">
        <div class="cover-box">${coverHtml(b)}</div>
        <div class="info"><small>${Math.max(1, Math.floor(b.percent))}%</small><b>${esc(b.title)}</b><span>${esc(b.author)}</span>
        <div class="progress"><i style="width:${clamp(b.percent, 1, 100)}%"></i></div></div>
      </button>`).join('') + `</div>`;
  }
  html += `<div class="h2" data-tab-link="library">Recently Added${icon('chev')}</div><div class="row-shelf">` + recent.map(bookTile).join('') + `</div>`;
  body.innerHTML = html;
}

function renderSearch() {
  const q = $('#lib-q').value.trim().toLowerCase();
  const out = $('#lib-results');
  if (!q) { out.innerHTML = ''; out.className = ''; return; }
  const hits = sorted().filter(b => (b.title + ' ' + b.author).toLowerCase().includes(q));
  out.className = 'list';
  out.innerHTML = hits.length ? hits.map(bookTile).join('') : `<div class="search-note">No books match “${esc(q)}”.</div>`;
}

function renderAll() {
  renderLibrary();
  if (tab === 'home') renderHome();
  if (tab === 'search') renderSearch();
}

function showTab(name) {
  tab = name;
  $$('#tabbar button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $$('.tab').forEach(t => { t.hidden = t.id !== 'tab-' + name; });
  if (name === 'home') renderHome();
  if (name === 'search') { renderSearch(); setTimeout(() => $('#lib-q').focus(), 50); }
  scrollTo(0, 0);
}

/* ================= library actions ================= */

function bookById(id) { return books.find(b => b.id === +id); }

async function setFinished(b, finished) {
  try {
    const row = await api(`books/${b.id}`, { method: 'PATCH', json: { finished, ...(finished ? {} : { percent: b.percent >= 99.5 ? 0 : b.percent }) } });
    Object.assign(b, row);
    renderAll();
  } catch (e) { toast(e.message); }
}

function bookMenu(anchor, b) {
  menu(anchor, [
    { title: b.title },
    { label: 'Read', icon: 'book', run: () => openBook(b.id) },
    b.finished ? { label: 'Mark as Still Reading', icon: 'book', run: () => setFinished(b, false) }
      : { label: 'Mark as Finished', icon: 'check', run: () => setFinished(b, true) },
    '-',
    { label: 'Remove…', icon: 'trash', danger: true, run: async () => {
      if (!await confirmBox('Remove this book?', `“${b.title}” and its bookmarks will be deleted from your library.`, 'Remove')) return;
      try {
        await api(`books/${b.id}`, { method: 'DELETE' });
        books = books.filter(x => x.id !== b.id);
        renderAll();
      } catch (e) { toast(e.message); }
    } },
  ]);
}

function libraryMenu(anchor) {
  const set = (k, v) => () => { prefs[k] = v; savePrefs(); renderAll(); };
  menu(anchor, [
    { label: 'Add a Book', icon: 'plus', run: pickFiles },
    '-',
    { title: 'Sort by' },
    { label: 'Recent', check: prefs.sort === 'recent', run: set('sort', 'recent') },
    { label: 'Title', check: prefs.sort === 'title', run: set('sort', 'title') },
    { label: 'Author', check: prefs.sort === 'author', run: set('sort', 'author') },
    '-',
    { label: 'Grid', check: prefs.view !== 'list', run: set('view', 'grid') },
    { label: 'List', check: prefs.view === 'list', run: set('view', 'list') },
  ]);
}

function pickFiles() {
  const input = $('#file');
  input.value = '';
  input.click();
}

function upload(file) {
  if (tab !== 'library') showTab('library');
  const shelf = $('#shelf');
  if (!books.length) { shelf.innerHTML = ''; shelf.className = prefs.view === 'list' ? 'list' : 'grid'; }
  const tile = document.createElement('div');
  tile.className = 'book loading';
  const title = file.name.replace(/(\.fb2)?\.(epub|fb2|txt|zip)$/i, '');
  tile.innerHTML = `<div class="cover-box"><div class="cover made-cover"><div class="t">${esc(title)}</div><div class="a"></div></div><div class="bar"><i></i></div></div><div class="info"><b>${esc(title)}</b><span>Adding…</span></div><div class="meta"><span>Adding…</span></div>`;
  shelf.prepend(tile);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', 'api/books');
  xhr.setRequestHeader('Authorization', 'Bearer ' + token);
  xhr.upload.onprogress = e => { if (e.lengthComputable) $('.bar i', tile).style.width = (e.loaded / e.total * 100) + '%'; };
  xhr.onload = () => {
    tile.remove();
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
    if (xhr.status === 200) {
      books.push(data);
      haptic('medium');
      renderAll();
    } else {
      toast(data.error || `Couldn't add ${file.name}.`, 4000);
      renderAll();
    }
  };
  xhr.onerror = () => { tile.remove(); toast(`Couldn't upload ${file.name}. Check the connection.`, 4000); renderAll(); };
  const fd = new FormData();
  fd.append('file', file, file.name);
  xhr.send(fd);
}

/* ================= reader ================= */

const R = {
  open: false, id: 0, meta: null, book: null, dark: true, menu: false,
  page: 0, pages: 1, pageW: 0, pageH: 0, secs: [], before: [], total: 1,
  text: null, saveTimer: 0, dirty: false, opening: false,
};
const reader = $('#reader');
const viewport = $('#viewport');
const flow = $('#flow');

function readerColors() {
  const t = THEMES[prefs.theme] || THEMES.original;
  return R.dark ? t.dark : t.light;
}

function applyReaderLook(keepPos) {
  const pos = keepPos && R.book ? currentPos() : null;
  R.dark = scheme() === 'dark';
  const t = THEMES[prefs.theme] || THEMES.original;
  const [bg, fg] = readerColors();
  const s = reader.style;
  s.setProperty('--rbg', bg);
  s.setProperty('--rfg', fg);
  s.setProperty('--rfg2', fg + '80');
  s.setProperty('--rsize', prefs.size + 'px');
  s.setProperty('--rlh', prefs.spacing);
  s.setProperty('--rmargin', Math.min(prefs.margins, innerWidth * .2) + 'px');
  s.setProperty('--rfont', (FONTS[prefs.font] || FONTS.original)[1]);
  s.setProperty('--rweight', (prefs.bold || t.bold) ? 600 : 400);
  reader.classList.toggle('justify', !!prefs.justify);
  reader.classList.toggle('paged', !prefs.scroll);
  reader.classList.toggle('dark', isDark(bg));
  applyScheme();
  if (R.book) {
    layout();
    if (pos) goToPos(pos, false);
  }
}

function isDark(hex) {
  const n = parseInt(hex.slice(1), 16);
  return ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000 < 128;
}

async function openBook(id) {
  if (R.opening) return;
  const b = bookById(id);
  if (!b) return;
  R.opening = true;
  const from = $(`[data-open="${id}"] .cover`);
  const start = from && from.getBoundingClientRect();
  reader.hidden = false;
  reader.style.opacity = '0';
  $('#spinner').hidden = false;
  R.open = true;
  R.id = b.id;
  R.meta = b;
  R.book = null;
  flow.innerHTML = '';
  $('#title-pill').textContent = b.title;
  $('#page-pill').textContent = '';
  reader.classList.remove('bare', 'menu-open');
  applyReaderLook(false);
  syncBack();
  // The cover flies from the shelf to the middle of the screen, as in Apple Books.
  let fly = null;
  if (start && start.width) {
    fly = from.cloneNode(true);
    fly.classList.add('fly');
    Object.assign(fly.style, { left: start.left + 'px', top: start.top + 'px', width: start.width + 'px', height: start.height + 'px' });
    document.body.appendChild(fly);
    const w = Math.min(innerWidth * .8, 340);
    const h = w * start.height / start.width;
    const dx = (innerWidth - w) / 2 - start.left;
    const dy = (innerHeight - h) / 2 - start.top;
    const k = w / start.width;
    fly.animate([{ transform: 'none' }, { transform: `translate(${dx}px, ${dy}px) scale(${k})` }],
      { duration: 420, easing: 'cubic-bezier(.2,.85,.25,1)', fill: 'forwards' });
    fly.style.transformOrigin = '0 0';
  }
  reader.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300, fill: 'forwards' }).onfinish = () => { reader.style.opacity = '1'; };
  try {
    const [data] = await Promise.all([api(`books/${id}`), wait(500)]);
    if (!R.open || R.id !== id) return;
    R.meta = data.meta;
    R.book = data.book;
    Object.assign(b, data.meta, { opened_at: new Date().toISOString() });
    render();
    const pos = data.meta.position || { c: 0, p: 0 };
    goToPos(pos, false);
    $('#spinner').hidden = true;
    if (fly) {
      const a = fly.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 260, fill: 'forwards' });
      a.onfinish = () => fly.remove();
    }
    reader.classList.remove('bare');
    setTimeout(() => { if (R.open && !R.menu) reader.classList.add('bare'); }, 2500);
  } catch (e) {
    if (fly) fly.remove();
    toast(e.message);
    closeReader(true);
  } finally {
    R.opening = false;
  }
}

function closeReader(quick) {
  if (!R.open) return;
  saveNow(true);
  closeReaderMenu();
  stack.slice().forEach(s => s.close());
  if (R.locked) { try { tg.unlockOrientation(); } catch (e) { /* older Telegram */ } R.locked = false; }
  R.open = false;
  const done = () => {
    reader.hidden = true;
    reader.style.opacity = '';
    flow.innerHTML = '';
    flow.style.transform = '';
    R.book = null;
    R.text = null;
    applyScheme();
    syncBack();
    renderAll();
  };
  if (quick) done();
  else reader.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.96)' }], { duration: 220 }).onfinish = done;
}

function resSrc(html) {
  return html.replace(/ src="res\/([^"]+)"/g, (m, name) => ` src="api/books/${R.id}/res/${encodeURIComponent(name)}?t=${encodeURIComponent(token)}"`);
}

function render() {
  const ch = R.book.chapters;
  flow.innerHTML = ch.map((c, i) => `<section class="ch" data-c="${i}">${resSrc(c.html)}</section>`).join('');
  R.before = [];
  let sum = 0;
  ch.forEach((c, i) => { R.before[i] = sum; sum += c.chars || 1; });
  R.total = sum || 1;
  R.text = null;
  // Pictures change the layout once they arrive.
  $$('img', flow).forEach(img => {
    if (!img.complete) img.addEventListener('load', relayoutSoon, { once: true });
    img.addEventListener('error', () => { img.remove(); relayoutSoon(); }, { once: true });
  });
  layout();
}

let relayoutTimer = 0;
function relayoutSoon() {
  clearTimeout(relayoutTimer);
  relayoutTimer = setTimeout(() => {
    if (!R.book) return;
    const pos = currentPos();
    layout();
    goToPos(pos, false);
  }, 150);
}

function flowPad() {
  const cs = getComputedStyle(flow);
  return { top: parseFloat(cs.paddingTop) || 0, bottom: parseFloat(cs.paddingBottom) || 0 };
}

/* Measures the book: page size, number of pages, where each chapter starts. */
function layout() {
  const secs = $$('section.ch', flow);
  R.secs = [];
  if (!prefs.scroll) {
    flow.style.transform = 'none';
    R.pageW = innerWidth;
    const fr = flow.getBoundingClientRect();
    R.pageH = fr.height;
    R.pages = Math.max(1, Math.round((flow.scrollWidth + 2 * parseFloat(getComputedStyle(reader).getPropertyValue('--rmargin'))) / R.pageW));
    secs.forEach((s, i) => {
      const rects = s.getClientRects();
      const first = rects.length ? rects[0] : s.getBoundingClientRect();
      const last = rects.length ? rects[rects.length - 1] : first;
      const start = Math.max(0, Math.floor((first.left - fr.left + 2) / R.pageW));
      const end = Math.max(start, Math.floor((last.right - fr.left - 2) / R.pageW));
      R.secs[i] = { start, len: end - start + 1 };
    });
    showPage(clamp(R.page, 0, R.pages - 1), false);
  } else {
    flow.style.transform = '';
    const pad = flowPad();
    R.pageH = Math.max(200, viewport.clientHeight - pad.top - pad.bottom + 40);
    const contentH = flow.scrollHeight - pad.top - pad.bottom;
    R.pages = Math.max(1, Math.ceil(contentH / R.pageH));
    secs.forEach((s, i) => { R.secs[i] = { top: s.offsetTop - pad.top, h: Math.max(1, s.offsetHeight) }; });
  }
  updatePill();
}

/* Position as {c: chapter, p: 0..1 inside it}; survives font and size changes. */
function currentPos() {
  if (!R.secs.length) return { c: 0, p: 0 };
  if (!prefs.scroll) {
    let c = 0;
    for (let i = 0; i < R.secs.length; i++) if (R.secs[i].start <= R.page) c = i;
    const s = R.secs[c];
    return { c, p: clamp((R.page - s.start) / s.len, 0, 1) };
  }
  const y = viewport.scrollTop + 4;
  let c = 0;
  for (let i = 0; i < R.secs.length; i++) if (R.secs[i].top <= y) c = i;
  const s = R.secs[c];
  return { c, p: clamp((y - s.top) / s.h, 0, 1) };
}

function goToPos(pos, animate) {
  const c = clamp(pos.c | 0, 0, R.secs.length - 1);
  const s = R.secs[c];
  if (!s) return;
  if (!prefs.scroll) showPage(s.start + Math.floor(pos.p * s.len + 1e-6), animate);
  else viewport.scrollTop = s.top + pos.p * s.h;
  updatePill();
}

function goToElement(el) {
  if (!el) return;
  if (!prefs.scroll) {
    const fr = flow.getBoundingClientRect();
    const r = el.getClientRects()[0] || el.getBoundingClientRect();
    showPage(Math.floor((r.left - fr.left + 2) / R.pageW), false);
  } else {
    const pad = flowPad();
    viewport.scrollTop = el.getBoundingClientRect().top - flow.getBoundingClientRect().top - pad.top;
  }
  updatePill();
}

function pageOfElement(el) {
  const r = (el.getClientRects && el.getClientRects()[0]) || el.getBoundingClientRect();
  const fr = flow.getBoundingClientRect();
  if (!prefs.scroll) return clamp(Math.floor((r.left - fr.left + 2) / R.pageW), 0, R.pages - 1);
  return clamp(Math.floor((r.top - fr.top - flowPad().top) / R.pageH), 0, R.pages - 1);
}

function pageOfPos(pos) {
  const s = R.secs[clamp(pos.c | 0, 0, R.secs.length - 1)];
  if (!s) return 0;
  if (!prefs.scroll) return clamp(s.start + Math.floor(pos.p * s.len + 1e-6), 0, R.pages - 1);
  return clamp(Math.floor((s.top + pos.p * s.h) / R.pageH), 0, R.pages - 1);
}

function currentPage() {
  if (!prefs.scroll) return R.page;
  const max = viewport.scrollHeight - viewport.clientHeight;
  if (max > 0 && viewport.scrollTop >= max - 2) return R.pages - 1;
  return clamp(Math.floor((viewport.scrollTop + 4) / R.pageH), 0, R.pages - 1);
}

function showPage(n, animate) {
  R.page = clamp(n, 0, R.pages - 1);
  const x = -R.page * R.pageW;
  if (animate) {
    flow.style.transition = 'transform .32s cubic-bezier(.25,.8,.3,1)';
    flow.addEventListener('transitionend', () => { flow.style.transition = ''; }, { once: true });
  } else {
    flow.style.transition = '';
  }
  flow.style.transform = `translate3d(${x}px,0,0)`;
  updatePill();
}

function updatePill() {
  if (!R.book) return;
  $('#page-pill').textContent = `${currentPage() + 1} of ${R.pages}`;
  $('#m-bookmark use').setAttribute('href', bookmarkHere() ? '#i-bookmark-fill' : '#i-bookmark');
  if (R.menu) placeThumb();
  markDirty();
}

function percentOf(pos) {
  const ch = R.book.chapters[pos.c];
  return clamp((R.before[pos.c] + pos.p * (ch ? ch.chars : 0)) / R.total * 100, 0, 100);
}

function markDirty() {
  R.dirty = true;
  clearTimeout(R.saveTimer);
  R.saveTimer = setTimeout(() => saveNow(false), 1200);
}

function progressBody() {
  const pos = currentPos();
  const atEnd = currentPage() >= R.pages - 1 && R.pages > 1;
  const body = { position: pos, percent: atEnd ? 100 : percentOf(pos) };
  if (atEnd) body.finished = true;
  return body;
}

function saveNow(beacon) {
  if (!R.book || !R.dirty) return;
  R.dirty = false;
  clearTimeout(R.saveTimer);
  const body = progressBody();
  const b = bookById(R.id);
  if (b) Object.assign(b, { position: body.position, percent: body.percent, finished: body.finished || b.finished });
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon(`api/books/${R.id}/progress?t=${encodeURIComponent(token)}`, new Blob([JSON.stringify(body)], { type: 'text/plain' }));
  } else {
    api(`books/${R.id}`, { method: 'PATCH', json: body }).catch(() => { R.dirty = true; });
  }
}

/* ---------- gestures ---------- */

function toggleChrome(show) {
  const bare = show === undefined ? !reader.classList.contains('bare') : !show;
  reader.classList.toggle('bare', bare);
}

function turn(dir) {
  if (prefs.scroll) {
    viewport.scrollBy({ top: dir * R.pageH * .9, behavior: 'smooth' });
    return;
  }
  const n = clamp(R.page + dir, 0, R.pages - 1);
  if (n === R.page) { showPage(n, true); return; }
  reader.classList.add('bare');
  showPage(n, true);
}

const touch = { x: 0, y: 0, t: 0, dx: 0, active: false, horizontal: null };

viewport.addEventListener('touchstart', e => {
  if (e.touches.length !== 1) return;
  touch.x = e.touches[0].clientX; touch.y = e.touches[0].clientY; touch.t = Date.now();
  touch.dx = 0; touch.active = true; touch.horizontal = null;
}, { passive: true });

viewport.addEventListener('touchmove', e => {
  if (!touch.active || prefs.scroll || !getSelection().isCollapsed) return;
  const dx = e.touches[0].clientX - touch.x;
  const dy = e.touches[0].clientY - touch.y;
  if (touch.horizontal === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) touch.horizontal = Math.abs(dx) > Math.abs(dy);
  if (!touch.horizontal) return;
  e.preventDefault();
  touch.dx = dx;
  const edge = (R.page === 0 && dx > 0) || (R.page === R.pages - 1 && dx < 0);
  flow.style.transition = '';
  flow.style.transform = `translate3d(${-R.page * R.pageW + (edge ? dx / 3 : dx)}px,0,0)`;
}, { passive: false });

viewport.addEventListener('touchend', () => {
  if (!touch.active) return;
  touch.active = false;
  if (prefs.scroll || !touch.horizontal) return;
  const fast = Math.abs(touch.dx) / Math.max(1, Date.now() - touch.t) > .35;
  if (Math.abs(touch.dx) > R.pageW * .25 || (fast && Math.abs(touch.dx) > 20)) turn(touch.dx < 0 ? 1 : -1);
  else showPage(R.page, true);
  touch.horizontal = null;
});

viewport.addEventListener('click', e => {
  if (!R.book) return;
  const a = e.target.closest('a');
  if (a && a.dataset.ext) {
    try { tg.openLink(a.dataset.ext); } catch (err) { window.open(a.dataset.ext, '_blank'); }
    return;
  }
  if (a && a.dataset.c !== undefined) {
    const target = (a.dataset.a && document.getElementById(a.dataset.a)) || $(`section.ch[data-c="${a.dataset.c}"]`, flow);
    goToElement(target);
    return;
  }
  if (getSelection().toString()) return;
  if (lookUp(e.clientX, e.clientY)) return;
  const x = e.clientX / innerWidth;
  if (!prefs.scroll && x < .28) turn(-1);
  else if (!prefs.scroll && x > .72) turn(1);
  else toggleChrome();
});

/* ---------- tap a word (or hold and stretch a phrase): what it means here ---------- */

const WORD_CH = /[\p{L}\p{M}\p{N}'’\-]/u;
const BLOCKS = 'p,li,blockquote,h1,h2,h3,h4,h5,h6,td,th,dd,dt,figcaption,pre';
const PHRASE_MAX = 200;
const explained = new Map(); // "book|word|sentence" -> {text, picture}
let card = null; // the open word card

function caretAt(x, y) {
  if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
  const p = document.caretPositionFromPoint && document.caretPositionFromPoint(x, y);
  if (!p) return null;
  const r = document.createRange();
  r.setStart(p.offsetNode, p.offset);
  return r;
}

// The word under a tap, or null when the tap is on blank space.
function wordAt(x, y) {
  const c = caretAt(x, y);
  if (!c || c.startContainer.nodeType !== 3 || !flow.contains(c.startContainer)) return null;
  const node = c.startContainer;
  const t = node.data;
  let a = c.startOffset, b = c.startOffset;
  while (a > 0 && WORD_CH.test(t[a - 1])) a--;
  while (b < t.length && WORD_CH.test(t[b])) b++;
  while (a < b && /['’\-]/.test(t[a])) a++;
  while (b > a && /['’\-]/.test(t[b - 1])) b--;
  if (a === b || !/\p{L}/u.test(t.slice(a, b))) return null;
  const range = document.createRange();
  range.setStart(node, a);
  range.setEnd(node, b);
  const hit = [...range.getClientRects()].some(r => x >= r.left - 4 && x <= r.right + 4 && y >= r.top - 4 && y <= r.bottom + 4);
  return hit ? { range, word: t.slice(a, b) } : null;
}

const squash = s => s.replace(/\s+/g, ' ').trim();

// The sentence (or sentences) the word or phrase is in, and the text around it for context.
function sentenceOf(w) {
  const start = w.range.startContainer;
  const el0 = start.nodeType === 3 ? start.parentElement : start;
  const block = el0.closest(BLOCKS) || el0;
  const text = block.textContent;
  const before = document.createRange();
  before.setStart(block, 0);
  before.setEnd(w.range.startContainer, w.range.startOffset);
  const at = before.toString().length;
  const end = at + Math.max(1, w.range.toString().length);
  let sentence = '';
  try {
    const segs = [...new Intl.Segmenter(R.book.language || undefined, { granularity: 'sentence' }).segment(text)];
    sentence = segs.filter(g => g.index < end && g.index + g.segment.length > at).map(g => g.segment).join('');
  } catch (e) { /* no Intl.Segmenter, or an odd language tag: the plain way below */ }
  if (!sentence) {
    const from = Math.max(0, ...['. ', '! ', '? ', '… '].map(m => text.lastIndexOf(m, at) + 1));
    const ends = ['.', '!', '?', '…'].map(m => text.indexOf(m, end - 1)).filter(i => i >= 0);
    sentence = text.slice(from, ends.length ? Math.min(...ends) + 1 : text.length);
  }
  sentence = squash(sentence);
  if (sentence.length > 560) {
    const i = Math.max(0, sentence.indexOf(squash(w.word)) - 250);
    sentence = '…' + sentence.slice(i, i + 540) + '…';
  }
  let context = squash(text);
  const prev = block.previousElementSibling;
  if (prev && prev.matches(BLOCKS)) context = squash(prev.textContent).slice(-400) + '\n' + context;
  return { sentence, context: context.slice(0, 1400) };
}

function lookUp(x, y) {
  const w = wordAt(x, y);
  if (!w) return false;
  openCard(w, false);
  return true;
}

const WAIT = '<span class="look-wait"><i></i><i></i><i></i></span>';

function openLink(url) {
  try { tg.openLink(url); } catch (e) { window.open(url, '_blank', 'noopener'); }
}

// The card next to the word: the meaning, real photos, Ask, and Add to Deck.
// fromSelection: opened by holding and stretching a selection; it then has no scrim (the handles stay
// usable) and goes away when the selection does, until the card itself is touched.
function openCard(w, fromSelection) {
  if (card) card.close();
  const word = squash(w.word).slice(0, PHRASE_MAX);
  const { sentence, context } = sentenceOf(w);
  const hl = !fromSelection && window.CSS && CSS.highlights && window.Highlight;
  if (hl) CSS.highlights.set('look', new Highlight(w.range));
  haptic('light');

  const el = document.createElement('div');
  el.className = 'look ' + (R.dark ? 'dark' : 'light');
  el.innerHTML = `<div class="look-head"><b>${esc(word)}</b></div>
    <div class="look-text">${WAIT}</div>
    <div class="look-pics" hidden></div>
    <div class="look-qa"></div>
    <form class="look-ask" hidden><input type="text" enterkeyhint="send" placeholder="Ask anything about it" maxlength="500"><button type="submit" aria-label="Send">${icon('send')}</button></form>
    <div class="look-acts"><button type="button" class="look-btn look-askbtn">${icon('ask')}<span>Ask</span></button><button type="button" class="look-btn look-add">${icon('plus')}<span>Add to Deck</span></button></div>`;
  const r = w.range.getBoundingClientRect();
  const width = Math.min(380, innerWidth - 24);
  el.style.width = width + 'px';
  el.style.left = clamp(r.left + r.width / 2 - width / 2, 12, innerWidth - width - 12) + 'px';
  const place = () => {
    // Next to the word, on the side with more room, never over it. While typing a question it moves to the
    // top, clear of the keyboard.
    el.style.top = el.style.bottom = '';
    if (el.classList.contains('asking')) {
      el.style.top = 'calc(var(--safe-top) + 10px)';
      el.style.maxHeight = '46vh';
      return;
    }
    const roomBelow = innerHeight - r.bottom, roomAbove = r.top;
    if (roomBelow >= roomAbove || (fromSelection && roomBelow > 260)) {
      el.style.top = (r.bottom + (fromSelection ? 30 : 10)) + 'px';
      el.style.maxHeight = (roomBelow - 24 - (fromSelection ? 20 : 0)) + 'px';
      el.style.transformOrigin = 'top center';
    } else {
      el.style.bottom = (innerHeight - r.top + (fromSelection ? 50 : 10)) + 'px';
      el.style.maxHeight = (roomAbove - 44 - (fromSelection ? 40 : 0)) + 'px';
      el.style.transformOrigin = 'bottom center';
    }
  };
  place();

  const state = { word, explanation: '', history: [], pinned: !fromSelection, fromSelection };
  const entry = layer(el, {
    scrim: !fromSelection, clear: true,
    onClose: () => {
      if (hl) CSS.highlights.delete('look');
      if (card && card.el === el) card = null;
      document.removeEventListener('pointerdown', outside, true);
    },
  });
  card = { el, state, close: () => entry.close() };
  // A selection card stays put once touched, and then closes on a tap anywhere else.
  const outside = e => { if (state.pinned && !el.contains(e.target)) entry.close(); };
  if (fromSelection) {
    el.addEventListener('pointerdown', () => { state.pinned = true; }, true);
    el.addEventListener('touchstart', () => { state.pinned = true; }, { capture: true, passive: true });
    setTimeout(() => document.addEventListener('pointerdown', outside, true), 0);
  }

  const out = $('.look-text', el);
  const pics = $('.look-pics', el);
  const qa = $('.look-qa', el);
  const askForm = $('.look-ask', el);
  const input = $('input', askForm);
  const add = $('.look-add', el);
  const key = `${R.id}|${word.toLowerCase()}|${sentence}`;
  const body = () => ({ word, sentence, context });

  const showPictures = q => {
    if (!q) return;
    api('pictures?q=' + encodeURIComponent(q)).then(list => {
      if (!list.length || !el.isConnected) return;
      pics.innerHTML = list.map((p, i) => `<button type="button" data-i="${i}" aria-label="${esc(p.title)}"><img src="${esc(p.src)}" alt="${esc(p.title)}" loading="lazy" referrerpolicy="no-referrer"></button>`).join('')
        + '<small>Photos: Wikimedia Commons</small>';
      pics.hidden = false;
      $$('img', pics).forEach(img => { img.onerror = () => img.parentElement.remove(); });
      pics.onclick = e => { const b = e.target.closest('button[data-i]'); if (b && list[+b.dataset.i].page) openLink(list[+b.dataset.i].page); };
    }).catch(() => {});
  };
  const show = res => {
    state.explanation = res.text;
    // The explanation opens with the dictionary form and part of speech: that becomes the title.
    const lines = res.text.split('\n');
    if (lines.length > 1 && lines[0].length <= 80) {
      $('.look-head b', el).textContent = lines[0].replace(/^[*#\s]+|[*\s]+$/g, '');
      out.textContent = lines.slice(1).join('\n').trim();
    } else {
      out.textContent = res.text;
    }
    showPictures(res.picture);
  };
  const explain = () => {
    if (explained.has(key)) { show(explained.get(key)); return; }
    api(`books/${R.id}/explain`, { method: 'POST', json: body() })
      .then(res => { explained.set(key, res); if (el.isConnected) show(res); })
      .catch(err => {
        out.innerHTML = `<span class="look-err">${esc(err.message)}</span> <button type="button" class="look-retry">Try Again</button>`;
        $('.look-retry', out).onclick = () => { out.innerHTML = WAIT; explain(); };
      });
  };
  explain();

  $('.look-askbtn', el).onclick = () => {
    askForm.hidden = false;
    $('.look-askbtn', el).hidden = true;
    el.classList.add('asking');
    place();
    input.focus();
  };
  askForm.onsubmit = e => {
    e.preventDefault();
    const question = input.value.trim();
    if (!question) return;
    input.value = '';
    const item = document.createElement('div');
    item.className = 'look-turn';
    item.innerHTML = `<p class="look-q">${esc(question)}</p><p class="look-a">${WAIT}</p>`;
    qa.appendChild(item);
    el.scrollTop = el.scrollHeight;
    const a = $('.look-a', item);
    api(`books/${R.id}/ask`, { method: 'POST', json: { ...body(), explanation: state.explanation, history: state.history, question } })
      .then(res => {
        state.history.push({ q: question, a: res.text });
        a.textContent = res.text;
        el.scrollTop = el.scrollHeight;
      })
      .catch(err => { a.innerHTML = `<span class="look-err">${esc(err.message)}</span>`; });
  };

  add.onclick = () => {
    add.disabled = true;
    api(`books/${R.id}/add-word`, { method: 'POST', json: { word, sentence, meaning: state.explanation } })
      .then(() => {
        add.classList.add('done');
        add.innerHTML = `${icon('check')}<span>Sent to the Bot</span>`;
        haptic('medium');
        toast('The bot will send the card preview in your chat', 3200);
      })
      .catch(err => { add.disabled = false; toast(err.message); });
  };
  return card;
}

// A selection that starts or ends inside a word takes in the whole word.
function snapToWords(range) {
  const { startContainer: a, endContainer: b } = range;
  let i = range.startOffset, j = range.endOffset;
  if (a.nodeType === 3) { while (i > 0 && WORD_CH.test(a.data[i - 1])) i--; range.setStart(a, i); }
  if (b.nodeType === 3) { while (j < b.data.length && WORD_CH.test(b.data[j])) j++; range.setEnd(b, j); }
  return range;
}

// Hold a word, stretch the selection over a phrase, let go: the card explains the whole phrase.
let selTimer = 0;
document.addEventListener('selectionchange', () => {
  clearTimeout(selTimer);
  if (!R.open || !R.book) return;
  const sel = getSelection();
  const inBook = sel.rangeCount && !sel.isCollapsed && flow.contains(sel.getRangeAt(0).commonAncestorContainer);
  if (!inBook) {
    if (card && card.state.fromSelection && !card.state.pinned) card.close();
    return;
  }
  selTimer = setTimeout(() => {
    const s = getSelection();
    if (!s.rangeCount || s.isCollapsed) return;
    const range = snapToWords(s.getRangeAt(0).cloneRange());
    const phrase = squash(range.toString());
    if (!/\p{L}/u.test(phrase) || !flow.contains(range.commonAncestorContainer)) return;
    if (card && card.state.word === phrase.slice(0, PHRASE_MAX)) return;
    if (card && !card.state.fromSelection) return; // a tapped word's card is open: leave it be
    openCard({ range, word: phrase }, true);
  }, 650);
});

let lastScroll = 0;
viewport.addEventListener('scroll', () => {
  if (!prefs.scroll || !R.book) return;
  const y = viewport.scrollTop;
  if (Math.abs(y - lastScroll) > 30) { reader.classList.add('bare'); lastScroll = y; }
  updatePill();
}, { passive: true });

document.addEventListener('keydown', e => {
  if (!R.open || stack.length) return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); turn(1); }
  if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); turn(-1); }
  if (e.key === 'Escape') back();
});

let resizeTimer = 0;
window.addEventListener('resize', () => {
  if (!R.book) return;
  clearTimeout(resizeTimer);
  const pos = currentPos();
  resizeTimer = setTimeout(() => { applyReaderLook(false); goToPos(pos, false); }, 120);
});

document.addEventListener('visibilitychange', () => { if (document.hidden) saveNow(true); });
window.addEventListener('pagehide', () => saveNow(true));

/* ---------- the reading menu ---------- */

function openReaderMenu() {
  R.menu = true;
  const m = $('#rmenu');
  m.hidden = false;
  reader.classList.add('menu-open');
  placeThumb();
  requestAnimationFrame(() => requestAnimationFrame(() => m.classList.add('in')));
  haptic();
  syncBack();
}

function closeReaderMenu() {
  if (!R.menu) return;
  R.menu = false;
  const m = $('#rmenu');
  m.classList.remove('in');
  reader.classList.remove('menu-open');
  setTimeout(() => { if (!R.menu) m.hidden = true; }, 260);
  syncBack();
}

$('#rmenu').addEventListener('click', e => { if (e.target.id === 'rmenu') closeReaderMenu(); });
$('#btn-menu').addEventListener('click', openReaderMenu);
$('#btn-close').addEventListener('click', () => closeReader());

function menuThen(fn) {
  return () => { closeReaderMenu(); setTimeout(fn, 120); };
}

$('#m-contents').addEventListener('click', menuThen(showContents));
$('#m-search').addEventListener('click', menuThen(showBookSearch));
$('#m-themes').addEventListener('click', menuThen(showThemes));
$('#m-share').addEventListener('click', share);
$('#m-rotate').addEventListener('click', toggleRotation);
$('#m-scroll').addEventListener('click', () => {
  const pos = currentPos();
  prefs.scroll = !prefs.scroll;
  savePrefs();
  R.page = 0;
  applyReaderLook(false);
  goToPos(pos, false);
  haptic();
});
$('#m-bookmark').addEventListener('click', toggleBookmark);

/* scrubber: drag up and down to move through the book */
function placeThumb() {
  const sc = $('#scrubber');
  const h = sc.clientHeight || 300;
  const f = R.pages > 1 ? currentPage() / (R.pages - 1) : 0;
  $('#scrub-thumb').style.top = (10 + f * (h - 26)) + 'px';
}

(() => {
  const sc = $('#scrubber');
  const tip = $('#scrub-tip');
  let dragging = false;
  const move = e => {
    const r = sc.getBoundingClientRect();
    const f = clamp((e.clientY - r.top - 10) / (r.height - 26), 0, 1);
    const page = Math.round(f * (R.pages - 1));
    if (!prefs.scroll) showPage(page, false);
    else viewport.scrollTop = page * R.pageH;
    updatePill();
    const pos = currentPos();
    const toc = tocAt(pos);
    tip.hidden = false;
    tip.style.top = (e.clientY - r.top) + 'px';
    tip.textContent = `${page + 1} of ${R.pages}` + (toc ? ` · ${toc.title}` : '');
  };
  sc.addEventListener('pointerdown', e => { dragging = true; sc.setPointerCapture(e.pointerId); move(e); });
  sc.addEventListener('pointermove', e => { if (dragging) move(e); });
  const end = () => { dragging = false; tip.hidden = true; };
  sc.addEventListener('pointerup', end);
  sc.addEventListener('pointercancel', end);
})();

function tocAt(pos) {
  let best = null;
  const page = pageOfPos(pos);
  for (const t of (R.book.toc || [])) {
    if (tocPage(t) <= page) best = t;
  }
  return best;
}

function tocPage(t) {
  const el = (t.a && document.getElementById(t.a)) || $(`section.ch[data-c="${t.c}"]`, flow);
  return el ? pageOfElement(el) : 0;
}

/* ---------- contents and bookmarks ---------- */

function showContents(which = 'contents') {
  const cur = currentPage();
  const toc = R.book.toc || [];
  const pages = toc.map(tocPage);
  let curIdx = -1;
  pages.forEach((p, i) => { if (p <= cur) curIdx = i; });
  const tocHtml = toc.length ? toc.map((t, i) => `<button class="toc-item l${Math.min(2, t.level || 0)} ${i === curIdx ? 'cur' : ''}" data-t="${i}"><span>${esc(t.title)}</span><small>${pages[i] + 1}</small></button>`).join('')
    : '<div class="toc-empty">This book has no table of contents.</div>';
  const marks = (R.meta.bookmarks || []).map((b, i) => ({ ...b, i, page: pageOfPos(b) })).sort((a, b) => a.page - b.page);
  const markHtml = marks.length ? marks.map(b => {
    const t = tocAt(b);
    return `<div style="display:flex;align-items:center"><button class="toc-item" data-b="${b.i}"><span>${esc(t ? t.title : R.meta.title)}<span class="sub">${esc(b.text || '')}</span></span><small>${b.page + 1}</small></button><button class="round" data-del="${b.i}" style="width:40px;height:40px;color:var(--fg2)" aria-label="Delete bookmark">${icon('trash')}</button></div>`;
  }).join('') : '<div class="toc-empty">No bookmarks yet. Tap the bookmark button in the reading menu to add one.</div>';
  const body = `<div class="seg"><button data-seg="contents">Contents</button><button data-seg="marks">Bookmarks</button></div><div data-pane="contents">${tocHtml}</div><div data-pane="marks">${markHtml}</div>`;
  const s = sheet(R.meta.title, body, { tall: true });
  const setSeg = w => {
    $$('[data-seg]', s.el).forEach(b => b.classList.toggle('on', b.dataset.seg === w));
    $$('[data-pane]', s.el).forEach(p => { p.hidden = p.dataset.pane !== w; });
  };
  setSeg(which);
  s.el.addEventListener('click', e => {
    const seg = e.target.closest('[data-seg]');
    if (seg) return setSeg(seg.dataset.seg);
    const ti = e.target.closest('[data-t]');
    if (ti) {
      const t = toc[+ti.dataset.t];
      s.close();
      goToElement((t.a && document.getElementById(t.a)) || $(`section.ch[data-c="${t.c}"]`, flow));
      return;
    }
    const del = e.target.closest('[data-del]');
    if (del) {
      R.meta.bookmarks.splice(+del.dataset.del, 1);
      saveBookmarks();
      s.close();
      setTimeout(() => showContents('marks'), 330);
      return;
    }
    const bi = e.target.closest('[data-b]');
    if (bi) {
      s.close();
      goToPos(R.meta.bookmarks[+bi.dataset.b], false);
    }
  });
  const curRow = $('.toc-item.cur', s.el);
  if (curRow && which === 'contents') setTimeout(() => curRow.scrollIntoView({ block: 'center' }), 340);
}

function bookmarkHere() {
  const cur = currentPage();
  return (R.meta && R.meta.bookmarks || []).findIndex(b => pageOfPos(b) === cur);
}

function textAtTop(n = 140) {
  const index = textIndex();
  const pos = startOfPage();
  if (!pos) return '';
  let i = index.offsetOf(pos.node, pos.offset);
  while (i > 0 && /\S/.test(index.text[i - 1])) i--; // start at a whole word
  const out = index.text.slice(i, i + n).replace(/\s+/g, ' ').trim();
  return out.length >= n - 1 && out.includes(' ') ? out.slice(0, out.lastIndexOf(' ')) : out;
}

function startOfPage() {
  const pad = flowPad();
  const left = parseFloat(getComputedStyle(reader).getPropertyValue('--rmargin')) + 4;
  const top = prefs.scroll ? pad.top + 8 : flow.getBoundingClientRect().top + 8;
  for (let dy = 0; dy < 200; dy += 20) {
    const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(left, top + dy)
      : (document.caretPositionFromPoint && (() => { const p = document.caretPositionFromPoint(left, top + dy); if (!p) return null; const rr = document.createRange(); rr.setStart(p.offsetNode, p.offset); return rr; })());
    if (r && flow.contains(r.startContainer) && r.startContainer.nodeType === 3) return { node: r.startContainer, offset: r.startOffset };
  }
  return null;
}

function toggleBookmark() {
  const i = bookmarkHere();
  R.meta.bookmarks = R.meta.bookmarks || [];
  if (i >= 0) {
    R.meta.bookmarks.splice(i, 1);
    toast('Bookmark removed', 1400);
  } else {
    R.meta.bookmarks.push({ ...currentPos(), text: textAtTop(), at: new Date().toISOString() });
    toast('Bookmarked', 1400);
    haptic('medium');
  }
  saveBookmarks();
  updatePill();
}

function saveBookmarks() {
  api(`books/${R.id}`, { method: 'PATCH', json: { bookmarks: R.meta.bookmarks } }).catch(e => toast(e.message));
  const b = bookById(R.id);
  if (b) b.bookmarks = R.meta.bookmarks;
}

/* ---------- search in the book ---------- */

function textIndex() {
  if (R.text) return R.text;
  const nodes = [];
  const starts = [];
  let text = '';
  const walker = document.createTreeWalker(flow, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    nodes.push(n);
    starts.push(text.length);
    text += n.nodeValue;
    const p = n.parentElement;
    if (p && !n.nextSibling && /^(P|H\d|LI|DIV|BLOCKQUOTE|TD|SECTION)$/.test(p.tagName)) text += ' ';
  }
  const lower = text.toLowerCase();
  const find = i => {
    let lo = 0, hi = starts.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (starts[mid] <= i) lo = mid; else hi = mid - 1; }
    return lo;
  };
  R.text = {
    text, lower,
    range(i, len) {
      const a = find(i), b = find(i + len - 1);
      const r = document.createRange();
      r.setStart(nodes[a], clamp(i - starts[a], 0, nodes[a].nodeValue.length));
      r.setEnd(nodes[b], clamp(i + len - starts[b], 0, nodes[b].nodeValue.length));
      return r;
    },
    offsetOf(node, off) { const k = nodes.indexOf(node); return k < 0 ? 0 : starts[k] + off; },
  };
  return R.text;
}

function rangePage(r) {
  const rect = r.getClientRects()[0] || r.getBoundingClientRect();
  const fr = flow.getBoundingClientRect();
  if (!prefs.scroll) return clamp(Math.floor((rect.left - fr.left + 2) / R.pageW), 0, R.pages - 1);
  return clamp(Math.floor((rect.top - fr.top - flowPad().top) / R.pageH), 0, R.pages - 1);
}

let lastQuery = '';
function showBookSearch() {
  const s = sheet('Search Book', `<div class="bsearch"><label class="search-field">${icon('search')}<input type="search" placeholder="Search this book" enterkeyhint="search" autocomplete="off" value="${esc(lastQuery)}"></label></div><div class="hits"></div>`, { tall: true, onClose: () => clearHit() });
  const input = $('input', s.el);
  const out = $('.hits', s.el);
  let timer = 0;
  let found = [];
  const run = () => {
    const q = input.value.trim().toLowerCase();
    lastQuery = input.value.trim();
    if (q.length < 2) { out.innerHTML = q ? '' : '<div class="search-note">Search for a word or phrase.</div>'; return; }
    const idx = textIndex();
    found = [];
    let i = idx.lower.indexOf(q);
    while (i >= 0 && found.length < 300) { found.push(i); i = idx.lower.indexOf(q, i + q.length); }
    if (!found.length) { out.innerHTML = `<div class="search-note">No results for “${esc(input.value.trim())}”.</div>`; return; }
    const rows = found.map((at, k) => {
      const r = idx.range(at, q.length);
      const page = rangePage(r);
      const pos = (() => { const sec = r.startContainer.parentElement.closest('section.ch'); return sec ? { c: +sec.dataset.c, p: 0 } : { c: 0, p: 0 }; })();
      const t = R.book.toc.filter(t => t.c <= pos.c).pop();
      const a = Math.max(0, at - 50), b = Math.min(idx.text.length, at + q.length + 70);
      const snip = (a > 0 ? '…' : '') + esc(idx.text.slice(a, at)) + '<mark>' + esc(idx.text.slice(at, at + q.length)) + '</mark>' + esc(idx.text.slice(at + q.length, b)) + (b < idx.text.length ? '…' : '');
      return `<button class="hit-row" data-k="${k}"><div class="where"><span>${esc(t ? t.title : '')}</span><span>Page ${page + 1}</span></div><div class="snip">${snip.replace(/\s+/g, ' ')}</div></button>`;
    });
    out.innerHTML = `<div class="search-note" style="padding:4px 0 8px;text-align:left">${found.length}${found.length >= 300 ? '+' : ''} result${found.length === 1 ? '' : 's'}</div>` + rows.join('');
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 250); });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { input.blur(); run(); } });
  out.addEventListener('click', e => {
    const row = e.target.closest('[data-k]');
    if (!row) return;
    const at = found[+row.dataset.k];
    const r = textIndex().range(at, input.value.trim().length);
    s.close();
    setTimeout(() => showHit(r), 10);
  });
  run();
  setTimeout(() => input.focus(), 350);
}

function showHit(r) {
  if (!prefs.scroll) showPage(rangePage(r), false);
  else viewport.scrollTop = Math.max(0, r.getBoundingClientRect().top - flow.getBoundingClientRect().top - flowPad().top - innerHeight / 3);
  if (window.CSS && CSS.highlights && window.Highlight) {
    CSS.highlights.set('hit', new Highlight(r));
    setTimeout(clearHit, 4000);
  }
  updatePill();
}

function clearHit() {
  if (window.CSS && CSS.highlights) CSS.highlights.delete('hit');
}

/* ---------- themes and settings ---------- */

function showThemes() {
  const s = sheet('Themes & Settings', '', { onClose: () => savePrefs() });
  const body = $('.sheet-body', s.el);
  const draw = () => {
    const tiles = Object.entries(THEMES).map(([k, t]) => {
      const [bg, fg] = R.dark ? t.dark : t.light;
      return `<button class="theme ${prefs.theme === k ? 'on' : ''}" data-theme="${k}" style="background:${bg};color:${fg}"><span class="aa" style="font-family:${FONTS[prefs.font][1].replace(/"/g, "'")};font-weight:${t.bold ? 700 : 400}">Aa</span><small>${t.name}</small></button>`;
    }).join('');
    body.innerHTML = `
      <div class="ts-top">
        <div class="ts-size"><button data-size="-1" aria-label="Smaller text">A</button><button data-size="1" aria-label="Bigger text">A</button></div>
        <button class="ts-toggle ${prefs.bold ? 'on' : ''}" data-bold aria-label="Bold text">B</button>
      </div>
      <div class="themes">${tiles}</div>
      <button class="cust-btn" data-custom>Customize${icon('chev')}</button>`;
  };
  const drawCustom = () => {
    const fonts = Object.entries(FONTS).map(([k, f]) => `<option value="${k}" ${prefs.font === k ? 'selected' : ''}>${f[0]}</option>`).join('');
    body.innerHTML = `
      <div class="opt">
        <div class="opt-row"><span>Font</span><select data-font>${fonts}</select></div>
        <div class="opt-row"><span>Line Spacing</span><input type="range" min="1.2" max="2.4" step="0.04" value="${prefs.spacing}" data-k="spacing"></div>
        <div class="opt-row"><span>Margins</span><input type="range" min="10" max="70" step="1" value="${prefs.margins}" data-k="margins"></div>
      </div>
      <div class="opt">
        <div class="opt-row"><span>Justify Text</span><button class="switch ${prefs.justify ? 'on' : ''}" data-sw="justify" aria-label="Justify text"></button></div>
        <div class="opt-row"><span>Bold Text</span><button class="switch ${prefs.bold ? 'on' : ''}" data-sw="bold" aria-label="Bold text"></button></div>
        <div class="opt-row"><span>Scrolling View</span><button class="switch ${prefs.scroll ? 'on' : ''}" data-sw="scroll" aria-label="Scrolling view"></button></div>
      </div>
      <button class="reset" data-reset>Reset Theme</button>
      <button class="cust-btn" data-back>${icon('back')}Themes</button>`;
  };
  const change = fn => {
    const pos = currentPos();
    fn();
    if (!prefs.scroll) R.page = 0;
    applyReaderLook(false);
    goToPos(pos, false);
  };
  draw();
  s.el.addEventListener('click', e => {
    const t = e.target.closest('[data-theme]');
    if (t) { change(() => { prefs.theme = t.dataset.theme; }); haptic(); draw(); return; }
    const z = e.target.closest('[data-size]');
    if (z) {
      const i = SIZES.findIndex(v => v >= prefs.size);
      const next = SIZES[clamp((i < 0 ? SIZES.length - 1 : i) + +z.dataset.size, 0, SIZES.length - 1)];
      change(() => { prefs.size = next; });
      haptic();
      return;
    }
    if (e.target.closest('[data-bold]')) { change(() => { prefs.bold = !prefs.bold; }); draw(); return; }
    if (e.target.closest('[data-custom]')) { $('h3', s.el).textContent = 'Customize'; drawCustom(); return; }
    if (e.target.closest('[data-back]')) { $('h3', s.el).textContent = 'Themes & Settings'; draw(); return; }
    const sw = e.target.closest('[data-sw]');
    if (sw) { change(() => { prefs[sw.dataset.sw] = !prefs[sw.dataset.sw]; }); sw.classList.toggle('on', !!prefs[sw.dataset.sw]); return; }
    if (e.target.closest('[data-reset]')) {
      change(() => { Object.assign(prefs, { theme: 'original', size: DEFAULTS.size, font: 'original', spacing: DEFAULTS.spacing, margins: DEFAULTS.margins, justify: false, bold: false }); });
      drawCustom();
    }
  });
  s.el.addEventListener('input', e => {
    const k = e.target.dataset.k;
    if (k) change(() => { prefs[k] = parseFloat(e.target.value); });
  });
  s.el.addEventListener('change', e => {
    if (e.target.matches('[data-font]')) change(() => { prefs.font = e.target.value; });
  });
}

/* ---------- share and rotation ---------- */

async function share() {
  const quote = textAtTop(220);
  const text = (quote ? `“${quote}…”\n\n` : '') + R.meta.title + (R.meta.author ? ` by ${R.meta.author}` : '');
  try {
    if (navigator.share) { await navigator.share({ text }); return; }
  } catch (e) { if (e.name === 'AbortError') return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied the passage');
  } catch (e) {
    toast('Sharing isn’t available here.');
  }
}

function toggleRotation() {
  if (!tg || !tg.isVersionAtLeast || !tg.isVersionAtLeast('8.0')) { toast('Update Telegram to lock rotation.'); return; }
  try {
    if (R.locked) { tg.unlockOrientation(); R.locked = false; toast('Rotation unlocked', 1400); }
    else { tg.lockOrientation(); R.locked = true; toast('Rotation locked', 1400); }
    $('#m-rotate').classList.toggle('on', R.locked);
  } catch (e) { toast('Rotation lock isn’t available here.'); }
}

/* ================= wiring ================= */

document.addEventListener('click', e => {
  if (R.open) return;
  const open = e.target.closest('[data-open]');
  if (open) { haptic(); openBook(+open.dataset.open); return; }
  const more = e.target.closest('[data-more]');
  if (more) { bookMenu(more, bookById(more.dataset.more)); return; }
  if (e.target.closest('[data-add]')) { pickFiles(); return; }
  const link = e.target.closest('[data-tab-link]');
  if (link) { showTab(link.dataset.tabLink); return; }
  const tb = e.target.closest('#tabbar button');
  if (tb) { haptic(); showTab(tb.dataset.tab); }
});
$('#btn-add').addEventListener('click', pickFiles);
$('#btn-view').addEventListener('click', () => { prefs.view = prefs.view === 'list' ? 'grid' : 'list'; savePrefs(); renderLibrary(); });
$('#btn-libmore').addEventListener('click', e => libraryMenu(e.currentTarget));
$('#file').addEventListener('change', e => { [...e.target.files].forEach(upload); });
$('#lib-q').addEventListener('input', renderSearch);

async function start() {
  setupTelegram();
  applyScheme();
  try {
    await login();
    const [list, saved] = await Promise.all([api('books'), api('prefs')]);
    books = list;
    prefs = { ...DEFAULTS, ...saved };
  } catch (e) {
    document.body.innerHTML = `<div class="gate"><div><b>Books</b>${esc(e.message)}</div></div>`;
    return;
  }
  renderAll();
  const last = new URLSearchParams(location.search).get('book');
  if (last && bookById(last)) openBook(+last);
}

window.__reader = { R, prefs: () => prefs, openBook, showContents, showThemes, showBookSearch, openReaderMenu };
start();
})();
