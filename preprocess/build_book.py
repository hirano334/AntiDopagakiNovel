"""fb2 → app/books/<name>.json

本文・チャンク分割・トークン（formId）・解析結果・辞書サブセット・成句を1つのJSONにまとめる。
事前に build_dict.py を実行して data/work/*.json を作っておくこと。

  python preprocess/build_book.py                # 全巻
  python preprocess/build_book.py --chapters 2   # 先頭2章だけ（動作確認用）
"""
import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pymorphy3
from razdel import sentenize, tokenize

from grammar_ja import form_ja, pos_ja

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "data/work"
NS = "{http://www.gribuser.ru/xml/fictionbook/2.0}"

TARGET = 450          # チャンクの目安文字数
MAX_GROUPS = 3        # 1語に表示する解析候補（見出し語×品詞）の最大数
MIN_SCORE = 0.05      # これ未満の候補は捨てる（先頭候補は残す）
PHRASE_MAX = 5
# 語の意味の足し算にすぎず、成句として出すとかえって邪魔なもの
PHRASE_STOP = {"что это", "и так", "и то", "даже так", "все вместе", "не так", "а то", "да и"}
# 強調の小詞。辞書にない「он-то」などは本体の語で引く
PARTICLE_SUFFIXES = ("-то", "-с", "-ка", "-де", "-таки")
STUTTER_RE = re.compile(r"^([а-яё])-(?=\1)")          # в-врешь, н-не
STRETCH_RE = re.compile(r"([аеёиоуыэюя])(?:-\1)+")     # да-а-а


def spelling_variants(key):
    """辞書に引けない表記を、辞書に引ける形に戻す候補を (語, 注記) で返す。"""
    for suf in PARTICLE_SUFFIXES:
        if key.endswith(suf) and len(key) > len(suf):
            yield key[: -len(suf)], f"＋{suf}"
    if "-" not in key:
        return
    k = key
    while STUTTER_RE.match(k):
        k = STUTTER_RE.sub("", k)
    if k != key:
        yield k, "（どもりの表記）"
    k2 = STRETCH_RE.sub(r"\1", k)
    if k2 != k:
        yield k2, "（引き伸ばしの表記）"
    yield k2.replace("-", ""), "（区切った表記）"

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
NOTE_RE = re.compile(r"\[\d+\]")
# 原注の参照。本文中では私用領域の文字で囲んだ番号として持ち、文配列にするときに {"n": 番号} に変える
MARK_RE = re.compile("\ue000(\\d+)\ue001")


def norm(s):
    return s.lower().replace("ё", "е")


# ---------- fb2 ----------

def text_of(el, notes=True):
    def walk(e):
        if tag(e) == "a" and e.get("type") == "note":
            num = re.sub(r"\D", "", e.get("{http://www.w3.org/1999/xlink}href", ""))
            out = f"\ue000{num}\ue001" if notes and num else ""
        else:
            out = (e.text or "") + "".join(walk(c) for c in e)
        return out + (e.tail or "")
    t = (el.text or "") + "".join(walk(c) for c in el)
    t = NOTE_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def tag(el):
    return el.tag.replace(NS, "")


def blocks(el):
    """section 以外の子要素から (kind, text) を文書順に取り出す。"""
    out = []
    for c in el:
        t = tag(c)
        if t == "section":
            continue
        if t == "title":
            for p in c.iter(NS + "p"):
                txt = text_of(p)
                if txt:
                    out.append(("h", txt))
        elif t == "p":
            txt = text_of(c)
            if txt:
                out.append(("p", txt))
        elif t in ("epigraph", "cite", "poem", "stanza"):
            for sub in blocks(c):
                out.append(("v" if sub[0] == "p" else sub[0], sub[1]))
        elif t == "v":
            txt = text_of(c)
            if txt:
                out.append(("v", txt))
        elif t == "text-author":
            txt = text_of(c)
            if txt:
                out.append(("v", txt))
    return out


def title_of(sec):
    t = sec.find(NS + "title")
    if t is None:
        return ""
    return ". ".join(text_of(p, notes=False) for p in t.iter(NS + "p"))


