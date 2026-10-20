"""실제 OpenAI·MCP와 FastAPI SSE 경로를 세 질문으로 확인한다.

유료 API를 호출한다. --run-paid-check가 있어야 실행하며 자동 재실행하지 않는다.
기존 서버와 session.db는 건드리지 않고 임시 DB와 ASGI 테스트 클라이언트를 쓴다.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-paid-check', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'eval/results/openai_check.json')
    args = parser.parse_args()
    if not args.run_paid_check:
        parser.error('실제 과금 호출입니다. 실행하려면 --run-paid-check를 지정하세요.')
    os.environ.update({'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false',
                       'LLM_BACKEND': 'openai', 'SUPERVISOR_VERSION': 'v2'})
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key or '...' in key:
        raise SystemExit('유효한 OPENAI_API_KEY 설정이 필요합니다.')
    from fastapi.testclient import TestClient
    import httpx
    from agent import openai_llm
    from agent.session_store import SqliteSessionStore
    import server

    models = {'fast': os.environ.get('OPENAI_FAST_MODEL', 'gpt-4o-mini'),
              'quality': os.environ.get('OPENAI_QUALITY_MODEL', 'gpt-4o')}
    calls = []
    original_post = httpx.post

    def bounded_post(url, **kwargs):
        if url != openai_llm.OPENAI_URL:
            raise RuntimeError('검증에서 허용하지 않은 POST 대상입니다.')
        if len(calls) >= 6:
            raise RuntimeError('검증용 유료 호출 한도 6회에 도달했습니다.')
        payload = dict(kwargs['json'])
        payload['max_completion_tokens'] = 1100 if payload['model'] == models['quality'] else 700
        if len(json.dumps(payload, ensure_ascii=False)) > 30000:
            raise RuntimeError('검증용 입력 크기 한도를 초과했습니다.')
        kwargs['json'] = payload
        entry = {'model': payload['model'], 'max_completion_tokens': payload['max_completion_tokens']}
        calls.append(entry)
        started = time.perf_counter()
        print(f"OpenAI 호출 {len(calls)}: {payload['model']}", flush=True)
        try:
            response = original_post(url, **kwargs)
            entry.update({'elapsed_seconds': round(time.perf_counter()-started, 4),
                          'http_status': response.status_code})
            if response.is_success:
                body = response.json()
                entry.update({'usage': body.get('usage', {}), 'actual_model': body.get('model'),
                              'finish_reason': body['choices'][0].get('finish_reason')})
            else:
                # 에러 메시지에는 API 키가 들어갈 수 있어 상태·코드·유형만 저장한다.
                error = response.json().get('error', {})
                entry['api_error'] = {k: error.get(k) for k in ('code', 'type')}
            return response
        except httpx.RequestError as exc:
            entry['transport_error'] = type(exc).__name__
            raise

    fixture = json.loads((ROOT / 'eval/ollama_cases.json').read_text())
    report = {'experiment': 'real_openai_mcp_fastapi_sse',
              'started_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'),
              'commit': subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),
              'models': models, 'tracing_enabled': False,
              'conditions': ['실제 OpenAI와 MCP stdio 서버', 'FastAPI ASGI SSE 경로, 브라우저/프록시 미포함',
                             '출력 상한: fast 700 / quality 1100 토큰', '총 유료 호출 상한 6회',
                             '실제 MCP 모의 데이터의 무작위 변동과 최초 재시도 포함',
                             '임시 SQLite DB, 기존 서버와 DB 유지'],
              'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
                                for p in ('agent/openai_llm.py','agent/graph.py','agent/grounding.py','server.py','eval/check_openai.py')},
              'cases': [], 'api_calls': calls, 'complete': False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    with tempfile.TemporaryDirectory(prefix='incident-openai-check-') as temporary:
        db_path = Path(temporary)/'session.db'
        with patch.object(server, 'SqliteSessionStore', lambda: SqliteSessionStore(db_path)), \
             patch.object(openai_llm.httpx, 'post', bounded_post), \
             TestClient(server.app, raise_server_exceptions=True) as client:
            for case in fixture['cases']:
                started, before = time.perf_counter(), len(calls)
                result = {'id':case['id'], 'query':case['query'], 'events':[], 'error':None,
                          'final':None,'done':False}
                try:
                    response = client.get('/chat/stream', params={'message':case['query'],
                                                                 'thread_id':'openai-check-'+case['id']})
                    response.raise_for_status()
                    for line in response.text.splitlines():
                        if not line.startswith('data: '):
                            continue
                        data = line[6:]
                        if data == '[DONE]':
                            result['done'] = True
                            continue
                        event = json.loads(data)
                        result['events'].append(event)
                        if event.get('type') == 'FinalAnswer':
                            result['final'] = event.get('reply')
                    expected = 'TargetClarification' if case['unregistered'] else 'Validation'
                    result['passed'] = bool(result['done'] and result['final'] and
                                            any(e.get('type')==expected for e in result['events']))
                    if case['unregistered']:
                        result['passed'] = result['passed'] and len(calls)==before
                    else:
                        result['passed'] = result['passed'] and any(
                            e.get('type')=='Validation' and e.get('verdict')=='합격' for e in result['events'])
                except Exception as exc:
                    # 키나 서버 스택은 결과물에 포함하지 않는다.
                    result['error'] = type(exc).__name__
                    result['passed'] = False
                result.update({'elapsed_seconds':round(time.perf_counter()-started,4),
                               'api_call_count':len(calls)-before})
                report['cases'].append(result)
                save()
                print(f"{case['id']}: {'통과' if result['passed'] else '실패'}, 유료 호출 {result['api_call_count']}회",flush=True)
    report['complete'] = True
    report['all_passed'] = all(case['passed'] for case in report['cases'])
    report['total_tokens_received'] = sum(call.get('usage',{}).get('total_tokens',0) for call in calls)
    save()
    print(f"결과 저장: {args.output}")
    if not report['all_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
