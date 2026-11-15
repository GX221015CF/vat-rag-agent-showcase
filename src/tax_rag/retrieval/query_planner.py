from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

from tax_rag.config import load_json
from tax_rag.ingest.docx_parser import chinese_number_to_int


PLANNER_CONFIG = "query_normalization_v1.json"
COMPACT = re.compile(r"[\s，。？！、,.!?;；:：()（）《》\[\]【】]+")
ARTICLE_STANDARD = re.compile(
    r"第(?P<number>[零〇一二三四五六七八九十百千万两\d]+)条"
)
ARTICLE_AFTER_ALIAS = re.compile(
    r"^(?:第)?(?P<number>[零〇一二三四五六七八九十百千万两\d]+)条"
)


@dataclass(frozen=True)
class AliasMatch:
    rule_id: str
    canonical_title: str
    alias: str
    start: int
    end: int


@dataclass(frozen=True)
class QueryPlan:
    raw_query: str
    fts_query_text: str
    resolved_document_title: str | None
    article_number: int | None
    exact_locator: bool
    expansion_terms: tuple[str, ...]
    applied_rules: tuple[str, ...]
    planner_version: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expansion_terms"] = list(self.expansion_terms)
        payload["applied_rules"] = list(self.applied_rules)
        return payload


def _compact(value: str) -> str:
    return COMPACT.sub("", value).lower()


def _validate_config(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("version"), str) or not payload["version"].strip():
        raise RuntimeError("查询规划配置缺少有效version")
    aliases = payload.get("document_aliases")
    expansions = payload.get("expansion_rules")
    if not isinstance(aliases, list) or not aliases:
        raise RuntimeError("查询规划配置缺少document_aliases")
    if not isinstance(expansions, list):
        raise RuntimeError("查询规划配置缺少expansion_rules")

    alias_owners: dict[str, str] = {}
    for item in aliases:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("rule_id"), str)
            or not isinstance(item.get("canonical_title"), str)
            or not isinstance(item.get("aliases"), list)
            or not item["aliases"]
        ):
            raise RuntimeError("查询规划配置包含无效document_aliases条目")
        for alias in item["aliases"]:
            compact_alias = _compact(alias) if isinstance(alias, str) else ""
            if not compact_alias:
                raise RuntimeError("查询规划配置包含空法规别名")
            owner = alias_owners.setdefault(compact_alias, item["canonical_title"])
            if owner != item["canonical_title"]:
                raise RuntimeError(f"法规别名映射冲突：{alias}")

    for item in expansions:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("rule_id"), str)
            or not isinstance(item.get("triggers"), list)
            or not item["triggers"]
            or not isinstance(item.get("terms"), list)
            or not item["terms"]
            or any(not isinstance(value, str) or not value.strip() for value in item["triggers"])
            or any(not isinstance(value, str) or not value.strip() for value in item["terms"])
        ):
            raise RuntimeError("查询规划配置包含无效expansion_rules条目")


@lru_cache(maxsize=1)
def load_query_planner_config() -> dict[str, Any]:
    payload = load_json(PLANNER_CONFIG)
    if not isinstance(payload, dict):
        raise RuntimeError("查询规划配置必须是JSON对象")
    _validate_config(payload)
    return payload


def _raw_alias_matches(compact_query: str, payload: dict[str, Any]) -> list[AliasMatch]:
    matches: list[AliasMatch] = []
    for item in payload["document_aliases"]:
        for raw_alias in item["aliases"]:
            alias = _compact(raw_alias)
            start = compact_query.find(alias)
            while start >= 0:
                matches.append(
                    AliasMatch(
                        item["rule_id"],
                        item["canonical_title"],
                        alias,
                        start,
                        start + len(alias),
                    )
                )
                start = compact_query.find(alias, start + 1)
    return matches


def _non_overlapping_alias_matches(
    compact_query: str, payload: dict[str, Any]
) -> list[AliasMatch]:
    selected: list[AliasMatch] = []
    for match in sorted(
        _raw_alias_matches(compact_query, payload),
        key=lambda item: (-(item.end - item.start), item.start, item.canonical_title),
    ):
        if any(
            existing.start <= match.start and existing.end >= match.end
            for existing in selected
        ):
            continue
        selected.append(match)
    return sorted(selected, key=lambda item: (item.start, item.end))


def _article_value(value: str) -> int:
    return int(value) if value.isdigit() else chinese_number_to_int(value)


def _article_candidates(
    compact_query: str, alias_matches: list[AliasMatch]
) -> list[tuple[int, str]]:
    candidates = [
        (_article_value(match.group("number")), match.group("number"))
        for match in ARTICLE_STANDARD.finditer(compact_query)
    ]
    for alias in alias_matches:
        match = ARTICLE_AFTER_ALIAS.match(compact_query[alias.end :])
        if match:
            candidates.append(
                (_article_value(match.group("number")), match.group("number"))
            )
    unique: dict[int, str] = {}
    for number, raw_value in candidates:
        unique.setdefault(number, raw_value)
    return list(unique.items())


def plan_query(query: str, payload: dict[str, Any] | None = None) -> QueryPlan:
    if payload is None:
        payload = load_query_planner_config()
    else:
        _validate_config(payload)
    raw_query = query.strip()
    compact_query = _compact(raw_query)
    if not bool(payload.get("enabled", True)):
        return QueryPlan(
            raw_query,
            raw_query,
            None,
            None,
            False,
            (),
            (),
            payload["version"],
        )

    alias_matches = _non_overlapping_alias_matches(compact_query, payload)
    canonical_titles = {item.canonical_title for item in alias_matches}
    applied_rules: list[str] = []
    resolved_document_title = None
    if len(canonical_titles) == 1:
        resolved_document_title = next(iter(canonical_titles))
        applied_rules.append(alias_matches[0].rule_id)
    elif len(canonical_titles) > 1:
        applied_rules.append("DOCUMENT_ALIAS_AMBIGUOUS")

    article_candidates = _article_candidates(compact_query, alias_matches)
    article_number = None
    if len(article_candidates) == 1:
        article_number, raw_number = article_candidates[0]
        applied_rules.append(
            "ARTICLE_ARABIC_TO_INTEGER"
            if raw_number.isdigit()
            else "ARTICLE_CHINESE_TO_INTEGER"
        )
    elif len(article_candidates) > 1:
        applied_rules.append("ARTICLE_AMBIGUOUS")

    expansion_terms: list[str] = []
    max_terms = max(0, int(payload.get("max_expansion_terms", 8)))
    for item in payload["expansion_rules"] if max_terms > 0 else []:
        if any(_compact(trigger) in compact_query for trigger in item["triggers"]):
            applied_rules.append(item["rule_id"])
            for term in item["terms"]:
                if term not in expansion_terms and _compact(term) not in compact_query:
                    expansion_terms.append(term)
                    if len(expansion_terms) >= max_terms:
                        break
        if len(expansion_terms) >= max_terms:
            break

    fts_query_text = " ".join([raw_query, *expansion_terms]).strip()
    return QueryPlan(
        raw_query=raw_query,
        fts_query_text=fts_query_text,
        resolved_document_title=resolved_document_title,
        article_number=article_number,
        exact_locator=(resolved_document_title is not None and article_number is not None),
        expansion_terms=tuple(expansion_terms),
        applied_rules=tuple(dict.fromkeys(applied_rules)),
        planner_version=payload["version"],
    )