def parse_fb2(path):
    root = ET.parse(path).getroot()
    body = next(b for b in root.iter(NS + "body") if b.get("name") is None)
    chapters = []
    pending = blocks(body)   # 本全体のエピグラフなど。次の章の先頭に付ける

    def walk(sec, path_titles):
        nonlocal pending
        subs = sec.findall(NS + "section")
        title = title_of(sec)
        if not subs:
            chapters.append({
                "title": " / ".join(path_titles[-1:] + [title]) if path_titles else title,
                "paras": pending + blocks(sec),
            })
            pending = []
            return
        pending = pending + blocks(sec)
        for s in subs:
            walk(s, path_titles + [title])

    for sec in body.findall(NS + "section"):
        walk(sec, [])

    notes = {}
    for b in root.iter(NS + "body"):
        if b.get("name") != "notes":
            continue
        for sec in b.iter(NS + "section"):
            num = re.sub(r"\D", "", sec.get("id", ""))
            paras = [text_of(p, notes=False) for p in sec.findall(NS + "p")]
            if num:
                notes[num] = [x for x in paras if x]
    return chapters, notes


# ---------- 形態素解析 ----------

class Analyzer:
    def __init__(self, dic, formof, llm=None):
        self.morph = pymorphy3.MorphAnalyzer()
        self.dic = dic
        self.formof = formof
        self.llm = llm or {}   # gen_glosses.py で生成した、辞書にない語の語義
        self.form_ids = {}
        self.forms = []
        self.lemma_sets = []   # formId → 見出し語（正規化）の集合。成句照合用
        self.used_keys = set()
        self.covered = []      # formId → 辞書に何かヒットしたか
        self.surf_ids = {}     # 本文の表記（大文字小文字そのまま）→ surfId
        self.surf = []         # surfId → 表記
        self.sf = []           # surfId → formId

    def token_id(self, word):
        sid = self.surf_ids.get(word)
        if sid is None:
            sid = len(self.surf)
            self.surf_ids[word] = sid
            self.surf.append(word)
            self.sf.append(self.form_id(word))
        return sid

    def form_id(self, word):
        key = word.lower()
        fid = self.form_ids.get(key)
        if fid is None:
            fid = len(self.forms)
            self.form_ids[key] = fid
            groups, lemmas = self.analyze(key)
            if not any(g[3] for g in groups):
                for variant, label in spelling_variants(key):
                    g2, l2 = self.analyze(variant)
                    if any(g[3] for g in g2):
                        for g in g2:
                            g[2] = (g[2] + " " + label).strip()
                        groups, lemmas = g2, l2
                        break
            self.forms.append(groups)
            self.lemma_sets.append(lemmas)
            self.covered.append(any(g[3] for g in groups))
        return fid

    def lookup_keys(self, p, surface):
        keys = []
        if p.tag.POS == "PRTF":
            nom = p.inflect({"masc", "sing", "nomn"})
            if nom:
                keys.append(nom.word)
        if p.tag.POS in ("GRND", "COMP", "PRTS"):
            keys.append(surface)
        keys.append(p.normal_form)
        found = [k for k in dict.fromkeys(norm(k) for k in keys) if k in self.dic]
        if not found:
            s = norm(surface)
            if s in self.dic:
                found = [s]
            else:
                found = [k for k in self.formof.get(s, []) if k in self.dic]
        if not found:
            k = norm(p.normal_form)
            e = self.llm.get(k)
            if e and e["g"]:
                self.dic["ai:" + k] = [{"pos": e["pos"], "g": e["g"], "note": e["note"], "ai": 1}]
                found = ["ai:" + k]
        return found[:2]

    def analyze(self, surface):
        groups = {}
        for p in self.morph.parse(surface):
            k = (p.normal_form, p.tag.POS)
            g = groups.setdefault(k, {"score": 0.0, "p": p, "forms": []})
            g["score"] += p.score
            f = form_ja(p.tag)
            if f and f not in g["forms"]:
                g["forms"].append(f)
        ranked = sorted(groups.values(), key=lambda g: -g["score"])
        ranked = [g for i, g in enumerate(ranked) if i == 0 or g["score"] >= MIN_SCORE][:MAX_GROUPS]
        out, lemmas = [], set()
        for g in ranked:
            p = g["p"]
            keys = self.lookup_keys(p, surface)
            self.used_keys.update(keys)
            lemma = p.normal_form
            if p.tag.POS == "PRTF":
                nom = p.inflect({"masc", "sing", "nomn"})
                if nom and nom.word != lemma:
                    lemma = f"{nom.word} ← {lemma}"
            out.append([lemma, pos_ja(p.tag), " / ".join(g["forms"][:3]), keys])
            lemmas.add(norm(p.normal_form))
        return out, lemmas


