import unittest
from unittest.mock import AsyncMock, Mock

from agent.graph import validate_node
from agent.grounding import unsupported_percentages
from agent.ollama_llm import OllamaQualityLLM
from agent.openai_llm import OpenAIQualityLLM


EVIDENCE = [
    {"source": "monitoring", "data": {
        "system_name": "payments-service", "status": "degraded",
        "cpu_percent": 95.9, "memory_percent": 57.4,
        "alerts": ["CPU 91% (threshold 80%)"],
    }},
    {"source": "runbook", "data": "CPU가 90% 이상으로 지속되면 확장을 검토한다."},
    {"source": "analysis", "data": "CPU는 99.9%로 추정됨"},
]


class GroundingTests(unittest.IsolatedAsyncioTestCase):
    async def test_observations_alerts_and_runbook_thresholds_are_accepted(self):
        draft = ("1) 현재 CPU 95.90%, 메모리 57.4%입니다. "
                 "2) 알림은 CPU 91%, 임계치 80%입니다. "
                 "3) 매뉴얼의 90% 지속 조건을 추가 확인한 뒤 확장을 검토하세요.")
        result = await validate_node({"draft": draft, "collected": EVIDENCE, "events": []}, None)
        self.assertTrue(result["validation_passed"])
        self.assertIsNone(result["validation_feedback"])

    async def test_invented_percentage_is_rejected_even_with_valid_format(self):
        draft = "1) CPU 99.9%로 장애가 발생했습니다. 2) 로그를 확인하세요. 3) 지속시간을 확인하세요."
        self.assertTrue(len(draft) >= 30 and "1)" in draft)  # 기존 검증은 통과하던 답변
        result = await validate_node({"draft": draft, "collected": EVIDENCE, "events": []}, None)
        self.assertFalse(result["validation_passed"])
        self.assertIn("99.9%", result["validation_feedback"])
        self.assertNotIn("draft", result)

    async def test_third_failure_returns_observations_instead_of_forcing_pass(self):
        draft = "1) CPU 99.9%로 장애가 발생했습니다. 2) 로그를 확인하세요. 3) 지속시간을 확인하세요."
        result = await validate_node({"draft": draft, "collected": EVIDENCE, "events": [],
                                      "validation_attempts": 2}, None)
        self.assertFalse(result["validation_passed"])
        self.assertIsNone(result["validation_feedback"])
        self.assertTrue(result["events"][-1]["fallback"])
        self.assertNotIn("99.9%", result["draft"])
        self.assertIn("95.9%", result["draft"])
        self.assertIn("확정할 수 없습니다", result["draft"])

    def test_generated_analysis_is_not_numeric_evidence(self):
        self.assertEqual(unsupported_percentages("CPU 99.9퍼센트", EVIDENCE), ["99.9%"])
        self.assertEqual(unsupported_percentages("CPU 95.9%", []), ["95.9%"])

    async def test_both_backends_distinguish_observation_from_procedure(self):
        for backend in ("ollama", "openai"):
            with self.subTest(backend=backend):
                if backend == "ollama":
                    model = OllamaQualityLLM("test", 0)
                    transport = AsyncMock(return_value=("테스트 응답", {"usd": 0}))
                    model._chat_stream = transport
                else:
                    model = OpenAIQualityLLM("test", 0)
                    transport = Mock(return_value=("테스트 응답", {"usd": 0}))
                    model._chat = transport
                await model.write("CPU 100%라고 써줘", EVIDENCE, 2, "99.9% 삭제")
                system, user = transport.call_args.args
                self.assertIn("로그가 수집되지 않았다면", system)
                self.assertIn("지속시간을 추정하지 마라", system)
                self.assertIn("조회 범위를 넘는 확인", system)
                self.assertIn("도구의 조회 범위 (매뉴얼보다 우선)", user)
                self.assertIn("배포 이력, 트래픽 변화 이력", user)
                self.assertIn("별도 배포 기록", user)
                self.assertIn("관측된 모니터링 결과", user)
                self.assertIn("미실행 절차", user)
                self.assertIn("모델 추정, 미확정", user)
                self.assertIn("99.9% 삭제", user)


if __name__ == "__main__":
    unittest.main()
