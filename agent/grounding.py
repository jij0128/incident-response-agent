"""답변 생성 지시와, 추가 LLM 호출 없이 수행하는 퍼센트 인용 검사.

수치의 출처 존재 여부만 검사한다. 동일 수치의 잘못된 서비스/지표 귀속이나
자연어 주장의 참/거짓까지 보장하는 의미 검증기는 아니다.
"""
import re
from decimal import Decimal


REPORT_SYSTEM_PROMPT = (
    "당신은 ICT 운영 보고 작성자다. 한국어로 간결하게 답하라. "
    "1) 확인된 사실 2) 추가 확인 사항 3) 조건부 권고 순서로 작성하라. "
    "모니터링 결과만 관측 사실이다. 현재 수치와 알림에 기록된 수치/임계치는 구분하라. "
    "매뉴얼은 앞으로 수행할 점검 절차이며 실제 수행 결과가 아니다. "
    "도구의 조회 범위는 제공된 기능 설명을 따르라. "
    "매뉴얼이 도구의 조회 범위를 넘는 확인을 지시해도 그 도구로 조회할 수 있다고 안내하지 마라. "
    "제공되지 않는 이력이나 로그는 미수집으로 표시하고 필요한 별도 자료를 요청하라. "
    "로그가 수집되지 않았다면 로그 분석 결과나 특정 패턴이 없다고 단정하지 말고 "
    "'로그 미수집으로 확인되지 않음'이라고 밝혀라. "
    "단일 시점 수치로 지속시간을 추정하지 마라. "
    "패턴 분석은 모델의 추정이며 원인 확정의 근거로 사용하지 마라. "
    "수치는 제공된 값을 그대로 인용하고 새 수치나 임계치를 만들지 마라. "
    "매뉴얼의 조치 조건(예: 높은 CPU가 지속되면)을 생략하지 말고, "
    "조건이 확인되지 않으면 추가 확인 후 조치를 검토하도록 권고하라. "
    "실제 재시작/확장/롤백을 수행했다고 표현하지 마라."
)


MONITORING_SCOPE = (
    "- get_monitoring_status는 시스템명, 현재 상태, CPU/메모리 사용률, 조회 시각, 알림 문구를 반환한다.\n"
    "- 배포 이력, 트래픽 변화 이력, 시계열 지표, 로그 원문, 프로파일링 결과는 조회할 수 없다.\n"
    "- 알림에 적힌 지연시간/커넥션 풀 수치 등은 알림 내용으로만 인용한다. "
    "조회 시각이나 단일 수치로 장애 지속시간 또는 변화 추이를 판단하지 않는다.\n"
    "- 배포 시점은 별도 배포 기록, 트래픽 변화/CPU 지속 여부는 시계열 지표, "
    "요청 패턴은 로그 원문을 확보해야 확인할 수 있다. "
    "이 자료가 필요하면 미수집임을 밝히고 별도 자료를 요청한다."
)


def render_evidence(query: str, collected: list[dict], feedback: str | None) -> str:
    sections = {"monitoring": [], "runbook": [], "analysis": []}
    for item in collected:
        source, data = item.get("source"), item.get("data")
        if source == "monitoring":
            sections[source].append(
                f"- {data['system_name']}: 상태={data['status']}, "
                f"CPU {data['cpu_percent']}%, 메모리 {data.get('memory_percent', '미수집')}"
                f"{'%' if 'memory_percent' in data else ''}, 알림={data.get('alerts', [])}")
        elif source in ("runbook", "analysis"):
            sections[source].append(str(data).strip())
    user = (
        f"## 사용자 요청 (관측 사실 아님)\n{query}\n\n"
        f"## 도구의 조회 범위 (매뉴얼보다 우선)\n{MONITORING_SCOPE}\n\n"
        "## 관측된 모니터링 결과\n" + ("\n".join(sections["monitoring"]) or "없음") +
        "\n\n## 대응 매뉴얼 (미실행 절차, 조건부 권고에만 사용)\n" +
        ("\n".join(sections["runbook"]) or "없음") +
        "\n\n## 패턴 분석 (모델 추정, 미확정)\n" +
        ("\n".join(sections["analysis"]) or "없음")
    )
    if feedback:
        user += f"\n\n## 이전 초안의 검증 반려 사유 (반드시 수정)\n{feedback}"
    return user


def _percentages(text: str) -> set[Decimal]:
    return {Decimal(value) for value in re.findall(
        r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*(?:%|퍼센트)", text)}


def unsupported_percentages(draft: str, collected: list[dict]) -> list[str]:
    allowed = set()
    for item in collected:
        data = item.get("data")
        if item.get("source") == "monitoring":
            for field in ("cpu_percent", "memory_percent"):
                if field in data:
                    allowed.add(Decimal(str(data[field])))
            allowed.update(_percentages(" ".join(data.get("alerts", []))))
        elif item.get("source") == "runbook":
            allowed.update(_percentages(str(data)))
    # 사용자 요청이나 모델이 생성한 analysis는 숫자의 근거로 인정하지 않는다.
    return [f"{value.normalize():f}%" for value in sorted(_percentages(draft) - allowed)]


def observed_fallback(collected: list[dict]) -> str:
    """반복 검증 실패 시 모델의 초안 대신 실제 수집한 필드만 그대로 보여준다."""
    lines = ["답변 검증을 완료하지 못해 진단을 확정할 수 없습니다. 수집된 상태만 안내합니다."]
    for item in collected:
        if item.get("source") == "monitoring":
            data = item["data"]
            lines.append(f"- {data['system_name']}: 상태={data['status']}, CPU {data['cpu_percent']}%")
    lines.append("원인과 조치는 로그 및 추가 관측을 확인한 뒤 판단해주세요.")
    return "\n".join(lines)