# ---------- 成句 ----------

class PhraseMatcher:
    def __init__(self, phrases, morph):
        self.phrases = phrases
        self.surface = {}   # 先頭語 → [語のタプル]
        self.lemma = {}     # 先頭見出し語 → [(見出し語のタプル, key)]
        for key in phrases:
            words = key.split()
            if key in PHRASE_STOP or not all(re.fullmatch(r"[а-я-]+", w) for w in words):
                continue
            self.surface.setdefault(words[0], []).append((tuple(words), key))
            if len(words) >= 3:
                lem = tuple(norm(morph.parse(w)[0].normal_form) for w in words)
                self.lemma.setdefault(lem[0], []).append((lem, key))
        self.ids = {}
        self.list = []

    def pid(self, key):
        if key not in self.ids:
            self.ids[key] = len(self.list)
            self.list.append({"t": key, "e": self.phrases[key]})
        return self.ids[key]

    def match(self, words, lemma_sets):
        """words: [(配列index, 正規化表層形, 直前が空白だけか)]。戻り値 [(開始i, 終了i, key)]（words上の位置）"""
        res, i, n = [], 0, len(words)
        while i < n:
            best = None
            for cand, key in self.surface.get(words[i][1], []):
                L = len(cand)
                if L > best_len(best) and self._fits(words, i, L) and \
                        all(words[i + j][1] == cand[j] for j in range(L)):
                    best = (L, key)
            for lset in lemma_sets[i]:
                for cand, key in self.lemma.get(lset, []):
                    L = len(cand)
                    if L > best_len(best) and self._fits(words, i, L) and \
                            all(cand[j] in lemma_sets[i + j] for j in range(L)):
                        best = (L, key)
            if best:
                res.append((i, i + best[0] - 1, best[1]))
                i += best[0]
            else:
                i += 1
        return res

    @staticmethod
    def _fits(words, i, L):
        return L <= PHRASE_MAX and i + L <= len(words) and \
            all(words[i + j][2] for j in range(1, L))


def best_len(b):
    return b[0] if b else 1


# ---------- 本文 → 文配列 ----------

def sentence_array(text, an):
    """文を「整数(surfId)＝単語」「文字列＝その他」「{"n": 番号}＝原注の参照」の配列にする。"""
    arr, buf = [], ""
    pieces = MARK_RE.split(text)   # [本文, 注番号, 本文, 注番号, …]
    for i, piece in enumerate(pieces):
        if i % 2:
            if buf:
                arr.append(buf)
                buf = ""
            arr.append({"n": int(piece)})
            continue
        pos = 0
        for t in tokenize(piece):
            buf += piece[pos:t.start]
            pos = t.stop
            if WORD_RE.search(t.text):
                if buf:
                    arr.append(buf)
                    buf = ""
                arr.append(an.token_id(t.text))
            else:
                buf += t.text
        buf += piece[pos:]
    if buf:
        arr.append(buf)
    return arr


def split_balanced(sents, lens):
    total = sum(lens)
    n = max(1, math.ceil(total / TARGET))
    per = total / n
    parts, cur, acc = [], [], 0
    for s, L in zip(sents, lens):
        if cur and acc + L / 2 > per * (len(parts) + 1) and len(parts) < n - 1:
            parts.append(cur)
            cur = []
        cur.append(s)
        acc += L
    parts.append(cur)
    return parts


