from __future__ import annotations

import re
from dataclasses import dataclass

from tax_rag.config import load_json
from tax_rag.domain.matter_state import RouteStatus


@dataclass(frozen=True)
class ScopeDecision:
    status: RouteStatus
    reason: str
    matched_keyword: str | None = None
    router_version: str = "topic-scope-router-v2"
    matched_topic_code: str | None = None
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class TopicRule:
    topic_code: str
    label: str
    required_term_groups: tuple[tuple[str, ...], ...]
    reason: str


OUT_OF_SCOPE_KEYWORDS = {
    "进口": "V1不覆盖进口环节",
    "出口": "V1不覆盖出口退免税或出口业务",
    "跨境电商": "V1不覆盖跨境电商",
    "跨境销售": "V1不覆盖跨境销售",
    "加计抵减": "V1不覆盖加计抵减",
    "留抵退税": "V1不覆盖留抵退税",
    "预缴": "V1不覆盖预缴模块",
    "个人所得税": "V1仅覆盖增值税",
    "企业所得税": "V1仅覆盖增值税",
    "消费税": "V1仅覆盖增值税",
    "印花税": "V1仅覆盖增值税",
}

IN_SCOPE_KEYWORDS = (
    "委托加工",
    "受托加工",
    "加工费",
    "视同销售",
    "视同应税",
    "自产产品",
    "外购货物",
    "卖货",
    "增值税",
    "一般纳税人",
    "销售货物",
    "内销货物",
    "销项税额",
    "进项税额",
    "抵扣",
    "发票",
    "销售额",
    "纳税义务发生时间",
    "税率",
    "小规模",
    "征收率",
    "起征点",
    "税务登记",
    "滞纳金",
    "税收保全",
    "延期缴纳",
)


def _normalize(value: str) -> str:
    return re.sub(r"[\s，。？！、,.!?;；:：()（）]+", "", value)


def _load_topic_rules() -> tuple[str, tuple[TopicRule, ...]]:
    payload = load_json("scope_topics_v2.json")
    version = payload.get("version")
    raw_rules = payload.get("rules")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("scope_topics_v2.json缺少有效version")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuntimeError("scope_topics_v2.json缺少专题规则")
    rules = []
    for raw_rule in raw_rules:
        groups = raw_rule.get("required_term_groups")
        if (
            not isinstance(groups, list)
            or not groups
            or any(
                not isinstance(group, list)
                or not group
                or any(not isinstance(term, str) or not term.strip() for term in group)
                for group in groups
            )
        ):
            raise RuntimeError("scope_topics_v2.json包含无效required_term_groups")
        rules.append(
            TopicRule(
                topic_code=raw_rule["topic_code"],
                label=raw_rule["label"],
                required_term_groups=tuple(tuple(group) for group in groups),
                reason=raw_rule["reason"],
            )
        )
    return version, tuple(rules)


TOPIC_ROUTER_VERSION, TOPIC_RULES = _load_topic_rules()


def _match_topic(normalized: str) -> tuple[TopicRule, tuple[str, ...]] | None:
    for rule in TOPIC_RULES:
        matched_terms = []
        for group in rule.required_term_groups:
            matched = next(
                (term for term in group if _normalize(term) in normalized),
                None,
            )
            if matched is None:
                break
            matched_terms.append(matched)
        else:
            return rule, tuple(matched_terms)
    return None


def route_scope(query: str) -> ScopeDecision:
    normalized = _normalize(query)
    if "小规模" in normalized and any(term in normalized for term in ("销售不动产", "出租不动产", "转让土地", "销售服务", "建筑服务", "旅游服务")):
        return ScopeDecision(RouteStatus.OUT_OF_SCOPE, "本批小规模范围为境内销售货物，所问交易超出范围", router_version=TOPIC_ROUTER_VERSION)
    if "简易计税" in normalized and "一般纳税人" in normalized and "小规模" not in normalized:
        return ScopeDecision(RouteStatus.OUT_OF_SCOPE, "一般纳税人简易计税专项业务尚未覆盖", router_version=TOPIC_ROUTER_VERSION)
    for keyword, reason in OUT_OF_SCOPE_KEYWORDS.items():
        if _normalize(keyword) in normalized:
            return ScopeDecision(
                RouteStatus.OUT_OF_SCOPE,
                f"{reason}；路由={TOPIC_ROUTER_VERSION}；方式=明确排除词；命中={keyword}",
                keyword,
                TOPIC_ROUTER_VERSION,
            )
    for keyword in IN_SCOPE_KEYWORDS:
        if _normalize(keyword) in normalized:
            return ScopeDecision(
                RouteStatus.IN_SCOPE,
                (
                    "问题包含V1范围内的增值税或配套征管主题；"
                    f"路由={TOPIC_ROUTER_VERSION}；方式=范围关键词；命中={keyword}"
                ),
                keyword,
                TOPIC_ROUTER_VERSION,
            )
    topic_match = _match_topic(normalized)
    if topic_match is not None:
        rule, matched_terms = topic_match
        return ScopeDecision(
            RouteStatus.IN_SCOPE,
            (
                f"{rule.reason}；路由={TOPIC_ROUTER_VERSION}；方式=专题规则；"
                f"专题={rule.topic_code}；命中={'+'.join(matched_terms)}"
            ),
            f"topic:{rule.topic_code}",
            TOPIC_ROUTER_VERSION,
            rule.topic_code,
            matched_terms,
        )
    return ScopeDecision(
        RouteStatus.NEED_SCOPE_CONFIRMATION,
        (
            "尚不能确认是否为境内销售货物增值税咨询；"
            f"路由={TOPIC_ROUTER_VERSION}；方式=无匹配"
        ),
        router_version=TOPIC_ROUTER_VERSION,
    )
