"""独立诊断界面；由宿主提供约定的同源 API。"""

PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>任务诊断</title>
<style>
:root{color-scheme:light;--ink:#172437;--muted:#506075;--line:#d6dee6;--blue:#163c61;--paper:#fff;--bg:#f3f6f8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}button,select,textarea{font:inherit}button,select,textarea{border:1px solid #9eacba;border-radius:6px}button{cursor:pointer;min-height:44px}button:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #287ac0;outline-offset:2px}button:disabled,select:disabled,textarea:disabled{opacity:.55;cursor:not-allowed}.shell{max-width:1180px;margin:auto;padding:32px 24px 56px}.top{border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:24px;display:flex;justify-content:space-between;gap:20px;align-items:end}.eyebrow{font-size:.76rem;letter-spacing:.12em;font-weight:700;color:var(--blue)}h1{font-size:clamp(1.8rem,4vw,2.6rem);line-height:1.2;margin:8px 0}h2{font-size:1.12rem;margin:0 0 12px}p{margin:0}.top p,.hint{color:var(--muted);font-size:.9rem}.badge{white-space:nowrap;border:1px solid #a8c5bc;color:#245348;background:#e7f2ee;border-radius:20px;padding:5px 12px;font-size:.8rem;font-weight:700}.layout{display:grid;grid-template-columns:minmax(250px,320px) minmax(0,1fr);gap:20px;align-items:start}.panel{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:22px}.sidebar section+section{border-top:1px solid var(--line);margin-top:22px;padding-top:22px}label{display:block;font-weight:650;margin:14px 0 6px}select,textarea{width:100%;padding:10px;background:white;color:var(--ink)}select{min-height:46px}textarea{min-height:92px;resize:vertical}.hint{margin-top:8px}.status{min-height:1.5em;margin-top:10px;font-size:.9rem;color:var(--muted);overflow-wrap:anywhere}.status.error{color:#9e2525}.context{padding:12px;background:#f2f6f8;border-left:3px solid #7291a8;font-size:.88rem;overflow-wrap:anywhere}.context strong{display:block}.quick{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:14px 0 24px}.quick button{background:#f4f8fa;color:var(--blue);border-color:#bdcdd8;padding:10px;text-align:left;font-weight:650}.quick button:hover:not(:disabled){background:#e4eff5}.compose{border-top:1px solid var(--line);padding-top:18px}.actions{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-top:12px}.primary{background:var(--blue);color:#fff;border-color:var(--blue);padding:9px 20px;font-weight:700}.primary:hover:not(:disabled){background:#23547d}.conversation{margin-top:24px;border-top:1px solid var(--line);padding-top:20px}.messages{display:grid;gap:14px}.message{border:1px solid var(--line);border-radius:7px;padding:16px;overflow-wrap:anywhere}.message.user{background:#eef4f8;border-color:#cedfe8}.message h3{font-size:1rem;margin:0 0 8px}.message h4{margin:14px 0 4px;font-size:.95rem}.message ul{margin:4px 0;padding-left:22px}.message li{white-space:pre-wrap}.message .meta{color:var(--muted);font-size:.82rem}.notice{color:#754b16;background:#fff8e8;padding:10px;margin-top:12px;border-left:3px solid #be8a34}.empty{color:var(--muted);font-size:.92rem}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:700px){.shell{padding:20px 16px 40px}.top{align-items:start;flex-direction:column}.layout{grid-template-columns:1fr}.quick{grid-template-columns:1fr}.actions{align-items:stretch;flex-direction:column}.primary{width:100%}}
.message li{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
</head>
<body>
<main class="shell">
  <header class="top"><div><div class="eyebrow">DIAGNOSTIC / 任务诊断</div><h1>查看已有结果</h1><p>选择任务版本与候选，围绕已有记录提问。</p></div><span class="badge">只读 · 不修改任务</span></header>
  <div class="layout">
    <aside class="panel sidebar" aria-label="诊断上下文">
      <section><h2>01 选择任务版本</h2><p class="hint">每条任务代表一个明确的版本；请先选择，不会自动选中。</p><label for="task">任务 / 版本</label><select id="task" disabled><option value="">正在加载任务…</option></select><div id="taskStatus" class="status" role="status" aria-live="polite"></div></section>
      <section><h2>02 选择候选</h2><label for="candidate">候选标题（可选）</label><select id="candidate" disabled><option value="">请先选择任务</option></select><p class="hint">没有候选时也可以继续诊断任务。</p><div id="candidateStatus" class="status" role="status" aria-live="polite"></div></section>
      <section><h2>使用边界</h2><p class="hint">本页面只读，不自动联网或核查。快捷问题不运行模型；自主提问使用本机 Ollama，素材和问题只发送到本机模型服务，不保证回答正确。</p></section>
    </aside>
    <div class="panel">
      <h2>03 提出问题</h2><div class="context" id="context"><strong>尚未选择任务</strong>请选择左侧任务版本。</div>
      <p class="hint">快捷问题直接查询；也可以选择场景并输入自己的筛选问题。</p>
      <div class="quick" aria-label="快捷问题">
        <button type="button" data-scenario="understanding" data-question="任务是如何理解视频内容的？" disabled>内容理解<br><small>任务是如何理解视频的？</small></button>
        <button type="button" data-scenario="review" data-question="这条候选有哪些审核信息？" disabled>候选审核<br><small>有哪些审核信息？</small></button>
        <button type="button" data-scenario="evidence" data-question="有哪些已有依据和证据？" disabled>依据与证据<br><small>有哪些已有依据？</small></button>
      </div>
      <form class="compose" id="form"><label for="scenario">查询场景</label><select id="scenario" disabled><option value="understanding">内容理解</option><option value="review">候选审核</option><option value="evidence">依据与证据</option></select><label for="question">自然语言问题</label><textarea id="question" maxlength="2000" placeholder="例如：这条标题为什么被拦？" disabled></textarea><div class="actions"><span class="hint">提交即同意本机模型读取当前任务记录、问题和最近六条对话；不上传云端。</span><button class="primary" id="submit" type="submit" disabled>使用本机模型提问</button></div></form>
      <div id="queryStatus" class="status" role="status" aria-live="polite"></div>
      <section class="conversation" aria-labelledby="conversationTitle"><h2 id="conversationTitle">对话记录</h2><div id="messages" class="messages" aria-live="polite" aria-relevant="additions"><p class="empty">选择任务后，查询结果将显示在这里。</p></div></section>
    </div>
  </div>
</main>
<script>
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const task = $('task'), candidate = $('candidate'), scenario = $('scenario'), question = $('question');
  const quick = Array.from(document.querySelectorAll('[data-scenario]'));
  let current = null, generation = 0, pending = false, history = [], controller = null;
  const names = {understanding:'内容理解',review:'候选审核',evidence:'依据与证据'};
  function text(value, fallback = '—') { return value == null || value === '' ? fallback : String(value); }
  function status(id, message, error = false) { const el = $(id); el.textContent = message; el.classList.toggle('error', error); }
  function option(value, label) { const el = document.createElement('option'); el.value = value; el.textContent = label; return el; }
  function controls() { const disabled = !current || pending; quick.forEach(el => { el.disabled = disabled; }); scenario.disabled = disabled; question.disabled = disabled; $('submit').disabled = disabled; }
  function resetConversation() { history = []; $('messages').replaceChildren(); const p = document.createElement('p'); p.className = 'empty'; p.textContent = '查询结果将显示在这里。'; $('messages').append(p); question.value = ''; scenario.value = 'understanding'; status('queryStatus', ''); }
  function showContext() { const el = $('context'); el.replaceChildren(); const title = document.createElement('strong'); title.textContent = current ? text(current.title, '未命名任务') : '尚未选择任务'; el.append(title, document.createTextNode(current ? `任务 ${text(current.task_id)} · 视频 ${text(current.video_id)} · 候选 ${candidate.selectedOptions[0] ? candidate.selectedOptions[0].textContent : '未指定'}` : '请选择左侧任务版本。')); }
  async function request(url, options) { const response = await fetch(url, options); let data; try { data = await response.json(); } catch (_) { throw new Error('服务器返回的内容不是有效 JSON'); } if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`); return data; }
  async function loadTasks() {
    status('taskStatus', '正在加载任务…');
    try { const data = await request('/api/tasks'); if (!data || !Array.isArray(data.tasks)) throw new Error('任务列表格式不正确'); task.replaceChildren(option('', data.tasks.length ? '请选择任务版本' : '暂无任务')); data.tasks.forEach(item => { if (item && item.task_id != null && String(item.task_id) !== '') task.append(option(String(item.task_id), `${text(item.title, '未命名')} · 任务 ${text(item.task_id)} · 视频 ${text(item.video_id)} · ${text(item.status)} · ${text(item.created_at)}`)); }); task.disabled = false; status('taskStatus', data.tasks.length ? `共 ${data.tasks.length} 条任务，请选择版本。` : '暂无任务，请稍后重试。'); }
    catch (err) { task.replaceChildren(option('', '任务加载失败')); status('taskStatus', `${err.message}；请刷新页面重试。`, true); }
  }
  task.addEventListener('change', async () => {
    const selected = task.value, version = ++generation; if (controller) controller.abort(); controller = null; current = null; pending = false; controls(); resetConversation(); showContext(); candidate.disabled = true; candidate.replaceChildren(option('', selected ? '正在加载候选…' : '请先选择任务')); status('candidateStatus', '');
    if (!selected) return;
    status('candidateStatus', '正在加载任务详情…');
    try { const data = await request('/api/task?task_id=' + encodeURIComponent(selected)); if (version !== generation) return; if (!data || String(data.task_id) !== selected || !Array.isArray(data.candidates)) throw new Error('任务详情格式不正确或任务不匹配'); current = data; candidate.replaceChildren(option('', '不指定候选')); data.candidates.forEach(item => { if (item && item.candidate_id != null && String(item.candidate_id) !== '') candidate.append(option(String(item.candidate_id), `${text(item.title, '未命名候选')} · ${text(item.candidate_id)}`)); }); candidate.disabled = false; status('candidateStatus', data.candidates.length ? `共 ${data.candidates.length} 个候选，可选择或不指定。` : '该任务没有候选，可直接查询任务。'); showContext(); controls(); }
    catch (err) { if (version !== generation) return; candidate.replaceChildren(option('', '详情加载失败')); status('candidateStatus', `${err.message}；请重新选择任务重试。`, true); }
  });
  candidate.addEventListener('change', () => { if (controller) controller.abort(); controller = null; generation++; pending = false; resetConversation(); showContext(); controls(); });
  scenario.addEventListener('change', () => { history = []; status('queryStatus', '已切换场景，后续提问不携带上一个场景的对话。'); });
  function node(tag, content, cls) { const el = document.createElement(tag); if (cls) el.className = cls; el.textContent = text(content, ''); return el; }
  function renderResult(data, asked, selectedScenario, selectedCandidate) {
    const messages = $('messages'); const empty = messages.querySelector('.empty'); if (empty) empty.remove();
    const user = node('article', '', 'message user'); user.append(node('h3', '你的问题'), node('p', asked), node('p', `${names[selectedScenario]} · 候选 ${selectedCandidate || '未指定'}`, 'meta')); messages.append(user);
    const answer = node('article', '', 'message'); answer.append(node('h3', text(data.title, '诊断结果')), node('p', `任务 ${text(data.task_id)} · 视频 ${text(data.video_id)} · 场景 ${names[data.scenario] || text(data.scenario)} · 候选 ${text(data.candidate_id, '未指定')}`, 'meta'));
    if (data.chat) { answer.append(node('p', data.chat.answer)); answer.append(node('p', `模型引用：${(data.chat.citations || []).join('、') || '无'}。${text(data.chat.notice, '')}`, 'notice')); if (Array.isArray(data.chat.evidence)) { const refs = document.createElement('details'); refs.append(node('summary', '查看本次模型引用的素材（未验证语义支持）')); data.chat.evidence.forEach(ref => refs.append(node('p', ref.kind === 'review_issue' ? `${text(ref.id)} · 评审问题记录${ref.sent ? '' : '（未随本次请求发送）'}：${text(ref.issue_code, '未标识')} · ${text(ref.reason, '原因未记录')}` : `${text(ref.id)} · ${text(ref.field, '来源未记录')} · ${text(ref.locator, '位置未记录')} · ${text(ref.quality, '质量未记录')}${ref.sent === false ? ' · 该条未随本次请求发送，仅来自诊断记录' : ''}\n${text(ref.text, '原文未记录')}`))); answer.append(refs); } }
    else if (data.summary && Array.isArray(data.summary.paragraphs)) data.summary.paragraphs.forEach(line => answer.append(node('p', line)));
    if (!data.chat && data.summary && Array.isArray(data.summary.evidence) && data.summary.evidence.length) { const refs = document.createElement('details'); refs.append(node('summary', `查看关联依据（${data.summary.evidence.length}）`)); data.summary.evidence.forEach(ref => refs.append(node('p', `${text(ref.evidence_id)} · ${ref.matched ? text(ref.field, '来源未记录') + ' · ' + text(ref.locator, '位置未记录') + ' · ' + text(ref.quality, '质量未记录') + '\n' + text(ref.text, '原文未记录') : '引用未找到'}`))); answer.append(refs); }
    const sections = Array.isArray(data.sections) ? data.sections : []; if (!sections.length) answer.append(node('p', '没有可展示的内容。', 'empty'));
    const details = document.createElement('details'); details.append(node('summary', '查看原始记录与证据'));
    sections.forEach(section => { if (!section) return; details.append(node('h4', text(section.heading, '未命名部分'))); const list = document.createElement('ul'); (Array.isArray(section.items) ? section.items : []).forEach(item => list.append(node('li', item))); details.append(list); }); answer.append(details);
    (Array.isArray(data.notices) ? data.notices : []).forEach(notice => answer.append(node('p', notice, 'notice'))); messages.append(answer);
  }
  async function diagnose(kind, asked) {
    if (!current || pending) return; const version = generation, taskId = String(current.task_id), selectedCandidate = candidate.value; pending = true; controls(); status('queryStatus', '正在查询已有信息…');
    if (controller) controller.abort(); controller = new AbortController();
    try { const data = await request(asked ? '/api/chat' : '/api/diagnose', {method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({task_id:taskId,scenario:kind,candidate_id:selectedCandidate || null,question:asked,confirm_local_model:!!asked,history:history.slice(-6)})}); if (version !== generation) return; if (!data || String(data.task_id) !== taskId || data.scenario !== kind || !Array.isArray(data.sections) || !Array.isArray(data.notices)) throw new Error('诊断结果格式不正确或上下文不匹配'); renderResult(data, asked || names[kind] + '：查看已有记录', kind, selectedCandidate); if (asked && data.chat) { history.push({role:'user',content:asked}, {role:'assistant',content:data.chat.answer}); history = history.slice(-6); } status('queryStatus', '查询完成。'); }
    catch (err) { if (err && err.name === 'AbortError') return; if (version === generation) status('queryStatus', `${err.message}；请重试。`, true); }
    finally { if (version === generation) { pending = false; controls(); } }
  }
  quick.forEach(button => button.addEventListener('click', () => diagnose(button.dataset.scenario, '')));
  $('form').addEventListener('submit', event => { event.preventDefault(); const asked = question.value.trim(); if (!asked) { status('queryStatus', '请输入问题后再查询。', true); question.focus(); return; } diagnose(scenario.value, asked); });
  controls(); loadTasks();
})();
</script>
</body>
</html>'''