def chunk_chapter(ci, paras, an):
    chunks, cur, cur_len = [], [], 0

    def flush():
        nonlocal cur, cur_len
        if cur:
            chunks.append({"ch": ci, "p": cur})
        cur, cur_len = [], 0

    for kind, text in paras:
        sents = [s.text for s in sentenize(text)]
        lens = [len(s) for s in sents]
        plen = sum(lens)
        if kind == "h":
            if any(x["k"] != "h" for x in cur):
                flush()
            cur.append({"k": "h", "s": [sentence_array(" ".join(sents), an)]})
            continue
        if cur_len and cur_len + plen > TARGET:
            flush()
        parts = split_balanced(sents, lens) if plen > TARGET else [sents]
        for j, part in enumerate(parts):
            if j > 0:
                flush()
            para = {"k": kind, "s": [sentence_array(s, an) for s in part]}
            if j > 0:
                para["c"] = 1   # 前のチャンクからの続き（字下げしない）
            cur.append(para)
            cur_len += sum(len(s) for s in part)
    flush()
    return chunks


def find_spans(chunk, an, pm):
    spans = []
    for pi, para in enumerate(chunk["p"]):
        for si, arr in enumerate(para["s"]):
            words, lsets, prev_space = [], [], True
            for ai, x in enumerate(arr):
                if isinstance(x, int):
                    words.append((ai, norm(an.surf[x]), prev_space))
                    lsets.append(an.lemma_sets[an.sf[x]])
                    prev_space = True
                elif isinstance(x, str):
                    prev_space = x.strip() == ""
            for a, b, key in pm.match(words, lsets):
                spans.append([pi, si, words[a][0], words[b][0], pm.pid(key)])
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fb2", default=str(ROOT / "data/raw/Dostoevskiyi_F._Bratya_Karamazovyi.fb2"))
    ap.add_argument("--out", default=str(ROOT / "app/books/karamazov.json"))
    ap.add_argument("--chapters", type=int, default=0)
    args = ap.parse_args()

    print("辞書を読み込み中…", file=sys.stderr)
    dic = json.load(open(WORK / "dict_ru.json", encoding="utf-8"))
    formof = json.load(open(WORK / "formof_ru.json", encoding="utf-8"))
    phrases = json.load(open(WORK / "phrases.json", encoding="utf-8"))

    llm_path = ROOT / "data/llm_glosses.json"
    llm = json.load(open(llm_path, encoding="utf-8")) if llm_path.exists() else {}
    an = Analyzer(dic, formof, llm)
    pm = PhraseMatcher(phrases, an.morph)

    chapters, notes = parse_fb2(args.fb2)
    if args.chapters:
        chapters = chapters[: args.chapters]

    toc, chunks = [], []
    for ci, ch in enumerate(chapters):
        toc.append({"t": ch["title"], "c": len(chunks)})
        chunks.extend(chunk_chapter(ci, ch["paras"], an))
        print(f"{ci + 1}/{len(chapters)} {ch['title'][:50]}", file=sys.stderr)

    spans = {}
    tok_count, miss = 0, Counter()
    for i, c in enumerate(chunks):
        sp = find_spans(c, an, pm)
        if sp:
            spans[i] = sp
        for para in c["p"]:
            for arr in para["s"]:
                for x in arr:
                    if isinstance(x, int):
                        tok_count += 1
                        if not an.covered[an.sf[x]]:
                            miss[an.surf[x].lower()] += 1

    book = {
        "title": "Братья Карамазовы",
        "toc": toc,
        "chunks": chunks,
        "surf": an.surf,
        "sf": an.sf,
        "forms": an.forms,
        "dict": {k: dic[k] for k in sorted(an.used_keys)},
        "phr": pm.list,
        "notes": {k: [sentence_array(s.text, an) for p in v for s in sentenize(p)]
                  for k, v in notes.items()},
        "spans": spans,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, separators=(",", ":"))

    n_miss = sum(miss.values())
    n_spans = sum(len(v) for v in spans.values())
    print(f"章={len(toc)} チャンク={len(chunks)} トークン={tok_count} 語形={len(an.forms)} "
          f"辞書見出し={len(book['dict'])} 成句={len(pm.list)}種/{n_spans}箇所")
    print(f"辞書ヒット率={(1 - n_miss / max(tok_count, 1)) * 100:.1f}%  "
          f"サイズ={Path(args.out).stat().st_size / 1e6:.1f}MB")
    print("未ヒット上位:", " ".join(f"{w}({n})" for w, n in miss.most_common(50)))


if __name__ == "__main__":
    main()
