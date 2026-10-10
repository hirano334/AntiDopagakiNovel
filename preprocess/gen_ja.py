"""日本語のデータ（辞書の和訳・箇所ごとのルビ・章のあらすじ）をビルド時に Claude で生成する。

API ではなく Claude Code（`claude -p`）を呼ぶので、費用はサブスクの利用枠から出る。
入力: app/books/karamazov.json（build_book.py の出力）
出力: data/ja_dict.json     {"辞書キー#エントリ番号": {"g": [...], "note": "..."}}   成句は "phr:成句#番号"
      data/ja_ruby.json     {チャンクの識別子: [[段落, 文, 文中の位置, 訳語], ...]}
      data/ja_summary.json  {"章番号": "あらすじ"}

済んだ分は飛ばし、1バッチごとに保存する。利用上限などで止まったら、再実行すれば続きから生成する。
生成後にもう一度 build_book.py を実行すると本の JSON に入る。

  python preprocess/gen_ja.py ruby --limit 3     # 3バッチだけ試す
  python preprocess/gen_ja.py dict --jobs 4
  python preprocess/gen_ja.py summary
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from build_book import chunk_key

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "app/books/karamazov.json"
DATA = ROOT / "data"

MODEL = "sonnet"
RUBY_MIN_LEVEL = "3"      # B1 以上（build_book.py の lv）にルビを付ける
RUBY_TARGETS = 450        # ルビの1バッチに入れる対象語の目安
RUBY_MAX_LEN = 10
DICT_BATCH = 200
LINE_RE = re.compile(r"^\s*(\d+)(n?)\s*=\s*(.+?)\s*$")

READER = """The reader is a Japanese learner of Russian (about CEFR A2-B1) reading Dostoevsky's \
"The Brothers Karamazov" in the original. """

SYS_RUBY = READER + """Unknown words get a tiny Japanese gloss printed above them (ruby), so that \
the reader can keep the meaning in view and spend attention on the grammar.

You get a passage. Some words are followed by a marker like ⟦12⟧. For every marker, give the \
Japanese gloss of that word AS USED IN THAT SENTENCE.
- One short Japanese word or phrase, 6 characters or fewer if at all possible. No explanations.
- Dictionary form: verbs in 終止形 (e.g. 尋ねる), adjectives in 終止形/連体形, nouns bare. Do not \
reproduce tense, case or number.
- If the word is part of a set phrase or idiom, give the meaning it has there.
- Archaic, church or dialect words: give the plain modern Japanese meaning.
- If a gloss would not help (a personal or place name, a particle with no translatable meaning, \
a stutter), answer with a single hyphen.

Output one line per marker, in order, in the form `number=gloss`, and nothing else. \
Every marker number must appear exactly once."""

SYS_DICT = READER + """You are writing the Japanese side of a concise Russian-Japanese dictionary \
that is shown when the reader taps a word.

Each numbered item is one dictionary entry: Russian headword, [part of speech], and English \
glosses from Wiktionary separated by " ; ". Translate each entry into Japanese.
- 1 to 4 short Japanese glosses per entry, most common sense first, separated by "｜". \
Cover the distinct senses of the English; merge near-synonyms.
- Write in the style of a 露和辞典: bare equivalents (e.g. 尋ねる｜頼む), no sentences. \
Usage labels go in 《》 before the gloss, e.g. 《口語》, 《古》, 《卑》. Verbs in 終止形.
- Use your own knowledge of Russian to choose the right Japanese word; the English only tells \
you which senses to cover. Glosses like "diminutive of X" or "alternative form of X" become \
"X の指小形" / "X の別形" plus the meaning when you know it.
- If an item has a line starting with "note:", also translate that note into natural Japanese.

Output one line per item in the form `number=gloss｜gloss`, and for an item with a note one more \
line `number` + `n=` + the note in Japanese (e.g. `17n=адвокат の俗な発音`). Nothing else. \
Every item number must appear."""

SYS_SUMMARY = READER + """Before each chapter the reader sees a short Japanese orientation, so \
that they know where the chapter is heading and can guess meanings from context.

You get the full Russian text of one chapter. Write the orientation in Japanese:
- 2 or 3 short lines (about 60-110 characters in total), plain である/だ style.
- Convey the DIRECTION of the chapter, not its content: who is on stage, what the scene is \
about, what kind of talk or mood to expect (e.g. a quarrel, a confession, a theological debate, \
a courtroom speech).
- Do not retell events and do not reveal how the chapter turns out.
- Names in the usual Japanese forms (アリョーシャ, ドミートリイ, イワン, フョードル, ゾシマ長老 ...).

Output only the orientation text."""


