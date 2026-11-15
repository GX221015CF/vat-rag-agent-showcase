import unittest
import sqlite3
import json
from datetime import date
from tax_rag.domain.scope_router import route_scope
from tax_rag.domain.conversation import consultation_context
from tax_rag.domain.calculation import calculate_single_transaction
from tax_rag.services.evidence_service import period_status, consultation_date, enrich_evidence, required_policy_evidence
from tax_rag.models import SearchHit


class SmallTaxpayerTests(unittest.TestCase):
    def test_paid_goods_evidence_comes_from_active_generation(self):
        class Service:
            def connect_readonly(self):
                connection = sqlite3.connect(':memory:')
                connection.row_factory = sqlite3.Row
                connection.executescript('''
                    CREATE TABLE documents(document_id TEXT, title TEXT);
                    CREATE TABLE chunks(chunk_id TEXT, document_id TEXT, version_id TEXT,
                        anchor TEXT, text TEXT, source_locator_json TEXT, ordinal INTEGER);
                    CREATE TABLE index_members(generation_id TEXT, chunk_id TEXT);
                    INSERT INTO documents VALUES('law', '中华人民共和国增值税法');
                    INSERT INTO chunks VALUES('active-3','law','v','第三条','有偿转让货物','{}',3);
                    INSERT INTO chunks VALUES('active-5','law','v','第五条','无偿转让货物','{}',5);
                    INSERT INTO chunks VALUES('active-10','law','v','第十条','加工服务税率','{}',10);
                    INSERT INTO chunks VALUES('old-3','law','old','第三条','旧版本','{}',3);
                    INSERT INTO index_members VALUES('active','active-3');
                    INSERT INTO index_members VALUES('active','active-5');
                    INSERT INTO index_members VALUES('active','active-10');
                    INSERT INTO index_members VALUES('old','old-3');
                ''')
                return connection

            def active_generation(self, connection):
                return {'generation_id': 'active'}

        for question in ('我买来的产品给员工发福利，要交增值税吗', '给员工外购福利，改成从工资扣款呢？', '员工直接支付价款，销售货物如何处理？'):
            with self.subTest(question=question):
                hits = required_policy_evidence(Service(), question)
                self.assertIn('active-3', [hit.chunk_id for hit in hits])
                self.assertNotIn('old-3', [hit.chunk_id for hit in hits])
        welfare = required_policy_evidence(Service(), '委托加工货物用于员工福利，要计算销项吗？')
        fee = required_policy_evidence(Service(), '受托方加工费怎么缴税？')
        self.assertNotIn('active-10', [hit.chunk_id for hit in welfare])
        self.assertIn('active-10', [hit.chunk_id for hit in fee])

    def test_small_goods_route_and_special_business_boundary(self):
        self.assertEqual(route_scope("小规模纳税人境内销售货物简易计税1%如何开票").status, "IN_SCOPE")
        self.assertEqual(route_scope("小规模纳税人销售不动产能减按1%吗").status, "OUT_OF_SCOPE")
        self.assertEqual(route_scope("一般纳税人简易计税专项优惠").status, "OUT_OF_SCOPE")

    def test_identity_change_keeps_goods_and_period(self):
        query, dialogue = consultation_context("改成小规模纳税人，按简易计税", [{"role":"USER","content":"2026年一般纳税人销售普通电视机税率多少？"},{"role":"ASSISTANT","content":"13%"}])
        self.assertIn("电视机", query)
        self.assertIn("2026", query)
        self.assertIn("小规模", query)
        self.assertTrue(dialogue)

    def test_expired_preference_and_confirmed_rate_decimal(self):
        self.assertEqual(consultation_date("请判断2025年一般纳税人自产福利").year,2025)
        self.assertEqual(consultation_date("2028年小规模还能按2026年第10号公告减税吗").year,2028)
        metadata={"effective_from":"2026-01-01","effective_to":"2027-12-31"}
        self.assertEqual(period_status(metadata,date(2028,1,1),None),"outside_effective_period")
        result=calculate_single_transaction("101000","TAX_INCLUDED","1")
        self.assertEqual(result.outputs['net_amount'],"100000.00")
        self.assertEqual(result.outputs['tax_amount'],"1000.00")

    def test_expired_policy_from_initial_retrieval_remains_reference_only(self):
        class Service:
            def connect_readonly(self):
                connection=sqlite3.connect(':memory:');connection.row_factory=sqlite3.Row
                connection.execute('create table source_metadata(version_id text,payload_json text)')
                connection.execute('insert into source_metadata values(?,?)',('v',json.dumps({'source_id':'MOF_2026_10_SMALL_RATE','effective_from':'2026-01-01','effective_to':'2027-12-31'})))
                return connection
        hit=SearchHit('c','d','v','政策','第一项','原文',{},1.0,'rrf')
        enriched=enrich_evidence(Service(),[hit],date(2028,1,1))
        self.assertEqual(enriched[0].source['period_status'],'expired_reference_only')
