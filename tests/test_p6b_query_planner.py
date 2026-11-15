from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tax_rag.config import load_json, load_toml
from tax_rag.retrieval import exact_retriever
from tax_rag.retrieval.hybrid import retrieve_with_plan
from tax_rag.retrieval.query_planner import plan_query
from tax_rag.services.retrieval_service import ActiveRetrievalService


class P6BQueryPlannerUnitTests(unittest.TestCase):
    def test_formal_config_is_bounded_to_five_documents_and_four_expansions(self) -> None:
        payload = load_json("query_normalization_v1.json")
        canonical_titles = {
            item["canonical_title"] for item in payload["document_aliases"]
        }
        triggers = {
            trigger
            for item in payload["expansion_rules"]
            for trigger in item["triggers"]
        }

        self.assertEqual(payload["version"], "query-planner-v1")
        self.assertEqual(len(canonical_titles), 5)
        self.assertEqual(len(payload["expansion_rules"]), 4)
        self.assertNotIn("什么时候交税", triggers)
        self.assertNotIn("白送", triggers)
        self.assertNotIn("视同销售", triggers)

    def test_resolves_short_law_name_and_arabic_article(self) -> None:
        plan = plan_query("增值税法第5条怎么规定？")

        self.assertEqual(plan.resolved_document_title, "中华人民共和国增值税法")
        self.assertEqual(plan.article_number, 5)
        self.assertTrue(plan.exact_locator)
        self.assertIn("DOCUMENT_ALIAS_VAT_LAW", plan.applied_rules)
        self.assertIn("ARTICLE_ARABIC_TO_INTEGER", plan.applied_rules)

    def test_longer_regulation_alias_wins_over_law_substring(self) -> None:
        plan = plan_query("增值税法实施条例第二十五条怎么处理？")

        self.assertEqual(
            plan.resolved_document_title,
            "中华人民共和国增值税法实施条例",
        )
        self.assertEqual(plan.article_number, 25)
        self.assertTrue(plan.exact_locator)

    def test_resolves_collection_rules_without_di(self) -> None:
        plan = plan_query("征管法实施细则42条需要哪些材料？")

        self.assertEqual(
            plan.resolved_document_title,
            "中华人民共和国税收征收管理法实施细则",
        )
        self.assertEqual(plan.article_number, 42)
        self.assertTrue(plan.exact_locator)

    def test_two_named_documents_are_not_forced_into_one_locator(self) -> None:
        plan = plan_query("增值税法和增值税法实施条例第五条有什么不同？")

        self.assertIsNone(plan.resolved_document_title)
        self.assertEqual(plan.article_number, 5)
        self.assertFalse(plan.exact_locator)
        self.assertIn("DOCUMENT_ALIAS_AMBIGUOUS", plan.applied_rules)

    def test_two_article_numbers_are_not_forced_into_one_locator(self) -> None:
        plan = plan_query("增值税法第五条和第十九条有什么关系？")

        self.assertEqual(plan.resolved_document_title, "中华人民共和国增值税法")
        self.assertIsNone(plan.article_number)
        self.assertFalse(plan.exact_locator)
        self.assertIn("ARTICLE_AMBIGUOUS", plan.applied_rules)

    def test_article_only_query_does_not_call_exact_database_lookup(self) -> None:
        connection = Mock()
        plan = plan_query("第五条怎么规定？")

        hits = exact_retriever.retrieve(connection, plan, 10)

        self.assertEqual(hits, [])
        connection.execute.assert_not_called()

    def test_title_only_query_does_not_return_arbitrary_first_articles(self) -> None:
        connection = Mock()
        plan = plan_query("增值税法主要讲什么？")

        hits = exact_retriever.retrieve(connection, plan, 10)

        self.assertEqual(hits, [])
        connection.execute.assert_not_called()

    def test_expansion_preserves_original_query_and_adds_term(self) -> None:
        query = "一般纳税人收到专票后怎样处理？"
        plan = plan_query(query)

        self.assertEqual(plan.raw_query, query)
        self.assertIn(query, plan.fts_query_text)
        self.assertIn("增值税专用发票", plan.fts_query_text)
        self.assertEqual(plan.expansion_terms, ("增值税专用发票",))
        self.assertIn("COLLOQUIAL_SPECIAL_INVOICE_V1", plan.applied_rules)

    def test_free_transfer_expansion_is_withheld_to_preserve_p5a_trace(self) -> None:
        plan = plan_query("公司把货物白送给客户怎么交税？")

        self.assertEqual(plan.expansion_terms, ())
        self.assertNotIn("无偿转让", plan.fts_query_text)

    def test_ambiguous_payment_time_phrase_is_not_expanded(self) -> None:
        plan = plan_query("一般纳税人什么时候交税？")

        self.assertEqual(plan.expansion_terms, ())
        self.assertNotIn("纳税义务发生时间", plan.fts_query_text)

    def test_invalid_duplicate_alias_mapping_fails_closed(self) -> None:
        payload = {
            "version": "bad",
            "enabled": True,
            "document_aliases": [
                {
                    "rule_id": "A",
                    "canonical_title": "文档甲",
                    "aliases": ["简称"],
                },
                {
                    "rule_id": "B",
                    "canonical_title": "文档乙",
                    "aliases": ["简称"],
                },
            ],
            "expansion_rules": [],
        }

        with self.assertRaisesRegex(RuntimeError, "法规别名映射冲突"):
            plan_query("简称第五条", payload)

    @patch("tax_rag.retrieval.hybrid.vector_retriever.retrieve", return_value=[])
    @patch("tax_rag.retrieval.hybrid.fts_retriever.retrieve", return_value=[])
    @patch("tax_rag.retrieval.hybrid.exact_retriever.retrieve", return_value=[])
    def test_hybrid_uses_plan_for_exact_fts_but_raw_query_for_vector(
        self,
        exact_mock,
        fts_mock,
        vector_mock,
    ) -> None:
        query = "一般纳税人收到专票后怎样处理？"
        config = load_toml("retrieval.toml")

        _, _, plan = retrieve_with_plan(
            Mock(),
            Path("isolated-chroma"),
            "collection",
            "bge-m3:latest",
            "http://127.0.0.1:11434",
            query,
            config,
        )

        self.assertIs(exact_mock.call_args.args[1], plan)
        self.assertIn("增值税专用发票", fts_mock.call_args.args[1])
        self.assertEqual(vector_mock.call_args.args[5], query)


class P6BRealAliasRetrievalTests(unittest.TestCase):
    def test_short_vat_law_alias_hits_article_five_at_exact_top_one(self) -> None:
        service = ActiveRetrievalService()
        self.addCleanup(service.close)

        _, hits, channels, plan = service.search_with_trace(
            "增值税法第5条怎么规定？"
        )

        self.assertTrue(plan.exact_locator)
        self.assertGreater(len(hits), 0)
        self.assertGreater(len(channels["exact"]), 0)
        self.assertEqual(
            (channels["exact"][0].document_title, channels["exact"][0].anchor),
            ("中华人民共和国增值税法", "第五条"),
        )
        self.assertEqual(
            (hits[0].document_title, hits[0].anchor),
            ("中华人民共和国增值税法", "第五条"),
        )


if __name__ == "__main__":
    unittest.main()
