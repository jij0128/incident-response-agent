# 회귀 테스트용 쿼리 목록 — 시스템명 하드코딩 수정 검증

| # | 쿼리 | 테스트 목적 |
|---|---|---|
| 1 | payments-service 결제 응답이 느려지고 CPU가 치솟았어요, 원인 확인해주세요 | 기존엔 auth-service로 조용히 되돌아갔던 4개 시스템 중 하나 — KNOWN_SYSTEMS 인식 확인 (cpu-spike runbook) |
| 2 | cache-service 메모리 사용률이 계속 올라가고 있어요 | 미인식 시스템 확인 2 (memory-leak runbook) |
| 3 | log-service 디스크 공간이 부족하다는 알림이 왔어요 | 미인식 시스템 확인 3 (disk-full runbook) |
| 4 | gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요 | 두 시스템 동시 언급 — system_name은 먼저 등장한 gateway-service 하나만 뽑히는지, 그런데도 research task는 두 시스템 다 조사하는지(비교 분석 영향 없음) 확인 |
| 5 | web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘 | 기본 2개 시스템 동시 언급 — 기존 동작(web-01, auth-service 둘 다 조사) 회귀 확인 |
| 6 | payments-service랑 cache-service 둘 다 저하됐는데 공통 원인이 있는지 분석해줘 | needs_analysis=true 유도 — gate(W6)·analysis_node 비교 분석이 여전히 동작하는지 확인 |
| 7 | 로그인 응답이 느려요, 원인 확인해주세요 | 시스템명 미언급 — auth-service 폴백 확인 |
| 8 | billing-service 응답이 느려요 | 미등록 시스템 확인 안내를 반환하고, LLM 및 MCP를 호출하지 않는지 확인. auth-service로 대체하면 실패 |
| 9 | 안녕, 너는 누구야? | route=chat 직행(Wave 5) — 토큰 스트리밍(AnswerToken)이 채팅 경로에서도 실시간으로 오는지 확인 |
| 10 | 제 이름은 민수입니다 → (다음 메시지로) 제 이름이 뭐였죠? | Wave 12 세션 기억 — 서버 재시작 후에도 유지되는지, 스트리밍과 충돌 없는지 확인 |
| 11 | payments-service와 billing-service 둘 다 진단해줘 | 미등록 대상이 섞여 있으면 전체 진단을 멈추고 billing-service 이름 확인 요청 |
| 12 | old-auth-service 응답이 느려요 | auth-service 부분 문자열을 등록된 대상으로 오인하지 않는지 확인 |

## 미등록 시스템 확인 개선 (v2)

- 그래프 시작 시 `target_check`에서 `data/monitoring.json`의 시스템 목록과 대조한다.
- 미등록 식별자가 있으면 대상 확인 답변으로 종료한다. `/chat`과 `/chat/stream`에 모두 적용된다.
- 검사 범위는 `-service`/`_service`가 포함된 식별자와 `web-02` 같은 숫자 접미사 호스트명이다.
- 등록된 이름은 대소문자를 포함해 정확히 일치해야 한다. 자연어 별칭과 이름이 생략된 요청, legacy v1의 처리는 이번 변경 범위에 포함하지 않는다.
- 실행: 프로젝트 폴더에서 `LANGCHAIN_TRACING_V2=false .venv/bin/python3 -m unittest discover -s tests -v`
