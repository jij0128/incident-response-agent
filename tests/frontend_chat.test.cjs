// 실제 인라인 채팅 로직을 가짜 Vue 반응형 래퍼와 EventSource로 실행한다.
// 레이아웃 검사는 브라우저에서 별도로 수행한다. 외부 서버는 호출하지 않는다.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'web_chat.html'), 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(text => text.includes('createApp({setup()'));

function harness() {
  let app, id=0;
  const sources=[], copied=[];
  class EventSource {
    constructor(url) { this.url=url; sources.push(this); }
    close() { this.closed=true; }
    emit(event) { this.onmessage({data:typeof event==='string'?event:JSON.stringify(event)}); }
    fail() { this.onerror(); }
  }
  vm.runInNewContext(script, {
    Vue:{ref:value=>({value}),reactive:value=>value,computed:fn=>({get value(){return fn();}}),
      nextTick:()=>Promise.resolve(),onBeforeUnmount:()=>{},
      createApp:options=>({mount:()=>{app=options.setup();}})},
    EventSource, crypto:{randomUUID:()=>String(++id)},
    fetch:async()=>({ok:true,json:async()=>({llm_backend:'fake'})}),
    requestAnimationFrame:()=>1,cancelAnimationFrame:()=>{},
    setTimeout:()=>1,clearTimeout:()=>{},
    navigator:{clipboard:{writeText:async text=>copied.push(text)}},
  });
  function submit(query='payments-service CPU 급증 원인 확인') {
    app.input.value=query;app.onSubmit();return sources.at(-1);
  }
  return {app,sources,copied,submit};
}

test('전송 중 Enter/submit 중복 요청을 차단한다',()=>{
  const {app,sources,submit}=harness();submit();submit('중복 요청');
  assert.equal(sources.length,1);assert.equal(app.turns.value.length,1);
  assert.equal(app.input.value,'중복 요청');
});

test('한글 조합 Enter와 Shift+Enter는 전송하지 않는다',()=>{
  const {app,sources}=harness();app.input.value='진단해주세요';
  const event={key:'Enter',shiftKey:false,isComposing:false,keyCode:13,preventDefault(){}};
  app.onInputKey({...event,isComposing:true});
  app.onInputKey({...event,keyCode:229});
  app.onInputKey({...event,shiftKey:true});
  assert.equal(sources.length,0);app.onInputKey(event);assert.equal(sources.length,1);
});

test('재작성 초안과 최종 답변을 구분하며 DONE까지 전송을 잠근다',()=>{
  const {app,submit}=harness();const source=submit();const turn=app.turns.value[0];
  source.emit({type:'AnswerToken',attempt:1,delta:'잘못된 초안'});
  source.emit({type:'Validation',attempt:1,verdict:'불합격'});
  source.emit({type:'AnswerToken',attempt:2,delta:'수정된 '});
  source.emit({type:'AnswerToken',attempt:2,delta:'초안'});
  source.emit({type:'Validation',attempt:2,verdict:'합격'});
  source.emit({type:'FinalAnswer',reply:'서버가 확정한 최종 답변'});
  assert.equal(turn.finalText,'서버가 확정한 최종 답변');
  assert.equal(app.currentDraft(turn),'수정된 초안');
  assert.equal(app.rejectedDrafts(turn).length,1);assert.equal(app.sending.value,true);
  source.emit('[DONE]');assert.equal(app.sending.value,false);assert.equal(source.closed,true);
  assert.equal(turn.phase,'completed');
});

test('검수 실패 fallback과 미등록 확인 안내를 정상 완료와 구분한다',()=>{
  const first=harness(), fallback=first.submit();
  fallback.emit({type:'Validation',attempt:3,verdict:'실패',fallback:true});
  fallback.emit({type:'FinalAnswer',reply:'관측 상태만 안내합니다.'});fallback.emit('[DONE]');
  assert.equal(first.app.turns.value[0].phase,'limited');
  const second=harness(), unknown=second.submit('billing-service 진단');
  unknown.emit({type:'TargetClarification',unknown_systems:['billing-service']});
  unknown.emit({type:'FinalAnswer',reply:'시스템명을 확인해주세요.'});unknown.emit('[DONE]');
  assert.equal(second.app.turns.value[0].phase,'clarification');
  assert.equal(second.app.turns.value[0].attempts.length,0);
});

test('최종 답변 없는 DONE은 부분 초안을 완료 처리하지 않는다',()=>{
  const {app,submit}=harness();const source=submit();
  source.emit({type:'AnswerToken',attempt:1,delta:'수신한 일부 초안'});source.emit('[DONE]');
  const turn=app.turns.value[0];assert.equal(turn.phase,'error');assert.equal(turn.finalReceived,false);
  assert.equal(app.currentDraft(turn),'수신한 일부 초안');assert.match(turn.error,/최종 답변/);
  assert.equal(app.sending.value,false);
});

test('연결 실패 후 늦은 이전 이벤트가 다음 요청을 덮어쓰지 않는다',()=>{
  const {app,submit}=harness();const old=submit();old.fail();
  assert.equal(app.turns.value[0].phase,'error');assert.equal(old.closed,true);
  const current=submit('다음 질문');old.emit({type:'FinalAnswer',reply:'뒤늦은 응답'});old.emit('[DONE]');
  assert.equal(app.sending.value,true);assert.equal(app.turns.value[1].finalReceived,false);
  current.emit({type:'FinalAnswer',reply:'다음 질문의 답변'});current.emit('[DONE]');
  assert.equal(app.turns.value[1].finalText,'다음 질문의 답변');
});

test('최종 답변 직후 연결 오류도 전송 완료로 숨기지 않는다',()=>{
  const {app,submit}=harness();const source=submit();source.emit({type:'FinalAnswer',reply:'받은 답변'});source.fail();
  const turn=app.turns.value[0];assert.equal(turn.phase,'interrupted');
  assert.equal(turn.finalText,'받은 답변');assert.match(turn.error,/전송 완료/);
});

test('예시는 자동 전송하지 않고 새 대화는 새 세션 ID를 사용한다',async()=>{
  const {app,sources,submit,copied}=harness();await app.useExample('예시 질문');
  assert.equal(app.input.value,'예시 질문');assert.equal(sources.length,0);
  const first=submit();first.emit({type:'FinalAnswer',reply:'첫 답변'});first.emit('[DONE]');
  await app.copyAnswer(app.turns.value[0]);assert.equal(copied[0],'첫 답변');
  app.newConversation();assert.equal(app.turns.value.length,0);
  const second=submit();assert.notEqual(new URL(first.url,'http://local').searchParams.get('thread_id'),
    new URL(second.url,'http://local').searchParams.get('thread_id'));
});
