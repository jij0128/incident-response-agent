"""실제 LLM/외부 모니터링 없이 대상 확인과 API 응답을 검증한다."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import KNOWN_SYSTEMS, build_graph
from agent.session_store import SqliteSessionStore


class MonitoringStub:
    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, *args, **kwargs):
        return "모니터링과 대응 매뉴얼을 조회하세요."

    async def call_tool(self, name, arguments):
        return {"system_name": arguments["system_name"], "status": "degraded",
                "cpu_percent": 91.0, "alerts": ["cpu high"]}

    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, *args, **kwargs):
        return "# CPU 대응\n1. 상태 확인\n2. 로그 확인\n3. 지속시간 확인"


class TargetCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_targets_stop_before_any_llm_or_mcp_call(self):
        queries = {
            "billing-service 응답이 느려요": ["billing-service"],
            "payments-service와 billing-service 진단해줘": ["billing-service"],
            "old-auth-service 응답이 느려요": ["old-auth-service"],
            "payments-service-v2 장애": ["payments-service-v2"],
            "web-02 장애": ["web-02"],
            "billing_service 장애": ["billing_service"],
            "billing-service랑 billing-service 확인": ["billing-service"],
            "billing-service와 inventory-service 장애": ["billing-service", "inventory-service"],
        }
        for query, unknown in queries.items():
            with self.subTest(query=query):
                # 속성 접근부터 실패하게 해 모델/모니터링 호출이 없음을 검증한다.
                graph = build_graph(SimpleNamespace())
                result = await graph.ainvoke({"query": query, "events": [], "cost_usd": 0.0})
                self.assertEqual(result["route"], "clarification")
                self.assertEqual(result["events"][0]["unknown_systems"], unknown)
                self.assertIn("시스템명을 확인해주세요", result["final"])
                self.assertFalse(result.get("tasks"))
                self.assertEqual(result["cost_usd"], 0.0)

    async def test_registered_systems_still_complete_diagnosis(self):
        deps = SupervisorDeps(FastLLM("fast", 0), QualityLLM("quality", 0),
                              MonitoringStub(), None, "v2")
        graph = build_graph(deps)
        for name in sorted(KNOWN_SYSTEMS):
            with self.subTest(system=name):
                result = await graph.ainvoke({"query": f"{name} 장애 진단해줘", "events": []})
                targets = [e["target"] for e in result["events"]
                           if e["type"] == "TaskCompleted" and e["source"] == "monitoring"]
                self.assertIn(name, targets)
                self.assertEqual(result["route"], "incident")
                self.assertTrue(result["final"])

    async def test_chat_and_unspecified_target_keep_existing_routing(self):
        for query, route in [("안녕, 너는 누구야?", "chat"),
                             ("read-only 모드가 뭐야?", "chat"),
                             ("로그인 장애 진단해줘", "incident")]:
            with self.subTest(query=query):
                fast = Mock()
                fast.route.return_value = ({"route": route, "needs_analysis": False}, {"usd": 0})
                deps = SupervisorDeps(fast, None, None, None, "v2")
                # route까지만 소비하면 기존 LLM 라우팅으로 전달되었는지 확인할 수 있다.
                async for update in build_graph(deps).astream({"query": query, "events": []}):
                    if "route" in update:
                        self.assertEqual(update["route"]["route"], route)
                        break
                fast.route.assert_called_once_with(query)


class TargetCheckApiTests(unittest.TestCase):
    def setUp(self):
        import server

        self.store = SqliteSessionStore(":memory:")
        deps = SupervisorDeps(FastLLM("fast", 0), QualityLLM("quality", 0),
                              MonitoringStub(), self.store, "v2")
        # lifespan을 실행하지 않고 테스트 전용 그래프와 메모리 DB를 주입한다.
        self.state = state = server.app.state
        self.original = dict(state._state)
        state.deps = deps
        state.graph = build_graph(deps)
        state.llm_backend = "test"
        state.instance_id = "test"
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.state._state.clear()
        self.state._state.update(self.original)

    def stream_events(self, query, thread_id):
        response = self.client.get("/chat/stream", params={"message": query, "thread_id": thread_id})
        self.assertEqual(response.status_code, 200)
        data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(data[-1], "[DONE]")
        return [json.loads(item) for item in data[:-1]]

    def test_chat_and_stream_return_clarification_and_save_history(self):
        response = self.client.post("/chat", json={"message": "billing-service 응답이 느려요",
                                                   "thread_id": "post-test"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("`billing-service`는 등록된", response.json()["reply"])
        self.assertEqual(response.json()["metadata"]["task_count"], 0)

        events = self.stream_events("billing-service 응답이 느려요", "stream-test")
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertEqual(final["reply"], self.store.load("post-test")[1]["content"])
        self.assertEqual(self.store.load("stream-test")[1]["content"], final["reply"])

    def test_registered_system_stream_completes_after_empty_target_check(self):
        events = self.stream_events("payments-service CPU 급증 원인 진단해줘", "known-stream")
        targets = [e["target"] for e in events
                   if e["type"] == "TaskCompleted" and e["source"] == "monitoring"]
        self.assertIn("payments-service", targets)
        self.assertFalse(any(e["type"] == "TargetClarification" for e in events))
        self.assertTrue(any(e["type"] == "AnswerToken" for e in events))
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertIn("payments-service", final["reply"])
        self.assertEqual(self.store.load("known-stream")[1]["content"], final["reply"])

    def test_chat_stream_completes_after_empty_target_check(self):
        events = self.stream_events("안녕, 너는 누구야?", "chat-stream")
        self.assertTrue(any(e["type"] == "AnswerToken" for e in events))
        self.assertTrue(any(e["type"] == "FinalAnswer" for e in events))

    def test_repeated_bad_percentages_finish_without_passing_invalid_draft(self):
        invalid = "1) CPU 99.9%로 장애가 발생했습니다. 2) 로그를 확인하세요. 3) 지속시간을 확인하세요."
        writer = AsyncMock(return_value=(invalid, {"usd": 0}))
        self.state.deps.quality_llm.write = writer
        query = "payments-service CPU 급증 원인 진단해줘"

        response = self.client.post("/chat", json={"message": query, "thread_id": "bad-post"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["metadata"]["validation_passed"])
        self.assertEqual(body["metadata"]["validation_attempts"], 3)
        self.assertNotIn("99.9%", body["reply"])

        events = self.stream_events(query, "bad-stream")
        validations = [e for e in events if e["type"] == "Validation"]
        self.assertEqual(len(validations), 3)
        self.assertTrue(validations[-1]["fallback"])
        self.assertNotEqual(validations[-1]["verdict"], "합격")
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertNotIn("99.9%", final["reply"])
        self.assertIn("91.0%", final["reply"])


if __name__ == "__main__":
    unittest.main()
