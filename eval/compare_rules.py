"""원본 Git 스냅샷과 현재 작업 트리의 대상 확인/검증 규칙을 고정 사례로 비교한다.

프로젝트 루트에서 .venv/bin/python3 eval/compare_rules.py 로 실행한다.
서버·실제 LLM·MCP·LangSmith는 호출하지 않는다. 원본 코드와 현재 코드는 별도
프로세스에서 import하여 Python 모듈 캐시가 섞이지 않도록 한다.
"""
import argparse
import asyncio
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def fingerprint(source):
    paths = ["agent/graph.py", "agent/grounding.py", "agent/deps.py", "agent/retry.py",
             "data/monitoring.json"]
    return {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in paths if (source / name).exists()}


async def evaluate(source, cases):
    # 모듈을 불러오기 전에 추적을 끈다. .env를 읽거나 서버를 시작하지 않는다.
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    sys.path.insert(0, str(source))
    from agent import graph

    targets = []
    for case in cases["target_cases"]:
        # 원본에는 진입 확인 노드가 없으므로 다음 단계에서 쓰는 시스템명이 무엇인지
        # 확인한다. 이것은 실제 모델의 계획/도구 선택 결과를 측정하는 시험이 아니다.
        state = {"query": case["query"], "events": [], "cost_usd": 0.0}
        checked = await graph.target_check_node(state, None) if hasattr(graph, "target_check_node") else {}
        action = "clarify" if checked.get("route") == "clarification" else "continue"
        target = None if action == "clarify" else graph._extract_system_name(case["query"])
        passed = action == case["action"] and (
            action == "clarify" or target == case["prompt_system"])
        targets.append({**case, "actual_action": action, "actual_prompt_system": target,
                        "reply": checked.get("final"), "passed": passed})

    drafts = []
    for case in cases["draft_cases"]:
        state = {"draft": case["draft"], "collected": copy.deepcopy(cases["evidence"]),
                 "events": [], "validation_attempts": 0}
        result = await graph.validate_node(state, None)
        accepted = result["events"][-1]["verdict"].startswith("합격")
        drafts.append({**case, "actual_accepted": accepted,
                       "passed": accepted == case["expected_accepted"],
                       "feedback": result.get("validation_feedback")})

    state = {"draft": cases["repeated_invalid_draft"], "collected": copy.deepcopy(cases["evidence"]),
             "events": [], "validation_attempts": 0}
    for _ in range(3):
        state.update(await graph.validate_node(state, None))
        if state.get("validation_feedback") is None:
            break
    final = state["draft"]
    repeated = {"attempts": state["validation_attempts"], "verdict": state["events"][-1]["verdict"],
                "final": final, "returned_invalid_draft": final == cases["repeated_invalid_draft"]}
    return {"targets": targets, "drafts": drafts, "repeated_failure": repeated,
            "source_sha256": fingerprint(source)}


def worker(source, cases_path):
    env = {**os.environ, "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false"}
    output = subprocess.check_output(
        [sys.executable, str(Path(__file__).resolve()), "--worker-source", str(source),
         "--cases", str(cases_path)], env=env, cwd=source)
    return json.loads(output)


def summarize(result):
    targets = result["targets"]
    drafts = result["drafts"]
    unknown = [case for case in targets if case["action"] == "clarify"]
    supported = [case for case in drafts if case["group"] == "supported"]
    unsupported = [case for case in drafts if case["group"] == "unsupported"]
    limitations = [case for case in drafts if case["group"] == "known_limitation"]
    return {
        "target_expectations": f"{sum(c['passed'] for c in targets)}/{len(targets)}",
        "unknown_target_continues": f"{sum(c['actual_action'] == 'continue' for c in unknown)}/{len(unknown)}",
        "unsupported_accepts": f"{sum(c['actual_accepted'] for c in unsupported)}/{len(unsupported)}",
        "supported_accepts": f"{sum(c['actual_accepted'] for c in supported)}/{len(supported)}",
        "known_limitations_accepts": f"{sum(c['actual_accepted'] for c in limitations)}/{len(limitations)}",
        "invalid_after_three": "반환" if result["repeated_failure"]["returned_invalid_draft"] else "관측 상태로 대체",
    }


