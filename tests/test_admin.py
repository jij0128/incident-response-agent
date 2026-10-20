import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langsmith.utils import LangSmithError, LangSmithNotFoundError

import server
from agent.admin import get_client


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ADMIN_API_TOKEN": "test-token",
                                          "LANGCHAIN_PROJECT": "test-project", "LANGSMITH_PROJECT": ""})
        self.env.start()
        self.project = uuid4()
        start = datetime.now(timezone.utc)
        self.run = SimpleNamespace(
            id=uuid4(), session_id=self.project, parent_run_id=None, name="chat_request_stream",
            run_type="chain", start_time=start, end_time=start + timedelta(seconds=2),
            error=None, inputs={"message": "payments-service 장애 진단"},
            outputs={"reply": "확인된 상태입니다.", "last_prompt": "내부 프롬프트",
                     "events": [{"type": "Validation", "attempt": 1, "verdict": "합격"}],
                     "metadata": {"token_count": 123, "cost_usd": 0.01, "llm_backend": "fake",
                                  "validation_passed": True, "validation_attempts": 1}},
            child_runs=[])
        self.provider = Mock()
        self.provider.list_runs.return_value = [self.run]
        self.provider.read_run.return_value = self.run
        self.provider.read_project.return_value = SimpleNamespace(id=self.project)
        self.previous_overrides = dict(server.app.dependency_overrides)
        server.app.dependency_overrides[get_client] = lambda: self.provider
        self.client = TestClient(server.app)
        self.headers = {"X-Admin-Token": "test-token"}

    def tearDown(self):
        self.client.close()
        server.app.dependency_overrides.clear()
        server.app.dependency_overrides.update(self.previous_overrides)
        self.env.stop()

    def test_access_key_required_before_reading_langsmith(self):
        for headers in ({}, {"X-Admin-Token": "wrong-token"}):
            response = self.client.get("/admin/runs", headers=headers)
            self.assertEqual(response.status_code, 401)
        self.provider.list_runs.assert_not_called()

    def test_unconfigured_access_key_disables_admin_api(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            response = self.client.get("/admin/runs", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.provider.list_runs.assert_not_called()

    def test_list_contains_summary_but_not_internal_prompt(self):
        response = self.client.get("/admin/runs", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["project"], "test-project")
        self.assertEqual(body["runs"][0]["tokens"], 123)
        self.assertEqual(body["runs"][0]["duration_ms"], 2000)
        self.assertNotIn("내부 프롬프트", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.provider.list_runs.call_args.kwargs["project_name"], "test-project")
        self.assertTrue(self.provider.list_runs.call_args.kwargs["is_root"])

    def test_detail_includes_prompt_and_validation_history(self):
        response = self.client.get(f"/admin/runs/{self.run.id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["prompt"], "내부 프롬프트")
        self.assertEqual(response.json()["events"][0]["type"], "Validation")
        self.assertEqual(len(response.json()["nodes"]), 1)

    def test_other_projects_and_child_runs_are_not_exposed(self):
        for field, value in (("session_id", uuid4()), ("parent_run_id", uuid4()),
                             ("name", "unrelated_application")):
            with self.subTest(field=field), patch.object(self.run, field, value):
                self.provider.read_run.reset_mock()
                response = self.client.get(f"/admin/runs/{self.run.id}", headers=self.headers)
                self.assertEqual(response.status_code, 404)
                # 소속 확인에 실패하면 하위 트리와 프롬프트를 추가 조회하지 않는다.
                self.assertEqual(self.provider.read_run.call_count, 1)

    def test_limit_is_bounded(self):
        self.assertEqual(self.client.get("/admin/runs?limit=1000", headers=self.headers).status_code, 422)
        self.provider.list_runs.assert_not_called()

    def test_project_before_first_trace_returns_empty_list(self):
        self.provider.list_runs.side_effect = LangSmithNotFoundError("not created")
        response = self.client.get("/admin/runs", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runs"], [])

    def test_provider_failure_returns_clean_error(self):
        self.provider.list_runs.side_effect = LangSmithError("provider internal detail")
        with self.assertLogs("agent.admin", level="ERROR"):
            response = self.client.get("/admin/runs", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("provider internal detail", response.text)


if __name__ == "__main__":
    unittest.main()
