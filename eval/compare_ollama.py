"""고정된 모니터링 데이터로 원본과 개선본 그래프를 실제 로컬 Ollama에서 비교한다."""
import argparse
import asyncio
import copy
from datetime import datetime
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from compare_rules import ROOT, fingerprint, git


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


class FixedMCP:
    """MCP 응답만 고정한다. 실제 MCP 전송과 무작위 변동은 포함하지 않는다."""
    def __init__(self, source, data):
        self.source, self.data = source, data
        self.monitoring_calls = []

    async def find_prompt(self, name):
        return 'ops'

    async def get_prompt(self, name, arguments, **kwargs):
        return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {arguments['system_name']}
- 증상: {arguments['symptom']}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""

    async def call_tool(self, name, arguments):
        system = arguments['system_name']
        self.monitoring_calls.append(system)
        return copy.deepcopy(self.data.get(system, {
            'system_name': system, 'status': 'unknown', 'cpu_percent': 0.0,
            'memory_percent': 0.0, 'last_checked': '2026-10-20T00:00:00+00:00',
            'alerts': [f"System '{system}' not found in monitoring"]}))

    async def find_resource_template(self, name):
        return 'ops', 'runbook://incident-response/{incident_type}'

    async def read_resource(self, uri, **kwargs):
        incident_type = uri.rsplit('/', 1)[-1]
        path = self.source / 'data/runbooks' / f'{incident_type}.md'
        if not path.is_file():
            return f'# Runbook Not Found\n\nNo runbook for incident type: {incident_type}'
        return path.read_text()


class MeasuredLLM:
    def __init__(self, model, calls, role):
        self.model, self.calls, self.role = model, calls, role

    def __getattr__(self, name):
        method = getattr(self.model, name)
        if name not in ('route', 'plan', 'analyze', 'chat', 'write'):
            return method

        def record(started, result):
            output, usage = result
            entry = {'role': self.role, 'method': name, 'model': self.model.name,
                     'elapsed_seconds': round(time.perf_counter() - started, 4),
                     'tokens': usage.get('tokens', 0), 'estimated_cost_usd': usage.get('usd', 0),
                     'output': output, 'prompt': usage.get('prompt')}
            self.calls.append(entry)
            print(f"  {self.role}.{name} 완료: {entry['elapsed_seconds']:.2f}초 / {entry['tokens']}토큰",
                  file=sys.stderr, flush=True)
            return result

        if inspect.iscoroutinefunction(method):
            async def async_call(*args, **kwargs):
                started = time.perf_counter()
                print(f'  {self.role}.{name} 실행 중', file=sys.stderr, flush=True)
                return record(started, await method(*args, **kwargs))
            return async_call

        def sync_call(*args, **kwargs):
            started = time.perf_counter()
            print(f'  {self.role}.{name} 실행 중', file=sys.stderr, flush=True)
            return record(started, method(*args, **kwargs))
        return sync_call


async def run_worker(args, fixture):
    # .env와 서버를 불러오지 않고, 프로세스 안에서 LangSmith 전송을 끈다.
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    os.environ['LANGSMITH_TRACING'] = 'false'
    sys.path.insert(0, str(args.worker_source))
    from agent import graph, ollama_llm
    from agent.deps import SupervisorDeps

    ollama_llm.OLLAMA_URL = args.url.rstrip('/') + '/api/chat'
    calls = []
    fast = MeasuredLLM(ollama_llm.OllamaFastLLM(args.fast_model, 0.00015), calls, 'fast')
    quality = MeasuredLLM(ollama_llm.OllamaQualityLLM(args.quality_model, 0.005), calls, 'quality')
    mcp = FixedMCP(args.worker_source, fixture['monitoring'])
    deps = SupervisorDeps(fast, quality, mcp, None, 'v2')
    compiled = graph.build_graph(deps)
    case = next(case for case in fixture['cases'] if case['id'] == args.case_id)
    started = time.perf_counter()
    try:
        result = await compiled.ainvoke({'query': case['query'], 'history': [], 'thread_id': 'benchmark',
                                        'events': [], 'cost_usd': 0.0})
        error = None
    except Exception as exc:
        result, error = {}, f'{type(exc).__name__}: {exc}'
    return {
        'case_id': case['id'], 'query': case['query'], 'requested_systems': case['requested_systems'],
        'unregistered': case['unregistered'], 'manual_review_criteria': case['manual_review'],
        'version': args.label, 'repeat': args.repeat_index,
        'elapsed_seconds': round(time.perf_counter() - started, 4), 'error': error,
        'model_calls': calls, 'total_tokens': sum(call['tokens'] for call in calls),
        'estimated_cost_usd': round(sum(call['estimated_cost_usd'] for call in calls), 6),
        'monitoring_targets': mcp.monitoring_calls, 'route': result.get('route'),
        'validation_attempts': result.get('validation_attempts', 0),
        'validation_passed': result.get('validation_passed'),
        'final': result.get('final'), 'events': result.get('events', []),
        'collected': result.get('collected', []), 'manual_review': '미검토',
    }


