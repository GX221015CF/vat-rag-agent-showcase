import unittest
import test_web_simplification


class BrowserConsentTests(unittest.TestCase):
    setUp = test_web_simplification.WebSimplificationTests.setUp
    tearDown = test_web_simplification.WebSimplificationTests.tearDown
    create = test_web_simplification.WebSimplificationTests.create
    ask = test_web_simplification.WebSimplificationTests.ask
    def test_enter_notice_accept_no_model_and_session_reuse(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-consent-required="true"', page)
        self.assertEqual(self.client.post("/cloud-notice", data={"csrf_token": self.csrf}).status_code, 400)
        response = self.client.post("/cloud-notice", data={"csrf_token": self.csrf, "accepted": "yes"}, headers={"Accept": "application/json"})
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(self.llm.calls, [])
        for _ in range(2):
            matter = self.create()
            self.assertIn('data-consent-required="false"', self.client.get(f"/matters/{matter}").get_data(as_text=True))
            self.assertEqual(self.app.extensions["runtime_store"].get_matter(matter)["status"], "ACTIVE")
        self.app.config["NOTICE_VERSION"] = "test-new-version"
        self.assertIn('data-consent-required="true"', self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.llm.calls, [])

    def test_new_browser_cannot_submit_or_retry_confirmed_matter(self):
        matter = self.create(accepted="yes")
        self.ask(matter, "视同销售有哪些？")
        other = self.app.test_client()
        page = other.get(f"/matters/{matter}").get_data(as_text=True)
        self.assertIn('data-consent-required="true"', page)
        with other.session_transaction() as state:
            csrf = state["csrf_token"]
        for endpoint in (f"/matters/{matter}/messages", f"/matters/{matter}/messages/fake/retry"):
            result = other.post(endpoint, data={"csrf_token": csrf}, headers={"Accept": "application/json"})
            self.assertEqual(result.status_code, 409)
            self.assertTrue(result.get_json()["consent_required"])
        self.assertEqual(len(self.llm.calls), 1)

    def test_draft_direct_link_preserves_input_and_version_can_be_reconfirmed(self):
        matter = self.create(content="尚未发送的问题")
        page = self.client.get(f"/matters/{matter}").get_data(as_text=True)
        self.assertIn("尚未发送的问题</textarea>", page)
        self.assertIn('data-consent-required="true"', page)
        self.client.post("/cloud-notice", data={"csrf_token": self.csrf, "accepted": "yes", "matter_id": matter})
        self.assertEqual(self.llm.calls, [])
        self.assertIn("尚未发送的问题</textarea>", self.client.get(f"/matters/{matter}").get_data(as_text=True))
        version = "new-notice-version"
        self.app.config["NOTICE_VERSION"] = version
        self.app.extensions["matter_service"].notice_version = version
        self.client.post("/cloud-notice", data={"csrf_token": self.csrf, "accepted": "yes", "matter_id": matter})
        self.assertEqual(self.app.extensions["runtime_store"].get_matter(matter)["notice_version"], version)

    def test_closed_consultation_reconfirmation_does_not_reopen_it(self):
        matter=self.create(accepted='yes')
        store=self.app.extensions['runtime_store']
        connection=store.connect()
        try:
            with connection:connection.execute("update matters set status='CLOSED',round_count=8 where matter_id=?",(matter,))
        finally:connection.close()
        self.app.config['NOTICE_VERSION']='closed-new-version'
        self.app.extensions['matter_service'].notice_version='closed-new-version'
        self.client.post('/cloud-notice',data={'csrf_token':self.csrf,'accepted':'yes','matter_id':matter})
        row=store.get_matter(matter)
        self.assertEqual(row['status'],'CLOSED')
        self.assertEqual(row['round_count'],8)
        self.assertEqual(row['notice_version'],'closed-new-version')
