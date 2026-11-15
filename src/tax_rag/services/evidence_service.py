from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from tax_rag.models import SearchHit


def metadata(connection: sqlite3.Connection, version_id: str) -> dict:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_metadata'").fetchone()
    if not exists:
        return {"validity_basis": "旧库缺少版本元数据，需要核对原文与适用期间"}
    row = connection.execute("SELECT payload_json FROM source_metadata WHERE version_id=?", (version_id,)).fetchone()
    return json.loads(row[0]) if row else {"validity_basis": "来源元数据缺失，需要人工核对"}


def period_status(source: dict, as_of: date, unresolved: bool | None) -> str:
    start = source.get("effective_from")
    end = source.get("effective_to")
    if end and as_of > date.fromisoformat(end):
        return "outside_effective_period"
    if start and as_of < date.fromisoformat(start):
        conditional = source.get("conditional_retroactive_from")
        if conditional and as_of >= date.fromisoformat(conditional):
            if unresolved is True:
                return "conditional_unresolved_matters"
            return "requires_unresolved_status" if unresolved is None else "outside_effective_period"
        return "outside_effective_period"
    return "within_recorded_period"


def consultation_date(text: str) -> date:
    matches = list(re.finditer(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?", text))
    if matches:
        return date(*(int(value) for value in matches[-1].groups()))
    matches = list(re.finditer(r"(20\d{2})年(\d{1,2})月", text))
    if matches:
        return date(int(matches[-1][1]), int(matches[-1][2]), 1)
    matches = list(re.finditer(r"(20\d{2})年(?!第)", text))
    return date(int(matches[-1][1]), 1, 1) if matches else date.today()


def enrich_evidence(service, hits: list, as_of: date) -> list:
    if not hasattr(service, "connect_readonly"):
        return hits
    enriched = []
    with closing(service.connect_readonly()) as connection:
        for hit in hits:
            version = metadata(connection, hit.version_id)
            state = period_status(version, as_of, None)
            boundary_reference = hit.channel == "policy_required" or version.get("source_id") in {"MOF_2026_10_THRESHOLD", "MOF_2026_10_SMALL_RATE"}
            if state == "outside_effective_period" and boundary_reference and version.get("effective_to") and as_of > date.fromisoformat(version["effective_to"]):
                state = "expired_reference_only"
            if state != "outside_effective_period":
                enriched.append(replace(hit, source={**hit.source, "version": version, "period_status": state, "as_of": as_of.isoformat()}))
    return enriched


def required_policy_evidence(service, query: str) -> list:
    """Retrieve scoped policy sections and legal principles from the active generation only."""
    if not hasattr(service, "connect_readonly"):
        return []
    legal_articles = set()
    if any(term in query for term in ("委托加工", "受托方", "加工费")):
        legal_articles.update(("第三条", "第八条", "第十一条", "第十四条", "第十六条"))
    if any(term in query for term in ("受托方", "加工费")):
        legal_articles.add("第十条")
    if any(term in query for term in ("施行", "生效", "实施日期")):
        legal_articles.add("第三十八条")
    if "收回" in query and "销售" in query:
        legal_articles.update(("第三条", "第八条", "第十条", "第十四条", "第十六条"))
    if any(term in query for term in ("税率", "13%", "9%", "电视机")):
        legal_articles.add("第十条")
    if any(term in query for term in ("视同", "福利", "无偿", "赠送")):
        legal_articles.add("第五条")
    if any(term in query for term in ("有偿", "价款", "扣款", "扣工资", "扣减", "付钱", "福利")):
        legal_articles.add("第三条")
    if any(term in query for term in ("销售额", "计税依据", "市场价格", "没有收钱")):
        legal_articles.add("第十九条")
    if "小规模" in query:
        legal_articles.update(("第八条", "第十一条", "第二十三条", "第二十七条"))
    if not legal_articles:
        return []
    with closing(service.connect_readonly()) as connection:
        generation = service.active_generation(connection)
        placeholders = ",".join("?" for _ in legal_articles)
        rows = connection.execute(
            f"""SELECT c.*, d.title FROM chunks c JOIN documents d USING(document_id)
               JOIN index_members i USING(chunk_id)
               WHERE i.generation_id=? AND (
                 (d.title='中华人民共和国增值税法' AND c.anchor IN ({placeholders})) OR
                 (?=1 AND (d.title LIKE '增值税起征点标准（2026%' OR
                   d.title LIKE '小规模纳税人3%减按1%政策%' OR
                   (d.title='国家税务总局关于起征点标准等增值税征管事项的公告' AND c.anchor IN ('第一项','第四项','第五项'))))
               ) ORDER BY d.title,c.ordinal""", (generation["generation_id"], *sorted(legal_articles), int("小规模" in query))
        ).fetchall()
        return [SearchHit(row["chunk_id"], row["document_id"], row["version_id"], row["title"],
                          row["anchor"], row["text"], json.loads(row["source_locator_json"]), 1.0, "policy_required") for row in rows]
