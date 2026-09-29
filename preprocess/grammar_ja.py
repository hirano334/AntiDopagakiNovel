"""pymorphy3（OpenCorpora）のタグを日本語のラベルに変換する。"""

POS = {
    "NOUN": "名詞", "ADJF": "形容詞", "ADJS": "形容詞短語尾", "COMP": "比較級",
    "VERB": "動詞", "INFN": "動詞不定形", "PRTF": "形動詞", "PRTS": "形動詞短語尾",
    "GRND": "副動詞", "NUMR": "数詞", "ADVB": "副詞", "NPRO": "代名詞",
    "PRED": "述語副詞", "PREP": "前置詞", "CONJ": "接続詞", "PRCL": "助詞",
    "INTJ": "間投詞",
}

# 表示順に並べる
GRAMMEMES = [
    ("perf", "完了体"), ("impf", "不完了体"),
    ("impr", "命令法"),
    ("pres", "現在"), ("past", "過去"), ("futr", "未来"),
    ("actv", "能動"), ("pssv", "受動"),
    ("1per", "1人称"), ("2per", "2人称"), ("3per", "3人称"),
    ("masc", "男性"), ("femn", "女性"), ("neut", "中性"), ("ms-f", "男女性"),
    ("sing", "単数"), ("plur", "複数"),
    ("nomn", "主格"), ("gent", "生格"), ("datv", "与格"), ("accs", "対格"),
    ("ablt", "造格"), ("loct", "前置格"), ("voct", "呼格"),
    ("gen2", "第二生格"), ("acc2", "第二対格"), ("loc2", "第二前置格"),
    ("Supr", "最上級"),
]

PROPER = {"Name", "Surn", "Patr", "Geox", "Orgn"}


def pos_ja(tag):
    label = POS.get(tag.POS, tag.POS or "?")
    if PROPER & tag.grammemes:
        label += "（固有名詞）"
    return label


def form_ja(tag):
    g = tag.grammemes
    return " ".join(ja for code, ja in GRAMMEMES if code in g)
