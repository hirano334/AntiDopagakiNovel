"""kaikki.org のロシア語 Wiktionary 抽出（jsonl）から、見出し語→英語語義の辞書を作る。

入力: data/raw/kaikki-ru.jsonl
  https://kaikki.org/dictionary/Russian/kaikki.org-dictionary-Russian.jsonl
出力: data/work/dict_ru.json  {key: [{"pos":..., "g":[...]}]}
      data/work/formof_ru.json {key: [lemma, ...]}   変化形→見出し語
      data/work/phrases.json   {key: [{"pos":..., "g":[...]}]}  空白を含む見出し
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data/raw/kaikki-ru.jsonl"
OUT = ROOT / "data/work"

MAX_SENSES = 5
MAX_GLOSS = 120
# これらのタグを持つ form-of は活用形（「生格単数」など）。語義としては捨てて、見出し語への参照だけ残す。
# 指小形（минутка）や動詞からの派生名詞（родитель）も form-of になっているが、これらは語義を残す。
INFLECTION_TAGS = {
    "nominative", "genitive", "dative", "accusative", "instrumental", "prepositional",
    "locative", "vocative", "partitive", "singular", "plural", "participle", "imperative",
    "past", "present", "future", "first-person", "second-person", "third-person", "short-form",
}
SKIP_POS = {"character", "letter", "symbol", "romanization"}


def norm(s):
    return s.lower().replace("ё", "е").replace("́", "").strip()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    words, phrases, formof = {}, {}, {}
    with open(SRC, encoding="utf-8") as f:
        for n, line in enumerate(f):
            e = json.loads(line)
            if e.get("lang_code") != "ru" or not e.get("word") or e.get("pos") in SKIP_POS:
                continue
            key = norm(e["word"])
            pos = e.get("pos", "")
            glosses = []
            for s in e.get("senses", []):
                tags = set(s.get("tags", []))
                for fo in s.get("form_of", []):
                    lem = norm(fo.get("word", ""))
                    if lem and lem != key:
                        lst = formof.setdefault(key, [])
                        if lem not in lst:
                            lst.append(lem)
                if "form-of" in tags and tags & INFLECTION_TAGS:
                    continue
                g = s.get("glosses")
                if not g:
                    continue
                text = g[-1]
                if text.startswith("The name of the Cyrillic"):
                    continue
                if len(text) > MAX_GLOSS:
                    text = text[: MAX_GLOSS - 1] + "…"
                if text not in glosses:
                    glosses.append(text)
            if not glosses:
                continue
            ent = {"pos": pos, "g": glosses[:MAX_SENSES]}
            nw = len(key.split())
            if nw >= 2:
                if nw <= 5:
                    phrases.setdefault(key, []).append(ent)
            else:
                words.setdefault(key, []).append(ent)
            if n % 200000 == 0:
                print(n, len(words), len(phrases), file=sys.stderr)
    for name, obj in (("dict_ru", words), ("formof_ru", formof), ("phrases", phrases)):
        with open(OUT / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    print(f"words={len(words)} phrases={len(phrases)} formof={len(formof)}")


if __name__ == "__main__":
    main()