class Stop(Exception):
    """利用上限など、続けても無駄な失敗。"""


def call_claude(system, prompt):
    """`claude -p` を1回呼び、(本文, 出力トークン数, 秒) を返す。"""
    # API キーがあるとサブスクではなく API 課金になるので外す
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    t0 = time.time()
    with tempfile.TemporaryDirectory() as cwd:   # このリポジトリの CLAUDE.md を読ませない
        r = subprocess.run(
            ["claude", "-p", "--model", MODEL, "--effort", "low", "--output-format", "json",
             "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
             "--no-session-persistence", "--system-prompt", system],
            input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=1800)
    try:
        res = json.loads(r.stdout)
    except ValueError:
        raise Stop(f"claude の出力が読めない (exit {r.returncode}): {(r.stdout or r.stderr)[:300]}")
    if r.returncode != 0 or res.get("is_error"):
        raise Stop(str(res.get("result") or res)[:300])
    return res["result"], res.get("usage", {}).get("output_tokens", 0), time.time() - t0


def parse_lines(text):
    """`番号=訳` の行を {番号: 訳}（注は {番号: 訳} を別に）にする。"""
    main, notes = {}, {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            (notes if m.group(2) else main)[int(m.group(1))] = m.group(3)
    return main, notes


def ask_numbered(system, prompt, numbers):
    """番号つきの答えを求める。欠けた番号があれば1回だけ聞き直す。"""
    text, out, _ = call_claude(system, prompt)
    main, notes = parse_lines(text)
    missing = [n for n in numbers if n not in main]
    if missing:
        text, out2, _ = call_claude(
            system, prompt + "\n\nAnswer ONLY for these numbers: " + ", ".join(map(str, missing)))
        m2, n2 = parse_lines(text)
        main.update({k: v for k, v in m2.items() if k in missing})
        notes.update(n2)
        out += out2
    return main, notes, out


def save(path, obj):
    """1件1行で書く（差分が読めるように）。"""
    lines = [f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
             for k, v in obj.items()]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    tmp.replace(path)


def load(path):
    return json.load(open(path, encoding="utf-8")) if path.exists() else {}


# ---------- ルビ ----------

def ruby_batches(book, done):
    """未生成のチャンクを、対象語が RUBY_TARGETS 個前後になるように本文の順で束ねる。"""
    batches, cur, n = [], [], 0
    for c in book["chunks"]:
        key = chunk_key(c, book["surf"])
        if key in done:
            continue
        targets = [(pi, si, ti) for pi, para in enumerate(c["p"]) for si, arr in enumerate(para["s"])
                   for ti, x in enumerate(arr)
                   if isinstance(x, int) and book["lv"][book["sf"][x]] >= RUBY_MIN_LEVEL]
        cur.append((key, c, targets))
        n += len(targets)
        if n >= RUBY_TARGETS:
            batches.append(cur)
            cur, n = [], 0
    if cur:
        batches.append(cur)
    return batches


def do_ruby(book, batch):
    lines, where = [], []   # where[番号-1] = (チャンクの識別子, 段落, 文, 位置)
    for key, c, targets in batch:
        tset = set(targets)
        for pi, para in enumerate(c["p"]):
            text = ""
            for si, arr in enumerate(para["s"]):
                for ti, x in enumerate(arr):
                    if isinstance(x, int):
                        text += book["surf"][x]
                        if (pi, si, ti) in tset:
                            where.append((key, pi, si, ti))
                            text += f"⟦{len(where)}⟧"
                    elif isinstance(x, str):
                        text += x
                text += " "
            lines.append(text.strip())
    result = {key: [] for key, _, _ in batch}
    out = 0
    if where:
        main, _, out = ask_numbered(SYS_RUBY, "\n".join(lines), range(1, len(where) + 1))
        for n, gloss in main.items():
            if 1 <= n <= len(where) and gloss.strip("-‐－ー ") and len(gloss) <= RUBY_MAX_LEN:
                key, pi, si, ti = where[n - 1]
                result[key].append([pi, si, ti, gloss])
        for rows in result.values():
            rows.sort()
    return result, out


# ---------- 辞書 ----------

def dict_items(book, done):
    items = []
    for prefix, entries in (("", book["dict"].items()), ("phr:", ((p["t"], p["e"]) for p in book["phr"]))):
        for key, ents in entries:
            for i, e in enumerate(ents):
                ident = f"{prefix}{key}#{i}"
                if ident not in done and not e.get("ja") and e["g"]:
                    items.append((ident, key.replace("ai:", ""), e))
    return [items[i:i + DICT_BATCH] for i in range(0, len(items), DICT_BATCH)]


def do_dict(book, batch):
    lines = []
    for n, (_, word, e) in enumerate(batch, 1):
        lines.append(f"{n}. {word} [{e['pos']}] " + " ; ".join(e["g"]))
        if e.get("note"):
            lines.append(f"   note: {e['note']}")
    main, notes, out = ask_numbered(SYS_DICT, "\n".join(lines), range(1, len(batch) + 1))
    result = {}
    for n, (ident, _, e) in enumerate(batch, 1):
        if n in main:
            result[ident] = {"g": [g.strip() for g in re.split(r"[｜|]", main[n]) if g.strip()][:4]}
            if e.get("note") and n in notes:
                result[ident]["note"] = notes[n]
    return result, out


# ---------- あらすじ ----------

def summary_batches(book, done):
    return [[i] for i in range(len(book["toc"])) if str(i) not in done]


def do_summary(book, batch):
    ci = batch[0]
    paras = []
    for c in book["chunks"][book["toc"][ci]["c"]:]:
        if c["ch"] != ci:
            break
        for para in c["p"]:
            paras.append(" ".join("".join(book["surf"][x] if isinstance(x, int) else x
                                           for x in arr if not isinstance(x, dict)) for arr in para["s"]))
    text, out, _ = call_claude(SYS_SUMMARY, f"Chapter: {book['toc'][ci]['t']}\n\n" + "\n".join(paras))
    return {str(ci): "\n".join(s.strip() for s in text.strip().splitlines() if s.strip())}, out


TASKS = {
    "ruby": (DATA / "ja_ruby.json", ruby_batches, do_ruby),
    "dict": (DATA / "ja_dict.json", dict_items, do_dict),
    "summary": (DATA / "ja_summary.json", summary_batches, do_summary),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=TASKS)
    ap.add_argument("--limit", type=int, default=0, help="今回実行するバッチ数の上限（試し用）")
    ap.add_argument("--jobs", type=int, default=3, help="並列数")
    args = ap.parse_args()

    path, make_batches, do = TASKS[args.task]
    book = json.load(open(BOOK, encoding="utf-8"))
    done = load(path)
    batches = make_batches(book, done)
    total = len(batches)
    if args.limit:
        batches = batches[: args.limit]
    print(f"{args.task}: 残り {total} バッチ、今回 {len(batches)} バッチ（並列 {args.jobs}）", file=sys.stderr)

    stop = threading.Event()

    def work(batch):
        if stop.is_set():
            return None
        return do(book, batch)

    t0, n_done, n_out, failed = time.time(), 0, 0, 0
    with ThreadPoolExecutor(args.jobs) as ex:
        for fut in as_completed([ex.submit(work, b) for b in batches]):
            try:
                res = fut.result()
            except Stop as e:
                if not stop.is_set():
                    print(f"  止める: {e}", file=sys.stderr)
                stop.set()
                continue
            except subprocess.TimeoutExpired:
                failed += 1
                print("  時間切れ、このバッチはとばす（再実行でやり直す）", file=sys.stderr)
                continue
            if res is None:
                continue
            result, out = res
            done.update(result)
            save(path, done)
            n_done += 1
            n_out += out
            print(f"  {n_done}/{len(batches)}  {time.time() - t0:.0f}秒  出力 {n_out} トークン", file=sys.stderr)
    print(f"{args.task}: {n_done} バッチ完了、{failed} バッチ失敗、{time.time() - t0:.0f}秒"
          + ("（途中で止まった。再実行で続きから）" if stop.is_set() else ""))
    sys.exit(1 if stop.is_set() else 0)


if __name__ == "__main__":
    main()