def source_hashes(source):
    result = fingerprint(source)
    paths = [source / 'agent/ollama_llm.py', *sorted((source / 'data/runbooks').glob('*.md'))]
    result.update({str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in paths})
    return result


def render_report(report):
    lines = ['# 실제 Ollama 고정 데이터 비교', '', f"생성 시각: {report['generated_at']}", '',
             f"진행 상태: {'완료' if report['complete'] else '진행 중 또는 중단'}", '',
             '## 실험 조건', '',
             f"- 원본: `{report['baseline_commit']}` / 개선본: 실행 당시 작업 트리 (HEAD `{report['current_head']}`).",
             f"- 모델: `{report['fast_model']}` / `{report['quality_model']}`. 설치된 모델 digest는 JSON에 기록.",
             f"- 사례별 반복: {report['repeats']}회. 실행 순서는 사례/반복별로 교대했다.",
             '- 원본·개선본의 LangGraph와 Ollama LLM 호출부를 실제로 실행했다. temperature=0 등 원본의 모델 설정을 유지했다.',
             '- MCP 경계는 고정 응답으로 대체했다. 무작위 모니터링 변동·실제 MCP 전송·의도적 첫 타임아웃은 포함하지 않는다. runbook 파일은 원본/개선본이 같은지 확인했다.',
             '- 웹 서버·브라우저·SQLite는 실행하지 않았다. 지연은 그래프 실행 구간이며 HTTP/SSE 웹 전달 시간은 포함하지 않는다.',
             '- 매 측정 전 두 모델에 num_predict=1의 별도 워밍업을 했다. 워밍업 시간/토큰은 표에 포함하지 않았다.',
             '- 토큰은 Ollama가 반환한 prompt_eval_count + eval_count의 누적값이다. 가상 단가 비용은 실제 청구액이 아니며 로컬 추론의 외부 API 과금은 없다.',
             '- LangSmith 추적은 강제로 끄고 .env를 읽지 않았다. 답변과 프롬프트는 로컬 결과 JSON에만 기록했다.',
             '', '## 측정 결과', '',
             '| 사례 | 반복 | 버전 | 시간(초) | 토큰 | LLM 호출 | 검증 횟수 | 실제 조회 대상 |',
             '|---|---:|---|---:|---:|---:|---:|---|']
    for case in report['case_order']:
        for item in sorted([r for r in report['runs'] if r['case_id'] == case],
                           key=lambda r: (r['repeat'], r['version'] != 'baseline')):
            label = '원본' if item['version'] == 'baseline' else '개선'
            if item['error']:
                label += ' (실패)'
            lines.append(f"| {case} | {item['repeat']} | {label} | {item['elapsed_seconds']:.2f} | "
                         f"{item['total_tokens']} | {len(item['model_calls'])} | {item['validation_attempts']} | "
                         f"{', '.join(item['monitoring_targets']) or '없음'} |")
    lines += ['', '## 미등록 대상 처리', '']
    for label in ('baseline', 'current'):
        selected = [r for r in report['runs'] if r['version'] == label and r['unregistered'] and not r['error']]
        investigated = sum(bool(r['monitoring_targets']) for r in selected)
        clarified = sum(r['route'] == 'clarification' for r in selected)
        lines.append(f"- {label}: 미등록 대상 포함 {len(selected)}건 중 모니터링 진행 {investigated}건, 확인 요청 경로 {clarified}건.")
    lines += ['', '## 답변 원문과 수동 확인 기준', '']
    for item in report['runs']:
        lines += [f"### {item['case_id']} / {item['version']} / 반복 {item['repeat']}", '',
                  f"질문: {item['query']}", '', '```text', item['final'] or item['error'] or '(응답 없음)',
                  '```', '', '수동 확인 기준: ' + ' / '.join(item['manual_review_criteria']), '']
    lines += ['## 해석 시 주의', '',
              '- 미등록 대상 조기 종료는 요청당 처리량과 답변 성격을 바꾼다. 등록 대상의 답변 생성 속도와 합쳐 평균 내지 않는다.',
              '- 소수 사례·소수 반복의 관찰이다. temperature=0이어도 완전한 재현성이나 일반화된 정확도를 보장하지 않는다.',
              '- 모델 캐시·메모리 상태·다른 작업에 따른 변동이 남는다. 단일 실행의 속도 차이를 성능 향상률로 일반화하지 않는다.',
              '- 원시 답변을 수동 검토하기 전에는 근거 없는 단정이 줄었다고 자동 판정하지 않는다. 판정 기준과 근거는 별도 검토 기록에 남긴다.', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='0672d14')
    parser.add_argument('--cases', type=Path, default=ROOT / 'eval/ollama_cases.json')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'eval/results')
    parser.add_argument('--url', default='http://127.0.0.1:11434')
    parser.add_argument('--fast-model', default='qwen3:0.6b')
    parser.add_argument('--quality-model', default='qwen3:8b')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--worker-source', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--case-id', help=argparse.SUPPRESS)
    parser.add_argument('--label', help=argparse.SUPPRESS)
    parser.add_argument('--repeat-index', type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    url = urlparse(args.url)
    if (url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1')
            or url.username or url.password or url.query or url.fragment or url.path not in ('', '/')):
        parser.error('--url은 인증 정보 없는 로컬 HTTP 주소만 허용합니다.')
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats는 1~10입니다.')
    fixture = json.loads(args.cases.read_text())
    if args.worker_source:
        print(json.dumps(asyncio.run(run_worker(args, fixture)), ensure_ascii=False))
        return

    import httpx
    with httpx.Client(timeout=300, trust_env=False) as client:
        response = client.get(args.url.rstrip('/') + '/api/tags')
        response.raise_for_status()
        installed = {model['name']: model for model in response.json()['models']}
        models = list(dict.fromkeys([args.fast_model, args.quality_model]))
        for model in models:
            if model not in installed:
                parser.error(f'설치되지 않은 모델입니다: {model}')

        commit = git('rev-parse', '--verify', '--end-of-options', args.baseline + '^{commit}').decode().strip()
        report = {
            'experiment': 'real_ollama_graph_fixed_mcp',
            'generated_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'),
            'baseline_commit': commit, 'current_head': git('rev-parse', 'HEAD').decode().strip(),
            'fast_model': args.fast_model, 'quality_model': args.quality_model,
            'models': {model: installed[model] for model in models},
            'repeats': args.repeats, 'case_order': [case['id'] for case in fixture['cases']],
            'cases_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'runs': [], 'warmups': [], 'complete': False,
        }
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        def save():
            write_json(output / 'ollama_comparison.json', report)
            (output / 'ollama_comparison.md').write_text(render_report(report))

        with tempfile.TemporaryDirectory(prefix='incident-ollama-baseline-') as temporary:
            source = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(git('archive', commit))) as archive:
                archive.extractall(source, filter='data')
            before_hashes, after_hashes = source_hashes(source), source_hashes(ROOT)
            for path in before_hashes:
                if path.startswith('data/') and before_hashes[path] != after_hashes.get(path):
                    raise SystemExit(f'원본/개선본 데이터가 다릅니다: {path}')
            report['source_sha256'] = {'baseline': before_hashes, 'current': after_hashes}
            save()
            for repeat in range(1, args.repeats + 1):
                for index, case in enumerate(fixture['cases']):
                    order = [('baseline', source), ('current', ROOT)]
                    if (index + repeat) % 2 == 0:
                        order.reverse()
                    for label, version_source in order:
                        print(f"[{case['id']} / {label} / {repeat}] 모델 워밍업", flush=True)
                        for model in models:
                            warm = client.post(args.url.rstrip('/') + '/api/generate', json={
                                'model': model, 'prompt': 'hi', 'stream': False, 'keep_alive': '10m',
                                'options': {'num_predict': 1}})
                            warm.raise_for_status()
                            report['warmups'].append({'case_id': case['id'], 'version': label,
                                                      'repeat': repeat, 'model': model,
                                                      'load_duration_ns': warm.json().get('load_duration')})
                        print(f"[{case['id']} / {label} / {repeat}] 실제 그래프 실행", flush=True)
                        command = [sys.executable, str(Path(__file__).resolve()),
                                   '--worker-source', str(version_source), '--cases', str(args.cases.resolve()),
                                   '--case-id', case['id'], '--label', label, '--repeat-index', str(repeat),
                                   '--url', args.url, '--fast-model', args.fast_model,
                                   '--quality-model', args.quality_model]
                        env = {**os.environ, 'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false'}
                        raw = subprocess.check_output(command, env=env, cwd=version_source, timeout=1200)
                        result = json.loads(raw)
                        report['runs'].append(result)
                        save()
                        print(f"[{case['id']} / {label}] 완료: {result['elapsed_seconds']:.2f}초, "
                              f"{result['total_tokens']}토큰, 오류={bool(result['error'])}", flush=True)
            report['complete'] = True
            save()
    failures = sum(bool(run['error']) for run in report['runs'])
    print(f"비교 완료: {len(report['runs'])}회 실행, 오류 {failures}건. 결과: {output / 'ollama_comparison.md'}")


if __name__ == '__main__':
    main()
