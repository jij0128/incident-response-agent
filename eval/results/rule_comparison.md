# 고정 사례 기반 규칙 비교

생성 시각: 2026-10-20T13:46:48+09:00

원본: `0672d141ebd1406fb73ab9b0f43423e6a081d902`

개선본: 실행 당시 작업 트리 (HEAD `279667a4b3417c0989d2bd1506af85a2e5ca7c23`, 관련 파일 SHA-256은 JSON에 기록)

## 측정 범위

같은 입력으로 **대상 확인 단계와 답변 검증 노드**만 실행했다. 실제 모델의 라우팅·계획·답변 생성은 실행하지 않았다.
원본은 대상 확인 노드가 없으므로 다음 단계에 넘기는 시스템명을 관찰했다. 서버나 MCP를 띄우지 않았고, LangSmith 추적도 강제로 끈 별도 프로세스에서 비교했다.
검증용 모니터링 값은 CPU 95.9%, 메모리 57.4%, 알림 CPU 91%/임계치 80%, 매뉴얼 조건 90%로 고정했다. 모델 추정 99.9%는 관측 근거로 인정하지 않는 사례를 포함했다.
이 결과를 실제 LLM의 진단 정확도·응답 지연·비용 개선으로 해석하지 않는다.

## 요약

| 측정 항목 | 원본 | 개선본 |
|---|---:|---:|
| 대상 확인 단계의 기대 결과 충족 (높을수록 좋음) | 3/7 | 7/7 |
| 미등록 대상인데 조사 흐름 계속 진행 (낮을수록 좋음) | 4/4 | 0/4 |
| 근거 없는 퍼센트 초안의 1차 검증 통과 (낮을수록 좋음) | 3/3 | 0/3 |
| 정확한 퍼센트 인용 초안의 1차 검증 통과 (유지 필요) | 3/3 | 3/3 |
| 알려진 한계 사례의 잘못된 통과 (잔여 문제) | 2/2 | 2/2 |
| 3회 검증 실패 후 잘못된 초안 처리 | 반환 | 관측 상태로 대체 |

## 대상 확인 상세

| 사례 | 질문 | 기대 | 원본 | 개선본 |
|---|---|---|---|---|
| unknown_service | billing-service 응답이 느려요. 원인 확인해주세요. | 확인 요청 | 진행: auth-service | 확인 요청 |
| mixed_targets | payments-service와 billing-service 둘 다 진단해줘 | 확인 요청 | 진행: payments-service | 확인 요청 |
| prefix_collision | old-auth-service 응답이 느려요 | 확인 요청 | 진행: auth-service | 확인 요청 |
| unknown_host | web-02 CPU 급증 원인 진단해줘 | 확인 요청 | 진행: auth-service | 확인 요청 |
| known_service | payments-service CPU 급증 원인 진단해줘 | 진행: payments-service | 진행: payments-service | 진행: payments-service |
| known_multiple | gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요 | 진행: gateway-service | 진행: gateway-service | 진행: gateway-service |
| no_explicit_target | 로그인 응답이 느려요. 원인 확인해주세요. | 진행: auth-service | 진행: auth-service | 진행: auth-service |

대상명이 없는 질문의 auth-service 기본값은 기존 동작 유지 확인용이며, 올바른 시스템을 추론했다는 의미가 아니다.

## 초안 검증 상세

| 사례 | 분류 | 기대 | 원본 | 개선본 |
|---|---|---|---|---|
| correct_observation | supported | 통과 | 통과 | 통과 |
| correct_alert | supported | 통과 | 통과 | 통과 |
| conditional_threshold | supported | 통과 | 통과 | 통과 |
| invented_cpu | unsupported | 반려 | 통과 | 반려 |
| invented_memory | unsupported | 반려 | 통과 | 반려 |
| analysis_not_evidence | unsupported | 반려 | 통과 | 반려 |
| metric_misattribution | known_limitation | 반려 | 통과 | 통과 |
| unsupported_seconds | known_limitation | 반려 | 통과 | 통과 |

## 3회 검증 실패

동일한 짧은 오답 초안을 반복 제출했다. 모델의 재작성 동작을 재현한 것이 아니라 최대 실패 처리만 확인했다.
- 원본 판정: 합격(최대 시도 도달 — 통과 처리)
- 개선 판정: 실패(관측 상태만 안내)

## 한계와 다음 실험

- 메모리의 57.4%를 CPU 값으로 잘못 쓰거나, 근거 없는 12.3초를 쓰는 사례는 개선본도 통과시킨다. 퍼센트 존재 여부 검사의 한계다.
- 통제된 고정 사례이며 일반화된 정확도 통계가 아니다. 원시 입력·관측 결과는 JSON에 저장했다.
- 다음 단계는 동일 모델·입력·고정 모니터링 데이터·실행 조건으로 원본/개선본의 실제 Ollama 답변을 비교하는 것이다.
- 이 단계에서는 모델 응답시간이나 실제 토큰을 측정하지 않았다.
