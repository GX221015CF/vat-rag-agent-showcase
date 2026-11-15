from __future__ import annotations

import re

import jieba


CHINESE_RUN = re.compile(r"[\u3400-\u9fff]+")
ALNUM = re.compile(r"[A-Za-z0-9_]+")


def search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    tokens.extend(token.strip().lower() for token in jieba.cut_for_search(text) if token.strip())
    tokens.extend(match.group(0).lower() for match in ALNUM.finditer(text))
    for run in CHINESE_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(token for token in tokens if token))


def fts_document_text(title: str, anchor: str, text: str) -> str:
    return " ".join(search_tokens(f"{title} {anchor} {text}"))


def fts_query(text: str) -> str:
    tokens = search_tokens(text)
    if not tokens:
        return '"__no_match__"'
    return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:80])

