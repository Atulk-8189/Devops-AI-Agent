/** Browser regression test. Requires an isolated Chrome CDP port and an open
 * existing conversation with Execution Trace. Never submits an investigation.
 * Run: node frontend/tests/sidebar_browser.mjs [CDP port, default 9223]
 */
import assert from 'node:assert/strict';
const port = process.argv[2] || '9223';
const pages = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = pages.find(p => p.type === 'page' && p.url.startsWith('http://127.0.0.1:8000'));
assert.ok(page, 'Open an existing local conversation in the isolated browser first');
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, {once: true}));
let sequence = 0;
const requests = new Map();
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  const request = requests.get(message.id);
  if (request) {
    requests.delete(message.id);
    message.error ? request.reject(message.error) : request.resolve(message.result);
  }
});
const call = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  requests.set(id, {resolve, reject});
  socket.send(JSON.stringify({id, method, params}));
});
const evaluate = async expression => {
  const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
};
const settle = () => new Promise(resolve => setTimeout(resolve, 150));
const measure = () => evaluate(`(() => {
  const rect = selector => document.querySelector(selector)?.getBoundingClientRect().toJSON();
  return {panel:rect('[data-devops-trace-pane]'), chat:rect('[data-devops-chat-pane]'),
    group:rect('[data-devops-trace-layout]'), splitter:rect('#devops-trace-splitter'),
    rail:rect('#devops-sidebar-rail'), composer:rect('#message-composer'),
    saved:localStorage.getItem('devops_panel_width'),
    dragging:document.body.classList.contains('devops-trace-dragging'),
    tabs:[...document.querySelectorAll('#devops-execution-trace-panel button')].map(e=>e.textContent.trim())};
})()`);
const click = async (selector) => {
  const rect = await evaluate(`document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect().toJSON()`);
  const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
  await call('Input.dispatchMouseEvent', {type:'mousePressed', x,y,button:'left',clickCount:1});
  await call('Input.dispatchMouseEvent', {type:'mouseReleased',x,y,button:'left',clickCount:1});
  await settle();
};
const drag = async delta => {
  const {splitter} = await measure();
  const x = splitter.x + splitter.width / 2, y = splitter.y + splitter.height / 2;
  await call('Input.dispatchMouseEvent', {type:'mouseMoved',x,y});
  await call('Input.dispatchMouseEvent', {type:'mousePressed',x,y,button:'left',clickCount:1});
  for (let step=1; step<=10; step++) {
    await call('Input.dispatchMouseEvent', {type:'mouseMoved',x:x+delta*step/10,y,buttons:1});
  }
  await call('Input.dispatchMouseEvent', {type:'mouseReleased',x:x+delta,y,button:'left',clickCount:1});
  await settle();
  return measure();
};
const closeTo = (actual, expected) => assert.ok(Math.abs(actual-expected)<2, `${actual} != ${expected}`);
function noOverlap(state) {
  assert.ok(state.chat.right <= state.panel.x+1, 'Chat overlaps trace');
  assert.ok(state.chat.width >= 359, 'Insufficient chat width');
  if (state.composer) assert.ok(state.composer.right <= state.chat.right+1, 'Composer overlaps trace');
  assert.equal(state.dragging,false,'Pointer capture not released');
}
try {
  const initial = await measure();
  assert.ok(initial.splitter, 'Splitter must be mounted outside the trace content');
  closeTo(initial.panel.width,288);
  let state = await drag(-220);
  closeTo(state.panel.width,508); noOverlap(state);
  state = await drag(160);
  closeTo(state.panel.width,348); noOverlap(state);
  await click('[aria-label="Collapse Investigation Details"]');
  state = await measure();
  closeTo(state.panel.width,0); closeTo(state.splitter.width,0);
  assert.ok(state.rail.width>0); closeTo(state.chat.right,state.rail.x);
  await click('#devops-sidebar-rail');
  state = await measure(); closeTo(state.panel.width,348); noOverlap(state);
  state = await drag(700); closeTo(state.panel.width,240); noOverlap(state);
  state = await drag(-900); closeTo(state.panel.width,800); noOverlap(state);
  await call('Emulation.setDeviceMetricsOverride',{width:1024,height:900,deviceScaleFactor:1,mobile:false});
  await settle(); state = await measure(); noOverlap(state);
  assert.ok(state.panel.width<800, 'Width must clamp to leave room for chat');
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await settle(); state = await measure(); closeTo(state.panel.width,800);
  await click('#devops-trace-splitter');
  await call('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowRight',code:'ArrowRight'});
  await call('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowRight',code:'ArrowRight'});
  await settle(); state = await measure(); closeTo(state.panel.width,776);
  assert.deepEqual(state.tabs,initial.tabs,'Trace tabs and controls must survive resizing');
  // Leave the isolated browser at the original width.
  state = await drag(488); closeTo(state.panel.width,288);
  console.log('PASS: actual browser drag wider/narrower, 240/800px bounds, complete collapse, reopen at previous width, responsive chat/composer, viewport clamp/restore, keyboard resizing, preserved trace controls.');
} finally { socket.close(); }
