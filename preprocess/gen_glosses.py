"""辞書（Wiktionary）に見出しのない語の語義を、ビルド時に一度だけ Claude で生成する。

入力: app/books/karamazov.json（build_book.py の出力）
出力: data/llm_glosses.json  {見出し語(正規化): {"pos", "g": [...], "note"}}

既に出力にある見出し語は送らないので、何度実行しても追加分だけ生成する。
生成後にもう一度 build_book.py を実行すると本の JSON に入る。

  python preprocess/gen_glosses.py --limit 40   # 1リクエスト分だけ試す
  python preprocess/gen_glosses.py
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "app/books/karamazov.json"
OUT = ROOT / "data/llm_glosses.json"

# 辞書にない古語・方言などが相手なので Haiku より Sonnet。思考は切って費用を抑える（全件で約2ドルの見込み）
MODEL = "claude-sonnet-5-5"
PRICE_IN, PRICE_OUT = 2.0, 10.0   # ドル / 100万トークン
BATCH = 40
MAX_CONTEXTS = 2
CTX_WINDOW = 150                  # 文脈は語の前後この字数だけ送る
ROMAN_RE = re.compile(r"^[ivxlc]+$")

SYSTEM = """You help a Japanese learner (intermediate Russian, about CEFR B1) read Dostoevsky's \
"The Brothers Karamazov" in the original. The reader's dictionary (English Wiktionary) has no entry \
for the words below. For each item you get the headword guessed by a morphological analyzer \
(it may be wrong), the form as it appears in the text, and one or two sentences where it occurs.

For each item return:
- lemma: copy the given headword exactly, even if it is wrong.
- pos: English part of speech (noun, verb, adjective, adverb, pronoun, particle, interjection, \
phrase, foreign word, ...).
- glosses: 1-3 short English glosses, the meaning in this context first. Glosses only - never \
translate the whole sentence.
- note: one short English note when useful, otherwise an empty string. Say if the word is archaic, \
dialectal, colloquial, a diminutive (of what), a nonstandard spelling (of what), or foreign \
(which language, and what it means). If the analyzer's headword is wrong, give the correct one \
(e.g. "form of the surname Хохлакова").
If you are not reasonably sure of the meaning, return an empty glosses list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lemma": {"type": "string"},
                    "pos": {"type": "string"},
                    "glosses": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["lemma", "pos", "glosses", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entries"],
    "additionalProperties": False,
}


def norm(s):
    return s.lower().replace("ё", "е")


def base_lemma(display):
    # 形動詞は「сделанный ← сделать」の形で表示している。build_book.py は右側（normal_form）で引く
    return display.split(" ← ")[-1]


def snippet(text, words):
    """文が長いときは、その語の前後 CTX_WINDOW 字だけにする。"""
    if len(text) <= CTX_WINDOW * 2:
        return text
    i = max(0, text.find(words[0]) if words else 0)
    a, b = max(0, i - CTX_WINDOW), min(len(text), i + CTX_WINDOW)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def collect(book):
    """辞書キーのない候補を見出し語ごとに集め、表層形と文脈の文を付ける。"""
    missing = {}   # 正規化した見出し語 → {"lemma", "forms": set, "ctx": []}
    for sid, fid in enumerate(book["sf"]):
        for lemma, pos, _form, keys in book["forms"][fid]:
            if keys or "固有名詞" in pos:
                continue
            key = norm(base_lemma(lemma))
            if ROMAN_RE.match(key) or len(key) < 2:
                continue
            m = missing.setdefault(key, {"lemma": base_lemma(lemma), "sids": set(), "ctx": []})
            m["sids"].add(sid)

    by_sid = defaultdict(list)
    for key, m in missing.items():
        for sid in m["sids"]:
            by_sid[sid].append(key)
    for c in book["chunks"]:
        for p in c["p"]:
            for arr in p["s"]:
                hits = [k for x in arr if isinstance(x, int) for k in by_sid.get(x, ())]
                if not hits:
                    continue
                text = "".join(book["surf"][x] if isinstance(x, int) else x
                               for x in arr if not isinstance(x, dict))
                for k in hits:
                    ctx = missing[k]["ctx"]
                    if len(ctx) >= MAX_CONTEXTS:
                        continue
                    snip = snippet(text, [book["surf"][x] for x in arr
                                          if isinstance(x, int) and k in by_sid.get(x, ())])
                    if snip not in ctx:
                        ctx.append(snip)
    for m in missing.values():
        m["forms"] = sorted({book["surf"][s] for s in m["sids"]})
    return missing


def ask(client, items):
    lines = []
    for i, m in enumerate(items, 1):
        lines.append(f"{i}. headword: {m['lemma']}  |  in text: {', '.join(m['forms'][:3])}")
        for c in m["ctx"]:
            lines.append(f"   - {c}")
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "between_tools"},   # Sonnet 5.5 で思考を切る指定
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    cost = (resp.usage.input_tokens * PRICE_IN + resp.usage.output_tokens * PRICE_OUT) / 1e6
    if resp.stop_reason != "end_turn":
        print(f"  stop_reason={resp.stop_reason}、このまとまりはとばす", file=sys.stderr)
        return [], cost
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["entries"], cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="送る見出し語の最大数（試し用）")
    ap.add_argument("--budget", type=float, default=3.0, help="この金額（ドル）に達したら止める")
    args = ap.parse_args()

    book = json.load(open(BOOK, encoding="utf-8"))
    done = json.load(open(OUT, encoding="utf-8")) if OUT.exists() else {}
    missing = collect(book)
    todo = [m for k, m in sorted(missing.items()) if k not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"辞書にない見出し語={len(missing)} 生成済み={len(done)} 今回={len(todo)}", file=sys.stderr)

    client = anthropic.Anthropic()
    spent = 0.0
    for i in range(0, len(todo), BATCH):
        if spent >= args.budget:
            print(f"  予算 {args.budget} ドルに達したので止める（再実行で続きから）", file=sys.stderr)
            break
        items = todo[i:i + BATCH]
        try:
            entries, cost = ask(client, items)
            spent += cost
        except anthropic.APIStatusError as e:
            print(f"  API エラー {e.status_code}: {e.message}、ここで止める（再実行で続きから）", file=sys.stderr)
            break
        wanted = {norm(m["lemma"]) for m in items}
        for e in entries:
            key = norm(e["lemma"])
            if key in wanted:
                done[key] = {"pos": e["pos"], "g": e["glosses"], "note": e["note"]}
        # 1リクエストごとに保存する（途中で止まっても払った分を失わない）
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"  {min(i + BATCH, len(todo))}/{len(todo)}  累計 {spent:.2f} ドル", file=sys.stderr)


if __name__ == "__main__":
    main()