def report_markdown(report):
    before, after = report["baseline"], report["current"]
    a, b = summarize(before), summarize(after)
    labels = [
        ("target_expectations", "대상 확인 단계의 기대 결과 충족 (높을수록 좋음)"),
        ("unknown_target_continues", "미등록 대상인데 조사 흐름 계속 진행 (낮을수록 좋음)"),
        ("unsupported_accepts", "근거 없는 퍼센트 초안의 1차 검증 통과 (낮을수록 좋음)"),
        ("supported_accepts", "정확한 퍼센트 인용 초안의 1차 검증 통과 (유지 필요)"),
        ("known_limitations_accepts", "알려진 한계 사례의 잘못된 통과 (잔여 문제)"),
        ("invalid_after_three", "3회 검증 실패 후 잘못된 초안 처리"),
    ]
    lines = ["# 고정 사례 기반 규칙 비교", "", f"생성 시각: {report['generated_at']}", "",
             f"원본: `{report['baseline_commit']}`", "",
             f"개선본: 실행 당시 작업 트리 (HEAD `{report['current_head']}`, 관련 파일 SHA-256은 JSON에 기록)", "",
             "## 측정 범위", "",
             "같은 입력으로 **대상 확인 단계와 답변 검증 노드**만 실행했다. 실제 모델의 라우팅·계획·답변 생성은 실행하지 않았다.",
             "원본은 대상 확인 노드가 없으므로 다음 단계에 넘기는 시스템명을 관찰했다. 서버나 MCP를 띄우지 않았고, LangSmith 추적도 강제로 끈 별도 프로세스에서 비교했다.",
             "검증용 모니터링 값은 CPU 95.9%, 메모리 57.4%, 알림 CPU 91%/임계치 80%, 매뉴얼 조건 90%로 고정했다. 모델 추정 99.9%는 관측 근거로 인정하지 않는 사례를 포함했다.",
             "이 결과를 실제 LLM의 진단 정확도·응답 지연·비용 개선으로 해석하지 않는다.", "",
             "## 요약", "", "| 측정 항목 | 원본 | 개선본 |", "|---|---:|---:|"]
    lines.extend(f"| {label} | {a[key]} | {b[key]} |" for key, label in labels)
    lines += ["", "## 대상 확인 상세", "", "| 사례 | 질문 | 기대 | 원본 | 개선본 |",
              "|---|---|---|---|---|"]
    def outcome(case):
        return "확인 요청" if case["actual_action"] == "clarify" else f"진행: {case['actual_prompt_system']}"
    for old, new in zip(before["targets"], after["targets"]):
        expected = "확인 요청" if old["action"] == "clarify" else f"진행: {old['prompt_system']}"
        lines.append(f"| {old['id']} | {old['query']} | {expected} | {outcome(old)} | {outcome(new)} |")
    lines += ["", "대상명이 없는 질문의 auth-service 기본값은 기존 동작 유지 확인용이며, 올바른 시스템을 추론했다는 의미가 아니다.",
              "", "## 초안 검증 상세", "", "| 사례 | 분류 | 기대 | 원본 | 개선본 |", "|---|---|---|---|---|"]
    for old, new in zip(before["drafts"], after["drafts"]):
        decision = lambda value: "통과" if value else "반려"
        lines.append(f"| {old['id']} | {old['group']} | {decision(old['expected_accepted'])} | "
                     f"{decision(old['actual_accepted'])} | {decision(new['actual_accepted'])} |")
    lines += ["", "## 3회 검증 실패", "",
              "동일한 짧은 오답 초안을 반복 제출했다. 모델의 재작성 동작을 재현한 것이 아니라 최대 실패 처리만 확인했다.",
              f"- 원본 판정: {before['repeated_failure']['verdict']}",
              f"- 개선 판정: {after['repeated_failure']['verdict']}",
              "", "## 한계와 다음 실험", "",
              "- 메모리의 57.4%를 CPU 값으로 잘못 쓰거나, 근거 없는 12.3초를 쓰는 사례는 개선본도 통과시킨다. 퍼센트 존재 여부 검사의 한계다.",
              "- 통제된 고정 사례이며 일반화된 정확도 통계가 아니다. 원시 입력·관측 결과는 JSON에 저장했다.",
              "- 다음 단계는 동일 모델·입력·고정 모니터링 데이터·실행 조건으로 원본/개선본의 실제 Ollama 답변을 비교하는 것이다.",
              "- 이 단계에서는 모델 응답시간이나 실제 토큰을 측정하지 않았다.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="0672d14")
    parser.add_argument("--cases", type=Path, default=ROOT / "eval/rule_cases.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval/results")
    parser.add_argument("--worker-source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    cases_path = args.cases.resolve()
    if args.worker_source:
        cases = json.loads(cases_path.read_text())
        print(json.dumps(asyncio.run(evaluate(args.worker_source.resolve(), cases)), ensure_ascii=False))
        return

    commit = git("rev-parse", "--verify", "--end-of-options", args.baseline + "^{commit}").decode().strip()
    with tempfile.TemporaryDirectory(prefix="incident-baseline-") as temporary:
        source = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(git("archive", commit))) as archive:
            archive.extractall(source, filter="data")
        if (source / "data/monitoring.json").read_bytes() != (ROOT / "data/monitoring.json").read_bytes():
            raise SystemExit("원본과 개선본의 모니터링 목록이 다릅니다. 같은 목록으로 통제할 방법부터 정하세요.")
        before = worker(source, cases_path)
        after = worker(ROOT, cases_path)

    report = {
        "experiment": "fixed_rule_cases_no_model_calls",
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        "baseline_commit": commit, "current_head": git("rev-parse", "HEAD").decode().strip(),
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "python_version": sys.version.split()[0], "baseline": before, "current": after,
    }
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "rule_comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "rule_comparison.md").write_text(report_markdown(report))
    a, b = summarize(before), summarize(after)
    print("고정 사례 규칙 비교 완료 (실제 LLM 호출 없음)")
    for key, label in [("unknown_target_continues", "미등록 대상 진행"),
                       ("unsupported_accepts", "근거 없는 퍼센트 통과"),
                       ("supported_accepts", "정확한 퍼센트 통과"),
                       ("known_limitations_accepts", "한계 사례 잘못된 통과"),
                       ("invalid_after_three", "3회 실패 후 처리")]:
        print(f"{label}: {a[key]} → {b[key]}")
    print(f"결과: {output / 'rule_comparison.md'}")


if __name__ == "__main__":
    main()
