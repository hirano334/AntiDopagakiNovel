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

// 文の配列は「数値＝単語（surfId）」「文字列＝空白・句読点」の交互
function render() {
  const ch = book.chunks[cur];
  let html = '';
  ch.p.forEach((para, pi) => {
    html += `<p class="k-${para.k}${para.c ? ' c' : ''}">`;
    para.s.forEach((arr, si) => {
      html += `<span class="sen" data-p="${pi}" data-s="${si}">`;
      arr.forEach((x, ti) => {
        if (typeof x === 'number') {
          html += `<span class="w" data-t="${ti}">${esc(book.surf[x])}</span>`;
        } else {
          html += esc(x);
        }
      });
      html += '</span>' + (si < para.s.length - 1 ? ' ' : '');
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

function showWord(w) {
  const sen = w.parentElement;
  const p = +sen.dataset.p, s = +sen.dataset.s, t = +w.dataset.t;
  const fid = book.sf[book.chunks[cur].p[p].s[s][t]];

  text.querySelectorAll('.w.on, .w.ph').forEach((e) => e.classList.remove('on', 'ph'));
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

  for (const [lemma, pos, form, keys] of book.forms[fid]) {
    h += `<div class="grp"><div class="hw">${esc(lemma)}</div>`;
    h += `<div class="meta">${esc(pos)}${form ? ' · ' + esc(form) : ''}</div>`;
    h += keys.length ? glossHtml(keys) : '<div class="none">辞書に見出しなし</div>';
    h += '</div>';
  }
  sheet.innerHTML = h;
  sheet.hidden = false;
  sheet.scrollTop = 0;

  // タップした語がシートに隠れるなら本文をずらす
  const r = w.getBoundingClientRect();
  const limit = sheet.getBoundingClientRect().top - 12;
  if (r.bottom > limit) page.scrollTop += r.bottom - limit + r.height;
}

function closeSheet() {
  sheet.hidden = true;
  text.querySelectorAll('.w.on, .w.ph').forEach((e) => e.classList.remove('on', 'ph'));
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
  if (sheet.contains(el)) return;
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
}
