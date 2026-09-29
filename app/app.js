'use strict';

const BOOK = 'karamazov';
const POS_KEY = 'pos:' + BOOK;

const $ = (id) => document.getElementById(id);
const page = $('page'), text = $('text'), sheet = $('sheet'), toc = $('toc'), where = $('where');

let book = null;
let cur = 0;          // 表示中のチャンク
let hl = null;        // ハイライトする文 {p, s}

function loadPos() {
  try { return JSON.parse(localStorage.getItem(POS_KEY)) || { c: 0, p: 0, s: 0 }; }
  catch (e) { return { c: 0, p: 0, s: 0 }; }
}
function savePos(p, s) {
  try { localStorage.setItem(POS_KEY, JSON.stringify({ c: cur, p, s })); } catch (e) { }
}

function esc(s) {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// 文の配列の要素は「数値＝単語（surfId）」「文字列＝空白・句読点」「{n}＝原注の参照」。
// 本文の単語は文中の位置 data-t、原注の中の単語は surfId そのもの data-x で識別する。
function sentHtml(arr, inNote) {
  let h = '';
  arr.forEach((x, ti) => {
    if (typeof x === 'number') {
      h += inNote ? `<span class="w" data-x="${x}" data-i="${ti}">` : `<span class="w" data-t="${ti}">`;
      h += esc(book.surf[x]) + '</span>';
    } else if (typeof x === 'string') {
      h += esc(x);
    } else if (!inNote) {
      h += `<sup class="nt" data-n="${x.n}">${x.n}</sup>`;
    }
  });
  return h;
}

function render() {
  const ch = book.chunks[cur];
  let html = '';
  ch.p.forEach((para, pi) => {
    html += `<p class="k-${para.k}${para.c ? ' c' : ''}">`;
    para.s.forEach((arr, si) => {
      html += `<span class="sen" data-p="${pi}" data-s="${si}">${sentHtml(arr, false)}</span>` + (si < para.s.length - 1 ? ' ' : '');
    });
    html += '</p>';
  });
  text.innerHTML = html;
  if (hl) {
    const el = text.querySelector(`.sen[data-p="${hl.p}"][data-s="${hl.s}"]`);
    if (el) el.classList.add('cur');
  }
  page.scrollTop = 0;
  const t = book.toc[ch.ch];
  where.textContent = `${t.t} · ${cur + 1}/${book.chunks.length}`;
}

function go(n) {
  if (n < 0 || n >= book.chunks.length) return;
  cur = n;
  hl = null;
  closeSheet();
  render();
  savePos(0, 0);
}

// ---------- 辞書シート ----------

function glossHtml(keys) {
  let h = '';
  for (const k of keys) {
    for (const e of book.dict[k] || []) {
      h += `<div><span class="pos">${esc(e.pos)}</span>${k !== keys[0] ? esc(k) : ''}</div>`;
      h += '<ol>' + e.g.map((g) => `<li>${esc(g)}</li>`).join('') + '</ol>';
    }
  }
  return h;
}

function findPhrase(p, s, t) {
  const sp = book.spans[cur];
  if (!sp) return null;
  return sp.find((x) => x[0] === p && x[1] === s && x[2] <= t && t <= x[3]) || null;
}

function wordHtml(sid) {
  let h = '';
  for (const [lemma, pos, form, keys] of book.forms[book.sf[sid]]) {
    h += `<div class="grp"><div class="hw">${esc(lemma)}</div>`;
    h += `<div class="meta">${esc(pos)}${form ? ' · ' + esc(form) : ''}</div>`;
    h += keys.length ? glossHtml(keys) : '<div class="none">辞書に見出しなし</div>';
    h += '</div>';
  }
  return h;
}

// シートを開き、タップした箇所（anchor）がシートに隠れるなら本文をずらす
function openSheet(h, anchor) {
  sheet.innerHTML = h;
  sheet.hidden = false;
  sheet.scrollTop = 0;
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();
  const limit = sheet.getBoundingClientRect().top - 12;
  if (r.bottom > limit) page.scrollTop += r.bottom - limit + r.height;
}

function clearMarks() {
  text.querySelectorAll('.w.on, .w.ph, .nt.on').forEach((e) => e.classList.remove('on', 'ph'));
}

function showWord(w) {
  const sen = w.parentElement;
  const p = +sen.dataset.p, s = +sen.dataset.s, t = +w.dataset.t;
  const sid = book.chunks[cur].p[p].s[s][t];

  clearMarks();
  text.querySelectorAll('.sen.cur').forEach((e) => e.classList.remove('cur'));
  sen.classList.add('cur');
  hl = { p, s };
  savePos(p, s);

  let h = '';
  const span = findPhrase(p, s, t);
  if (span) {
    const ph = book.phr[span[4]];
    for (let i = span[2]; i <= span[3]; i++) {
      const e = sen.querySelector(`.w[data-t="${i}"]`);
      if (e) e.classList.add('ph');
    }
    h += `<div class="phr"><div class="hw">${esc(ph.t)}</div>`;
    for (const e of ph.e) {
      h += `<div><span class="pos">${esc(e.pos)}</span></div><ol>` +
        e.g.map((g) => `<li>${esc(g)}</li>`).join('') + '</ol>';
    }
    h += '</div>';
  }
  w.classList.add('on');
  openSheet(h + wordHtml(sid), w);
}

// 原注。注の中の単語もタップでき、その語の意味を注の下に出す（sid, i がタップした語）
function showNote(n, sup, sid, i) {
  let h = `<div class="note"><div class="meta">原注 ${n}</div><div class="ntext" data-n="${n}">`;
  h += (book.notes[n] || []).map((arr) => sentHtml(arr, true)).join(' ') + '</div></div>';
  if (sid !== undefined) h += wordHtml(sid);
  if (sup) {
    clearMarks();
    sup.classList.add('on');
  }
  openSheet(h, sup);
  if (sid !== undefined) sheet.querySelector(`.w[data-i="${i}"]`).classList.add('on');
}

function closeSheet() {
  sheet.hidden = true;
  clearMarks();
}

// ---------- 目次 ----------

function openToc() {
  const now = book.chunks[cur].ch;
  toc.querySelector('ol').innerHTML = book.toc
    .map((t, i) => `<li data-i="${i}"${i === now ? ' class="now"' : ''}>${esc(t.t)}</li>`).join('');
  toc.hidden = false;
  const li = toc.querySelector('li.now');
  if (li) li.scrollIntoView({ block: 'center' });
}

// ---------- 入力 ----------

document.addEventListener('click', (ev) => {
  if (!book) return;
  const el = ev.target;
  if (el === where) { toc.hidden ? openToc() : (toc.hidden = true); return; }
  if (!toc.hidden) {
    const li = el.closest('li[data-i]');
    if (li) { toc.hidden = true; go(book.toc[+li.dataset.i].c); }
    return;
  }
  if (sheet.contains(el)) {
    if (el.classList.contains('w') && el.dataset.x) {
      showNote(el.closest('.ntext').dataset.n, null, +el.dataset.x, el.dataset.i);
    }
    return;
  }
  if (el.classList.contains('nt')) { showNote(el.dataset.n, el); return; }
  if (el.id === 'navR') { go(cur + 1); return; }
  if (el.id === 'navL') { go(cur - 1); return; }
  if (el.classList.contains('w')) { showWord(el); return; }
  if (!sheet.hidden) { closeSheet(); return; }
  // 本文より下の余白 → 次へ
  if (ev.clientY > text.getBoundingClientRect().bottom) go(cur + 1);
});

// ---------- 起動 ----------

async function start() {
  const res = await fetch(`books/${BOOK}.json`);
  book = await res.json();
  const pos = loadPos();
  cur = Math.min(pos.c, book.chunks.length - 1);
  hl = { p: pos.p, s: pos.s };
  render();
  const el = text.querySelector('.sen.cur');
  if (el) el.scrollIntoView({ block: 'center' });
}

start().catch((e) => { text.innerHTML = `<p class="msg">${esc(String(e))}</p>`; });

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => { });
  // 端末の空き容量が減ってもキャッシュを消されないようにする
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist();
}
