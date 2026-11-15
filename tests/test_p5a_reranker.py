from __future__ import annotations

import hashlib
import unittest

from tax_rag.config import load_toml
from tax_rag.models import SearchHit
from tax_rag.retrieval.hybrid import validate_reranker_config
from tax_rag.retrieval.legal_reference_reranker import rerank_legal_references
from tax_rag.services.retrieval_service import ActiveRetrievalService


def hit(
    chunk_id: str,
    anchor: str,
    text: str,
    title: str = "中华人民共和国增值税法",
) -> SearchHit:
    return SearchHit(
        chunk_id,
        f"doc-{title}",
        "version-1",
        title,
        anchor,
        text,
        {},
        1.0,
        "rrf",
        {"vector": 1},
    )


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(path) -> list[tuple[str, str]]:
    return [
        (item.relative_to(path).as_posix(), sha256_file(item))
        for item in sorted(path.rglob("*"))
        if item.is_file()
    ]


class P5ALegalReferenceRerankerTests(unittest.TestCase):
    def test_rescues_referenced_article_after_top_referrer(self) -> None:
        pool = [
            hit("c19", "第十九条", "第十九条 发生本法第五条规定的视同应税交易。"),
            hit("c18", "第十八条", "第十八条 其他规则。"),
            hit("c7", "第七条", "第七条 其他规则。"),
            hit("c23", "第二十三条", "第二十三条 其他规则。"),
            hit("c16", "第十六条", "第十六条 其他规则。"),
            hit("c5", "第五条", "第五条 无偿转让货物视同应税交易。"),
        ]

        result, trace = rerank_legal_references(pool, final_top_k=5)

        self.assertEqual([item.chunk_id for item in result], ["c19", "c5", "c18", "c7", "c23"])
        self.assertEqual(result[1].ranks["rrf_pool"], 6)
        self.assertEqual(result[1].ranks["reference_rescue"], 1)
        self.assertEqual(result[1].ranks["final"], 2)
        self.assertEqual(trace[0]["reference_text"], "本法第五条")

    def test_does_not_rescue_reference_from_second_rank(self) -> None:
        pool = [
            hit("c1", "第一条", "第一条 不含交叉引用。"),
            hit("c19", "第十九条", "第十九条 发生本法第五条规定的交易。"),
            hit("c2", "第二条", "第二条 其他规则。"),
            hit("c3", "第三条", "第三条 其他规则。"),
            hit("c4", "第四条", "第四条 其他规则。"),
            hit("c5", "第五条", "第五条 被引用规则。"),
        ]

        result, trace = rerank_legal_references(pool, final_top_k=5)

        self.assertEqual([item.chunk_id for item in result], ["c1", "c19", "c2", "c3", "c4"])
        self.assertEqual(trace, [])

    def test_does_not_cross_from_regulation_to_named_law(self) -> None:
        regulation = "中华人民共和国增值税法实施条例"
        pool = [
            hit("reg18", "第十八条", "第十八条 依照增值税法第五条处理。", regulation),
            hit("reg2", "第二条", "第二条 其他规则。", regulation),
            hit("reg3", "第三条", "第三条 其他规则。", regulation),
            hit("reg4", "第四条", "第四条 其他规则。", regulation),
            hit("reg6", "第六条", "第六条 其他规则。", regulation),
            hit("law5", "第五条", "第五条 法律规则。"),
        ]

        result, trace = rerank_legal_references(pool, final_top_k=5)

        self.assertEqual([item.chunk_id for item in result], ["reg18", "reg2", "reg3", "reg4", "reg6"])
        self.assertEqual(trace, [])

    def test_keeps_existing_top_five_when_target_already_present(self) -> None:
        pool = [
            hit("c19", "第十九条", "第十九条 发生本法第五条规定的交易。"),
            hit("c5", "第五条", "第五条 被引用规则。"),
            hit("c2", "第二条", "第二条 其他规则。"),
            hit("c3", "第三条", "第三条 其他规则。"),
            hit("c4", "第四条", "第四条 其他规则。"),
            hit("c6", "第六条", "第六条 其他规则。"),
        ]

        result, trace = rerank_legal_references(pool, final_top_k=5)

        self.assertEqual([item.chunk_id for item in result], ["c19", "c5", "c2", "c3", "c4"])
        self.assertEqual(trace, [])
        self.assertEqual(result[1].ranks["rrf_pool"], 2)

    def test_formal_config_enables_only_approved_candidate(self) -> None:
        config = load_toml("retrieval.toml")

        self.assertEqual(config["retrieval"]["candidate_pool_top_k"], 40)
        self.assertEqual(config["retrieval"]["final_top_k"], 5)
        self.assertTrue(config["reranker"]["enabled"])
        self.assertEqual(
            config["reranker"]["strategy"],
            "LEGAL_CROSS_REFERENCE_RESCUE_V1",
        )
        self.assertEqual(config["reranker"]["referrer_top_n"], 1)

    def test_unknown_enabled_strategy_is_rejected(self) -> None:
        config = {"reranker": {"enabled": True, "strategy": "TYPO"}}

        with self.assertRaisesRegex(ValueError, "不支持的重排策略"):
            validate_reranker_config(config)


class P5ARealRetrievalIntegrationTests(unittest.TestCase):
    def test_d07_rescues_article_five_without_mutating_formal_chroma(self) -> None:
        service = ActiveRetrievalService()
        generation = service.active_generation()
        runtime_snapshot = (
            service.chroma_snapshot_root / generation["generation_id"]
        )
        self.assertTrue(runtime_snapshot.exists())
        runtime_before = sha256_tree(runtime_snapshot)
        isolated_root = None
        try:
            _, hits, _ = service.search(
                "公司把货物白送给客户，没有取得任何收入，"
                "增值税销售额应当按零还是按市场价格？"
            )
            isolated_copy = service.isolated_chroma_copy(generation["generation_id"])
            isolated_root = isolated_copy.parent
            self.assertEqual(
                [(item.document_title, item.anchor) for item in hits[:2]],
                [
                    ("中华人民共和国增值税法", "第十九条"),
                    ("中华人民共和国增值税法", "第五条"),
                ],
            )
            rescued = hits[1]
            self.assertGreater(rescued.ranks["rrf_pool"], 5)
            self.assertLessEqual(rescued.ranks["rrf_pool"], 40)
            self.assertEqual(rescued.ranks["reference_rescue"], 1)
            self.assertEqual(rescued.ranks["final"], 2)
            self.assertEqual(runtime_before, sha256_tree(runtime_snapshot))
        finally:
            service.close()
        if isolated_root is not None:
            self.assertFalse(isolated_root.exists())


if __name__ == "__main__":
    unittest.main()
