from __future__ import annotations

import ipaddress
import json
import threading
from pathlib import Path
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import RunConfig, resolve_case_bank
from .data import Dataset
from .goldsheet import read_criteria, record as record_gold, remove as remove_gold
from .pipeline import Pipeline
from .storage import Storage
from .types import HumanDecision


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>标题任务调试台</title><style>
:root{font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#20252b;background:#f4f6f8}*{box-sizing:border-box}body{margin:0}header{background:#fff;border-bottom:1px solid #dce1e6;padding:14px 20px;display:flex;gap:14px;align-items:center;position:sticky;top:0;z-index:2}h1{font-size:18px;margin:0}header .sub{color:#65717c}.layout{display:grid;grid-template-columns:260px minmax(0,1fr);min-height:calc(100vh - 58px)}aside{background:#fff;border-right:1px solid #dce1e6;padding:14px;overflow:auto}.main{padding:18px;max-width:1400px;width:100%;margin:0 auto}.newtask{border-bottom:1px solid #e4e8ec;padding-bottom:14px;margin-bottom:14px}input,textarea,button{font:inherit}input,textarea{border:1px solid #cbd3da;border-radius:6px;padding:8px;background:#fff;width:100%}button{border:1px solid #c6cdd4;background:#fff;border-radius:6px;padding:7px 10px;cursor:pointer;color:#20252b}button:hover{background:#f2f5f7}button.primary{background:#245ca6;color:#fff;border-color:#245ca6}button.danger{color:#a13030}.task{display:block;width:100%;text-align:left;margin:5px 0;padding:9px;border:1px solid transparent}.task.active{background:#eef4fb;border-color:#c6d9ed}.muted{color:#697681}.small{font-size:12px}.card{background:#fff;border:1px solid #dce1e6;border-radius:8px;padding:14px;margin:0 0 12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.spread{justify-content:space-between}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.badge{display:inline-block;padding:2px 8px;border-radius:99px;background:#edf0f2;font-size:12px}.badge.ok{background:#e3f4e9;color:#18633a}.badge.warn{background:#fff1d3;color:#755000}.badge.bad{background:#fae4e4;color:#962c2c}h2{font-size:16px;margin:0 0 10px}h3{font-size:14px;margin:10px 0 5px}.headline{font-size:18px;font-weight:650;margin:8px 0}.issue{border-left:3px solid #d39b22;background:#fff9eb;padding:8px;margin:6px 0}.timeline{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:7px}.step{border:1px solid #d9dfe4;border-radius:6px;padding:8px;background:#fafbfc}.step.done{border-color:#a9d5b7;background:#f0f8f2}.step.fail{border-color:#e1a9a9;background:#fff3f3}.step.current{border-color:#98b9df;background:#f1f6fc}details{border-top:1px solid #e7eaed;padding:8px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f8;border:1px solid #e2e6e9;padding:10px;border-radius:6px;max-height:440px;overflow:auto;font-size:12px}.evidence{padding:7px 0;border-top:1px solid #edf0f2}.reviewform{display:flex;gap:8px;align-items:center;margin-top:10px}.reviewform details{flex:1}.wide{grid-column:1/-1}.review-first{border:2px solid #a9c4e4}.review-first h2{font-size:18px}.review-hint{background:#eef4fb;padding:9px;border-radius:6px;margin-bottom:10px}.reject-dialog{border:1px solid #cbd3da;border-radius:10px;width:min(560px,calc(100vw - 24px));padding:18px;box-shadow:0 12px 40px #17212b33}.reject-dialog::backdrop{background:#17212b66}.reject-dialog label{display:block;font-weight:600;margin:10px 0 4px}.reject-dialog textarea{min-height:76px}.conditional{display:none}.conditional.show{display:block}.notice{padding:10px;background:#fff5dc;border:1px solid #eed9a2;border-radius:6px}.empty{padding:50px;text-align:center;color:#697681}@media(max-width:800px){.layout{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #dce1e6;max-height:280px}.grid{grid-template-columns:1fr}.main{padding:10px}}

.task-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin:2px 0 14px}.task-heading h2{font-size:19px;line-height:1.4;margin:0;max-width:900px}.task-meta{border:0;padding:3px 0 0;color:#697681}.task-meta summary{font-size:12px;font-weight:500}.task-meta div{padding-top:4px}.review-first{border:0;padding:0;background:transparent}.review-first h2{font-size:17px;margin:0}.review-heading{display:flex;align-items:center;gap:10px;margin:0 0 8px}.mock-tag{font-size:12px;color:#775400;background:#fff3d4;border-radius:4px;padding:2px 7px}.risk-note{border-left:3px solid #c0722a;background:#fff7ef;padding:10px 12px;border-radius:6px;margin:0 0 10px;font-size:13px}.candidate{background:#fff;border:1px solid #dce1e6;border-radius:8px;padding:15px 16px;margin:0 0 8px}.candidate-top{display:flex;align-items:center;justify-content:space-between}.candidate-id{font-size:12px;color:#697681}.candidate .headline{font-size:18px;line-height:1.45;margin:8px 0 10px}.candidate-bottom{display:flex;align-items:center;justify-content:space-between;gap:12px}.candidate-bottom>.small{white-space:nowrap}.reviewform{margin:0;gap:7px}.reviewform button{min-width:76px}.reviewform details{position:relative;flex:initial;border:0;padding:0 0 0 4px}.reviewform details[open]{position:absolute;z-index:1;background:#fff;padding:12px;border:1px solid #dce1e6;border-radius:8px;box-shadow:0 8px 24px #17212b18;min-width:280px}.reviewform details summary{font-size:12px;color:#526579}.decision-result{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:13px}.decision-mark{border-radius:4px;padding:3px 7px;font-size:12px}.decision-mark.approved{background:#e3f4e9;color:#18633a}.decision-mark.rejected{background:#fae4e4;color:#962c2c}.candidate-detail{margin-top:10px;padding-top:7px}.candidate-detail summary{font-size:12px;color:#526579}.candidate-detail pre{max-height:240px}.system-only,.candidate-detail,.timeline,.task-meta{display:none!important}.candidate-bottom>.small{display:none!important}.task-group{margin-bottom:10px}.task{padding:9px 8px;margin:3px 0}.task b{font-size:13px;line-height:1.45;display:inline-block}.task .task-status{margin-top:5px}.older{padding-left:16px}.newtask p{margin:6px 0 0}.newtask h2{margin-bottom:9px}.main>.card{margin-top:10px}.flow-card{padding:10px 14px}.flow-card>details>summary{font-size:13px;color:#526579}.flow-card .timeline{margin-top:10px}
</style></head><body>
<header><h1>标题任务调试台</h1><span class="sub">单条内容评审</span><span id="connection" class="badge">连接中</span></header>
<div class="layout"><aside><div class="newtask"><h2>启动单条任务</h2><input id="videoSearch" list="videos" placeholder="输入视频 ID 或标题"><datalist id="videos"></datalist><button id="run" class="primary" style="width:100%;margin-top:7px">生成并评审</button><p class="muted small">每次只启动一条。当前模型：<span id="provider">加载中</span></p></div><div class="row spread"><h2>最近任务</h2><button id="refresh">刷新</button></div><div id="taskList"></div></aside>
<main class="main" id="content"><div class="empty">选择一条任务，查看它如何从素材走到评审。</div></main></div>
<script>
const $=id=>document.getElementById(id);let tasks=[],videos=[],criteriaList=[],active=null,poll=null,expandedVideo=null,rejectTarget=null,marginalTarget=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let maxChars=20;const showLen=(id,text)=>{const el=$(id);if(!el)return;const n=[...String(text)].length;el.textContent=`${n}/${maxChars} 字`;el.className='small '+(n>maxChars?'badge bad':'muted')};
const pretty=x=>esc(JSON.stringify(x,null,2));
const statusName={prepared:'已创建',generating:'生成中',reviewing:'评审中',rewriting:'重写中',pending_human:'等待人工判断',approved:'人工通过',approved_marginal:'勉强通过',stopped:'已停止',technical_failed:'技术失败',rejected:'人工驳回'};
const nodeName={N00:'输入',N03:'理解',N04:'规则匹配',N05:'事实边界',N07:'看点',N09:'标题生成',R01:'独立评审',Q01:'质量路由',N12:'人工交接',X01:'技术失败',H01:'人工判断'};
function badge(value){let tone=['approved','ready_for_human_review','pass','usable','已采用','通过','可用'].includes(value)?'ok':['technical_failed','reject','rejected','excluded','fail','已驳回','驳回'].includes(value)?'bad':['needs_review','pending_human','limited','rewrite','待评审','需复核'].includes(value)?'warn':' ';return `<span class="badge ${tone}">${esc(value||'—')}</span>`}
async function api(url,options={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...options});const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
function renderList(){const groups=new Map();for(const t of tasks){if(!groups.has(t.video_id))groups.set(t.video_id,[]);groups.get(t.video_id).push(t)}const __rank={unreviewed:0,in_progress:1,done:2,no_candidates:3};const __name={unreviewed:'还未评',in_progress:'评审中',done:'已评完',no_candidates:'无候选'};const __groups=[...groups.entries()].sort((a,b)=>(__rank[a[1][0].review_status]??9)-(__rank[b[1][0].review_status]??9));$('taskList').innerHTML=__groups.map(([videoId,items])=>{const latest=items[0],open=expandedVideo===videoId||items.some(x=>x.task_id===active&&x.task_id!==latest.task_id);const __badge=latest.review_status?'<span class="badge '+(latest.review_status==='done'?'good':latest.review_status==='in_progress'?'warn':'')+'">'+(__name[latest.review_status]||'')+(latest.review_status==='in_progress'?' '+latest.review_done+'/'+latest.review_total:'')+'</span>':'';return `<div class="task-group"><button class="task ${active===latest.task_id?'active':''}" data-id="${esc(latest.task_id)}"><b>${esc(latest.title||videoId)}</b><br><span class="small muted task-status">${esc(videoId)} · ${esc(statusName[latest.status]||latest.status)} ${__badge}</span></button>${items.length>1?`<button class="small history-toggle" data-video="${esc(videoId)}">${open?'收起':'查看'}此前 ${items.length-1} 次运行</button>`:''}${open?items.slice(1).map(t=>`<button class="task older ${active===t.task_id?'active':''}" data-id="${esc(t.task_id)}"><b title="${esc(t.task_id)}">${esc(statusName[t.status]||t.status)}</b><br><span class="small muted">${esc(new Date(t.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}))}</span></button>`).join(''):''}</div>`}).join('')||'<p class="muted">暂无任务</p>';document.querySelectorAll('.task').forEach(b=>b.onclick=()=>openTask(b.dataset.id));document.querySelectorAll('.history-toggle').forEach(b=>b.onclick=()=>{expandedVideo=expandedVideo===b.dataset.video?null:b.dataset.video;renderList()});}
async function refresh(){try{const d=await api('/api/tasks');tasks=d.tasks;if(d.max_chars)maxChars=d.max_chars;renderList();$('connection').textContent='本机已连接';$('provider').textContent=d.provider==='mock'?'Mock 占位输出':d.provider+' / '+d.model;if(active){const t=tasks.find(x=>x.task_id===active);if(t&&['generating','reviewing','rewriting'].includes(t.status))openTask(active,false)}}catch(e){$('connection').textContent='连接失败';console.error(e)}}
function stepStatus(events,node){let e=events.filter(x=>x.node===node);if(e.some(x=>x.event_type==='node_failed'||x.event_type==='technical_failure'))return'fail';if(e.some(x=>x.event_type==='checkpoint_saved'||x.event_type==='model_reply'||x.event_type==='rule_match'||x.event_type==='quality_route'||x.event_type==='handoff'))return'done';return''}
function eventText(e){const p=e.payload||{};if(e.event_type==='checkpoint_saved')return `${p.stage||''}`;if(e.event_type==='model_error'||e.event_type==='technical_failure'||e.event_type==='node_failed')return `${p.task||''} · ${p.error||''}`;if(e.event_type==='model_reply')return `已完成一次模型处理`;if(e.event_type==='model_cache_hit')return`已使用缓存`;if(e.event_type==='quality_route')return `已完成自动检查`;if(e.event_type==='human_decision')return `${p.decision?.decision} · ${p.decision?.candidate_id}`;return p.stage||p.status||''}
function render(t){const s=t.state||{},events=t.events||[],video=s.video||{},evidence=video.evidence||[],rules=s.rules||{},decisions=s.candidate_decisions||[],humanDecisions=s.human_decisions||[],reviews=s.reviews||[],candidates=(()=>{const all=s.candidates||[];const r0=s.route||{};const el=new Set(r0.eligible_candidate_ids||[]);return el.size?all.filter(c=>el.has(c.candidate_id)):all.slice(0,3)})();const route=s.route||{},modelProvider=t.config?.model_provider||s.run_manifest?.config?.model_provider||'unknown',isMock=modelProvider==='mock';const timeline=['N00','N03','N04','N05','N07','N09','R01','Q01','N12'].map(n=>`<div class="step ${stepStatus(events,n)}"><b>${nodeName[n]}</b><div class="small muted">${esc(n)}</div>${events.filter(e=>e.node===n).slice(-2).map(e=>`<div class="small">${esc(eventText(e))}</div>`).join('')}</div>`).join('');
const risk=s.narrative_risk||{};const riskSignals=risk.signals||[];const riskBadge=risk.level&&risk.level!=='none'?`<span class="badge bad" title="${esc(riskSignals.map(x=>x.detail).join('；'))}">低可信生成</span>`:'';const riskNote=riskSignals.length?`<div class="risk-note"><b>生成可信度低</b>：${riskSignals.map(x=>`${esc(x.detail)}（${esc(x.value)}）`).join('；')}。<br><span class="small muted">判定标准不变——按标题是否可用判三档；这条标记只说明标题是推断出来的，将来供分发降权使用。</span></div>`:'';
let cards=candidates.map(c=>{const r=reviews.find(x=>x.candidate_id===c.candidate_id)||{},d=decisions.find(x=>x.candidate_id===c.candidate_id)||{},h=[...humanDecisions].reverse().find(x=>x.candidate_id===c.candidate_id);const candidateStatus=h?(h.decision==='approved'?'已采用':h.decision==='marginal'?'勉强通过':'不可用'):'待人工评审';const issues=[...(r.issues||[])];for(const x of (c.mechanical_checks?.errors||[]))issues.push({issue_code:x.code,reason:x.reason,action:'按机械校验提示修正'});const form=h?`<div class="decision-result">${h.reason?`<span class="muted">${esc(h.reason)}</span>`:''}<button type="button" onclick="reopenDecision('${esc(c.candidate_id)}')">撤回并重新评审</button></div>`:`<div class="reviewform"><button class="primary" onclick="decide('${esc(c.candidate_id)}','approved')">通过</button><button onclick="openMarginal('${esc(c.candidate_id)}',this)">勉强通过</button><button onclick="openReject('${esc(c.candidate_id)}',this)">驳回</button><details><summary>编辑后通过</summary><input id="final-${esc(c.candidate_id)}" value="${esc(c.rendered_title||c.headline)}" aria-label="修改后的最终标题" oninput="showLen('len-${esc(c.candidate_id)}',this.value)"><span id="len-${esc(c.candidate_id)}" class="small muted"></span><button class="primary" style="margin-top:6px" onclick="decide('${esc(c.candidate_id)}','approved',true)">保存并通过</button></details></div>`;const reviewDetails=(Object.keys(r.gates||{}).length||Object.keys(r.quality||{}).length||issues.length||r.validation_errors?.length)?`<details class="candidate-detail"><summary>评审详情${issues.length?` · ${issues.length} 项需关注`:''}</summary>${issues.map(i=>`<div class="issue"><b>${esc(i.issue_code||i.issue_id||'问题')}</b><div>${esc(i.reason)}</div></div>`).join('')}${r.validation_errors?.length?`<div class="issue">结构校验：${esc(r.validation_errors.join('；'))}</div>`:''}<pre>${pretty({gates:r.gates||{},quality:r.quality||{}})}</pre></details>`:'';return `<article class="candidate"><div class="candidate-top"><span class="candidate-id">${esc(c.candidate_id)}</span>${badge(candidateStatus)}</div><div class="headline">${esc(c.rendered_title||c.headline)}</div><div class="candidate-bottom"><span class="small muted">${[...String(c.rendered_title||c.headline||'')].length} 字</span>${form}</div>${reviewDetails}</article>`}).join('');
const promptEvents=events.filter(e=>e.event_type==='prompt_snapshot'),modelCalls=events.filter(e=>e.event_type==='model_reply');const prompts=promptEvents.map(e=>{const p=e.payload;return `<details><summary>${esc(nodeName[e.node]||e.node)} · ${esc(p.task)} · ${esc((p.prompt_hash||'').slice(0,12))}</summary><div class="small muted">调用适配器：${esc(p.model||'未知')} · 源文件：${esc(p.prompt_file)} → ${esc(p.prompt_constant)} · Prompt hash ${esc((p.prompt_hash||'').slice(0,12))} · request ${esc((p.request_hash||'').slice(0,12))}</div><h3>${isMock?'组装后传给 Mock 的系统提示词（未被大模型消费）':'实际发送的系统提示词'}</h3><pre>${esc(p.system_prompt||'')}</pre><h3>${isMock?'传给 Mock 适配器的用户上下文':'实际发送的用户上下文'}</h3><pre>${pretty(p.user_payload)}</pre></details>`}).join('');const promptStatus=promptEvents.length?(isMock?`<p class="notice">Mock 模式：记录了 ${promptEvents.length} 组传给 Mock 适配器的 Prompt 参数，但 Mock 不会调用大模型，也不会按这些提示词生成标题；${modelCalls.length} 条回复均为占位输出。</p>`:`<p class="notice">已记录 ${promptEvents.length} 次实际提示词调用；对应 ${modelCalls.length} 条模型回复。可展开查看每次调用的完整输入。</p>`):isMock&&modelCalls.length?`<p class="notice">旧 Mock 任务：标题来自确定性模板，未调用模型，也没有保存 Prompt 快照；无法评估模型效果或还原当时上下文。</p>`:modelCalls.length?`<p class="notice">这是一条旧任务：记录到 ${modelCalls.length} 条模型回复，但没有保存当时的 Prompt 快照，无法从现有日志还原完整系统提示词和用户上下文。新任务会逐次记录实际发送内容。</p>`:'<p class="notice">尚未记录到模型调用。任务可能还没运行到模型节点，或在第一个模型调用前失败。</p>';
const err=s.error?`<div class="notice"><b>技术失败：</b>${esc(s.error)}<br><span class="small">最近检查点：${esc(JSON.stringify(s.failure_checkpoint||{}))}</span>${t.status==='technical_failed'&&t.resume_compatible?` <button onclick="resumeTask('${esc(t.task_id)}')">从成功节点缓存续跑</button>`:t.status==='technical_failed'?'<p class="small">当前代码/配置与原任务不一致，不能续跑；请新建任务以免混用版本。</p>':''}</div>`:'';
$('content').innerHTML=`<div class="task-heading"><div><h2>${esc(video.title||t.video_id)}</h2><details class="task-meta"><summary>任务信息</summary><div class="small muted">${esc(t.task_id)} · ${esc(t.video_id)} · ${esc(video.domain?.一级||'未分类')} / ${esc(video.format||'未分类')}</div><div class="small muted">${esc(modelProvider==='unknown'?'历史任务':modelProvider)} · ${esc(t.config?.generation_model||s.run_manifest?.config?.generation_model||'')} / ${esc(t.config?.review_model||s.run_manifest?.config?.review_model||'')} · 规则 ${esc(s.run_manifest?.prompt_pack_version||'')} · 入口 ${esc(s.run_manifest?.launch_source||'旧任务未记录')}</div></details></div>${badge(statusName[t.status]||t.status)}</div>${err}<section class="card review-first"><div class="review-heading"><h2>标题评审</h2><span class="muted">${candidates.length} 个候选 · ${candidates.filter(c=>!humanDecisions.some(d=>d.candidate_id===c.candidate_id)).length} 待评审</span>${isMock?'<span class="mock-tag" title="确定性模板输出，没有调用大模型">Mock 占位</span>':''}${riskBadge}</div>${riskNote}${cards||'<div class="muted">暂无候选标题</div>'}<dialog id="reject-dialog" class="reject-dialog"><form id="reject-form"><h2>人工判定</h2><p class="small muted" id="reject-headline"></p><p class="decision-result" style="margin:6px 0 2px">判定结果：<span class="decision-mark rejected">不可用</span><span class="small muted">（驳回即不可用，无需再选）</span></p><label for="reject-type">驳回原因<span class="small muted">（建议选一个，便于归因）</span></label><select id="reject-type"><option value="">未分类</option><option value="fact">事实错误：主体、动作、道具、数字与视频不符</option><option value="context">关键背景缺失：读者会误解视频在讲什么</option><option value="point">没写到核心看点：写成了边角料或作者的话</option><option value="grammar">句子不完整：缺主语/动词/宾语，读不成句</option><option value="unclear">用词读不懂：缩写、生造词、黑话</option><option value="other">其他</option></select><label for="reject-criteria">问题归类（可选，精确到判据）</label><select id="reject-criteria"></select><p class="small muted" id="reject-criteria-hint" style="margin:4px 0 0"></p><label for="reject-detail">一句话理由（必填）</label><textarea id="reject-detail" required placeholder="用人话说标题哪里不合适；不确定问题来自表达还是上游理解时，直接写现象即可"></textarea><label for="reject-reviewer">审核人（可选）</label><input id="reject-reviewer" placeholder="审核人"><div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" onclick="closeReject()">取消</button><button type="submit" class="danger">确认提交</button></div></form></dialog><dialog id="marginal-dialog" class="reject-dialog"><form id="marginal-form"><h2>勉强通过</h2><p class="small muted" id="marginal-headline"></p><p class="small muted">能用，但有明确缺陷需要在记录里留痕。缺陷说明必填——否则灰度档会退化成"都能过"。</p><label for="marginal-detail">缺陷说明（必填）</label><textarea id="marginal-detail" required placeholder="例如：FT 是缩写，多数用户不认识；改成「金融时报」即可用"></textarea><label for="marginal-criteria">命中判据（可留空）</label><select id="marginal-criteria"></select><p class="small muted" id="marginal-criteria-hint" style="margin:4px 0 0"></p><label for="marginal-title">最终标题（默认沿用原标题）</label><input id="marginal-title" oninput="showLen('marginal-len',this.value)"><span id="marginal-len" class="small muted"></span><label for="marginal-reviewer">审核人（可选）</label><input id="marginal-reviewer" placeholder="审核人"><div class="row" style="justify-content:flex-end;margin-top:14px"><button type="button" onclick="$('marginal-dialog').close()">取消</button><button type="submit" class="primary">确认勉强通过</button></div></form></dialog></section><section class="card flow-card system-only"><details><summary>生成流程 · 9 个节点</summary><div class="timeline">${timeline}</div>${route.action?`<p>最终路由：${badge(route.action)} ${esc(route.reason)}</p>`:''}</details></section><div class="grid"><section class="card"><h2>输入与证据</h2><p><a href="${esc(video.url||'#')}" target="_blank" rel="noreferrer">打开视频来源</a> · 作者：${esc(video.author||'未知')}</p><details><summary>原始标题与描述</summary><pre>${esc(video.title||'')}\n\n${esc(video.description||'')}\n\n${esc(video.caption||'')}</pre></details><details><summary>证据列表 · ${evidence.length} 条</summary>${evidence.map(e=>`<div class="evidence"><b>${esc(e.evidence_id)} · ${esc(e.field)} · ${esc(e.quality)}</b><div>${esc(e.text)}</div><span class="small muted">${esc(e.locator||'')}</span></div>`).join('')}</details></section><section class="card system-only"><h2>事实边界与看点</h2><details><summary>Claims</summary><pre>${pretty(s.understanding?.claims||[])}</pre></details><details><summary>Boundary decisions</summary><pre>${pretty(s.boundary?.decisions||[])}</pre></details><details><summary>Highlights</summary><pre>${pretty(s.highlights||[])}</pre></details><details><summary>本次命中的规则 ${esc((rules.rule_ids||[]).join('、'))} · ${esc(rules.version||'')}</summary><p class="small muted">规则指纹：${esc(s.rule_bundle_hash||'')}</p><pre>${pretty({match_trace:rules.match_trace,common:rules.common,domain_rules:rules.domain_rules,form_rules:rules.form_rules})}</pre></details></section></div><section class="card system-only"><h2>Prompt 对照</h2>${promptStatus}${prompts||''}</section><section class="card system-only"><h2>任务事件</h2>${events.slice(-100).map(e=>`<details><summary>${esc(e.created_at)} · ${esc(e.node)} · ${esc(e.event_type)}</summary><pre>${pretty(e.payload)}</pre></details>`).join('')}</section>`;const rejectType=$('reject-type');if(rejectType)rejectType.onchange=()=>{const v=rejectType.value;$('reject-expression').classList.toggle('show',v==='expression');$('reject-upstream').classList.toggle('show',v==='upstream');$('reject-rewrite').classList.toggle('show',v==='manual_rewrite');$('reject-common').style.display=v==='expression'?'block':'none';};const rejectForm=$('reject-form');if(rejectForm)rejectForm.onsubmit=submitReject;const marginalForm=$('marginal-form');if(marginalForm)marginalForm.onsubmit=submitMarginal;
}
async function openTask(id,refreshPage=true){active=id;try{const d=await api('/api/task?task_id='+encodeURIComponent(id));if(tasks.find(x=>x.task_id===id))expandedVideo=d.task.video_id;render(d.task);if(refreshPage)await refresh()}catch(e){alert(e.message)}}
function criteriaOptions(selected){return '<option value="">根因待定位（留空即可，由 AI 结合证据分析）</option>'+criteriaList.map(c=>`<option value="${esc(c.id)}" ${selected===c.id?'selected':''}>${esc(c.id)} ${esc(c.name)}（${esc(c.gate)}）</option>`).join('')}
async function reopenDecision(candidate){if(!confirm('撤回这条候选的当前人工判定？当前金标准行和人工标签会被移除，之后可重新评审。'))return;try{await api('/api/decision',{method:'POST',body:JSON.stringify({task_id:active,candidate_id:candidate,decision:'reopen'})});await openTask(active)}catch(e){alert(e.message)}}
async function maybeAdvance(taskId){const t=tasks.find(x=>x.task_id===taskId);if(!t||t.review_status!=='done')return;const n=tasks.find(x=>x.task_id!==taskId&&['unreviewed','in_progress'].includes(x.review_status)&&x.status==='pending_human');if(n)await openTask(n.task_id)}
async function decide(candidate,decision,edited=false){const t=active;const body={task_id:t,candidate_id:candidate,decision,final_headline:edited?$('final-'+candidate)?.value:null,reviewer:$('reviewer-'+candidate)?.value||'',reason:''};try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}
function openMarginal(candidate,button){marginalTarget=candidate;const article=button.closest('article');const title=article?.querySelector('.headline')?.textContent||'';$('marginal-headline').textContent='当前标题：'+title;$('marginal-form').reset();$('marginal-title').value=title;$('marginal-criteria').innerHTML=criteriaOptions('');$('marginal-criteria-hint').textContent='';$('marginal-criteria').onchange=()=>{const c=criteriaList.find(x=>x.id===$('marginal-criteria').value);$('marginal-criteria-hint').textContent=c?('判据条件：'+c.condition):''};showLen('marginal-len',title);$('marginal-dialog').showModal()}
async function submitMarginal(event){event.preventDefault();const defect=$('marginal-detail').value.trim();if(!defect){alert('勉强通过必须写清缺陷');return}const body={task_id:active,candidate_id:marginalTarget,decision:'marginal',reason:defect,criteria_id:$('marginal-criteria').value,final_headline:$('marginal-title').value.trim()||null,reviewer:$('marginal-reviewer').value};const t=active;try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});$('marginal-dialog').close();await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}
function openReject(candidate,button){rejectTarget=candidate;$('reject-headline').textContent='当前标题：'+button.closest('article')?.querySelector('.headline')?.textContent;$('reject-form').reset();$('reject-criteria').innerHTML='<option value="">不确定 / 留给 AI 归因</option>'+criteriaList.filter(c=>!['C11','C12','C13'].includes(c.id)).map(c=>`<option value="${esc(c.id)}">${esc(c.id)} ${esc(c.name)}</option>`).join('');$('reject-criteria-hint').textContent='';$('reject-criteria').onchange=()=>{const c=criteriaList.find(x=>x.id===$('reject-criteria').value);$('reject-criteria-hint').textContent=c?('可选归类：'+c.condition):''};$('reject-dialog').showModal()}
function closeReject(){$('reject-dialog')?.close()}
async function submitReject(event){event.preventDefault();const cause=$('reject-type').value,criteria=$('reject-criteria').value,detail=$('reject-detail').value.trim();if(!detail){alert('请写一句话说明标题哪里不合适');return}const body={task_id:active,candidate_id:rejectTarget,decision:'rejected',reason_type:cause||null,criteria_id:criteria||null,reason:detail,reviewer:$('reject-reviewer').value};const t=active;try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});closeReject();await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}
async function resumeTask(id){try{await api('/api/resume',{method:'POST',body:JSON.stringify({task_id:id})});await openTask(id)}catch(e){alert(e.message)}}
$('run').onclick=async()=>{const value=$('videoSearch').value.trim();const video=videos.find(x=>x.video_id===value)||videos.find(x=>x.title===value);if(!video){alert('请从列表中选择视频 ID 或完整标题');return}try{const d=await api('/api/run',{method:'POST',body:JSON.stringify({video_id:video.video_id})});active=d.task_id;await refresh();await openTask(active)}catch(e){alert(e.message)}};
$('refresh').onclick=refresh;
async function init(){try{const [d,c]=await Promise.all([api('/api/videos'),api('/api/criteria')]);criteriaList=c.criteria||[];videos=d.videos;$('videos').innerHTML=videos.map(v=>`<option value="${esc(v.video_id)}">${esc(v.title)} · ${esc(v.format||'')}</option>`).join('');await refresh();const q=new URLSearchParams(location.search).get('task_id');if(q)openTask(q)}catch(e){$('connection').textContent='连接失败';$('content').innerHTML=`<div class="notice">${esc(e.message)}</div>`}}
init();poll=setInterval(refresh,3500);
</script></body></html>'''


REJECTION_REASONS = {
    "fact": "事实错误",
    "context": "关键背景缺失",
    "point": "没写到核心看点",
    "grammar": "句子不完整",
    "unclear": "用词读不懂",
    "other": "其他",
}


def _clean(value: Any) -> str | None:
    """前端"采用"时 final_headline 传 null，str(None) 会落成字符串 "None"。"""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in {"", "None", "null", "undefined"} else text


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: getattr(value, k) for k in value.__dataclass_fields__}
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def serve(config: RunConfig, dataset: Dataset, db_path: str | list[str], pipeline_factory, host: str = "127.0.0.1", port: int = 8765) -> None:
    """db_path 可以是单个路径，也可以是多个。多个时按 task_id 路由到所属库。

    历史任务散在很多库里（每次实验一个目录），只看一个库会把大部分候选挡在门外。
    """
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("debugger only supports a loopback host")
    db_paths = [str(x) for x in ([db_path] if isinstance(db_path, (str, Path)) else db_path)]
    primary_db = db_paths[0]
    locks: set[str] = set()
    lock = threading.Lock()
    task_db: dict[str, str] = {}

    def index_tasks() -> None:
        for db in db_paths:
            try:
                store = Storage(db)
            except Exception:
                continue
            try:
                for row in store.list_tasks(limit=200):
                    task_db[row["task_id"]] = db
            finally:
                store.conn.close()

    index_tasks()

    def storage_for(task_id: str | None = None) -> Storage:
        if task_id and task_id not in task_db:
            index_tasks()
        # 判定写案例时用同一个独立案例库，保证跨实验目录可复用
        return Storage(task_db.get(task_id, primary_db) if task_id else primary_db, case_bank_path=(resolve_case_bank(config) or None))

    for db in db_paths:
        try:
            recovery = Storage(db)
        except Exception:
            continue
        try:
            for row in recovery.list_tasks(limit=200):
                if row["status"] in {"prepared", "generating", "reviewing", "rewriting"}:
                    state = json.loads(row["state_json"] or "{}")
                    state.update({"error": "调试台在任务执行中退出；可按原运行指纹续跑", "failure_retryable": True})
                    recovery.set_state(row["task_id"], "technical_failed", state)
                    recovery.event(row["task_id"], "X01", "technical_failure", {"error": state["error"], "retryable": True, "resume_available": True, "reason": "debugger_process_restarted"})
        finally:
            recovery.conn.close()

    def make_handler_storage() -> Storage:
        return Storage(primary_db)

    def worker(task_id: str, resume: bool = False) -> None:
        try:
            storage = storage_for(task_id)
            pipeline = pipeline_factory(storage)
            if resume:
                pipeline.resume(task_id)
            else:
                pipeline.execute(task_id)
            storage.conn.close()
        except Exception as exc:
            storage = storage_for(task_id)
            try:
                row = storage.get_task(task_id)
                state = json.loads(row["state_json"] or "{}")
                state["error"] = str(exc)
                storage.set_state(task_id, "technical_failed", state)
                storage.event(task_id, "X01", "technical_failure", {"error": str(exc), "resume_available": False})
            finally:
                storage.conn.close()
        finally:
            with lock:
                locks.discard(task_id)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TitleDebugger/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            # Avoid writing prompt payloads, transcript data or request bodies to logs.
            return

        def send_json(self, data: Any, status: int = 200) -> None:
            raw = json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ValueError("request too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request must be a JSON object")
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                raw = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == "/api/videos":
                rows = [{"video_id": str(row.get("videoID", "")), "title": str(row.get("title", "")), "format": str(row.get("内容形态", "")), "domain": row.get("内容领域", {})} for row in dataset.rows]
                self.send_json({"videos": rows})
                return
            if parsed.path == "/api/criteria":
                self.send_json({"criteria": read_criteria()})
                return
            if parsed.path == "/api/tasks":
                tasks = []
                for db in db_paths:
                    try:
                        storage = Storage(db)
                    except Exception:
                        continue
                    try:
                        for row in storage.list_tasks():
                            state = json.loads(row.pop("state_json") or "{}")
                            cands = state.get("candidates") or []
                            human = state.get("human_decisions") or []
                            eligible = (state.get("route") or {}).get("eligible_candidate_ids") or []
                            pool = eligible or [c.get("candidate_id") for c in cands]
                            judged = {h.get("candidate_id") for h in human if h.get("candidate_id")}
                            done = sum(1 for p in pool if p in judged)
                            if not pool:
                                review_status = "no_candidates"
                            elif done == 0:
                                review_status = "unreviewed"
                            elif done >= len(pool):
                                review_status = "done"
                            else:
                                review_status = "in_progress"
                            tasks.append({**row, "db": db, "title": state.get("video", {}).get("title", row.get("video_id")), "launch_source": (state.get("run_manifest") or {}).get("launch_source", "unknown"), "review_status": review_status, "review_done": done, "review_total": len(pool)})
                    finally:
                        storage.conn.close()
                tasks.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
                self.send_json({"tasks": tasks, "provider": config.model_provider, "model": f"{config.generation_model} / {config.review_model}", "max_chars": config.max_chars})
                return
            if parsed.path == "/api/task":
                query = parse_qs(parsed.query)
                task_id = (query.get("task_id") or [""])[0]
                if not task_id:
                    self.send_json({"error": "task_id required"}, 400)
                    return
                storage = storage_for(task_id)
                try:
                    row = storage.get_task(task_id)
                    state = json.loads(row["state_json"] or "{}")
                    config_snapshot = json.loads(row["config_json"] or "{}")
                    events = storage.events(task_id)
                    manifest = state.get("run_manifest") or {}
                    current_hashes = pipeline_factory(storage)._manifest(task_id).get("source_hashes")
                    resume_compatible = bool(manifest.get("source_hashes") == current_hashes and config_snapshot == asdict(config))
                    self.send_json({"task": {"task_id": row["task_id"], "video_id": row["video_id"], "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"], "state": state, "config": config_snapshot, "resume_compatible": resume_compatible, "events": events}})
                except KeyError:
                    self.send_json({"error": "task not found"}, 404)
                finally:
                    storage.conn.close()
                return
            self.send_json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            try:
                body = self.read_body()
                if self.path == "/api/run":
                    video_id = str(body.get("video_id", ""))
                    if video_id not in {str(x.get("videoID")) for x in dataset.rows}:
                        self.send_json({"error": "unknown video_id"}, 400)
                        return
                    with lock:
                        if locks:
                            self.send_json({"error": "已有任务正在执行；单任务调试台会等当前任务结束后再启动"}, 409)
                            return
                        storage = make_handler_storage()
                        try:
                            pipeline = pipeline_factory(storage)
                            task_id = pipeline.prepare(video_id, "debugger.ui")
                        finally:
                            storage.conn.close()
                        locks.add(task_id)
                    threading.Thread(target=worker, args=(task_id,), daemon=True, name=f"title-task-{task_id}").start()
                    self.send_json({"task_id": task_id, "status": "prepared"}, 202)
                    return
                if self.path == "/api/resume":
                    task_id = str(body.get("task_id", ""))
                    storage = storage_for(task_id)
                    try:
                        row = storage.get_task(task_id)
                        if row["status"] != "technical_failed":
                            self.send_json({"error": f"task is not resumable: {row['status']}"}, 409)
                            return
                        state = json.loads(row["state_json"] or "{}")
                        manifest = state.get("run_manifest") or {}
                        current_hashes = pipeline_factory(storage)._manifest(task_id).get("source_hashes")
                        if json.loads(row["config_json"] or "{}") != asdict(config) or manifest.get("source_hashes") != current_hashes:
                            self.send_json({"error": "Prompt、规则、代码或运行配置已变更；为保证复现，请用当前版本创建新任务"}, 409)
                            return
                    finally:
                        storage.conn.close()
                    with lock:
                        if locks:
                            self.send_json({"error": "已有任务正在执行；请等当前任务结束后再续跑"}, 409)
                            return
                        locks.add(task_id)
                    threading.Thread(target=worker, args=(task_id, True), daemon=True, name=f"title-resume-{task_id}").start()
                    self.send_json({"task_id": task_id, "status": "resuming"}, 202)
                    return
                if self.path == "/api/decision":
                    task_id = str(body.get("task_id", ""))
                    if body.get("decision") == "reopen":
                        storage = storage_for(task_id)
                        try:
                            row = storage.get_task(task_id)
                            storage.reopen_human_decision(task_id, str(body.get("candidate_id", "")))
                        finally:
                            storage.conn.close()
                        remove_gold(video_id=row["video_id"], candidate_id=str(body.get("candidate_id", "")), task_id=task_id)
                        self.send_json({"task_id": task_id, "status": "reopened"})
                        return
                    decision = HumanDecision(candidate_id=str(body.get("candidate_id", "")), decision=str(body.get("decision", "")), final_headline=_clean(body.get("final_headline")) if body.get("decision") in {"approved", "marginal"} else None, reason=str(body.get("reason", "")), reviewer=str(body.get("reviewer", "")) or None)
                    reason_type = str(body.get("reason_type") or "")
                    if decision.decision == "rejected":
                        if not decision.reason.strip():
                            raise ValueError("驳回必须填写一句话理由")
                    storage = storage_for(task_id)
                    gold_path = None
                    try:
                        payload = {"candidate_id": decision.candidate_id, "decision": decision.decision, "final_headline": decision.final_headline, "reason": decision.reason, "reviewer": decision.reviewer}
                        # 判据 ID 与驳回原因对三档都要落库：只写进 gold.csv 会让库里的判定
                        # 无法按判据归因，而库才是后续重评与反馈账本的来源。
                        if decision.decision in {"rejected", "marginal"}:
                            payload.update({"reason_type": reason_type or None, "criteria_id": str(body.get("criteria_id") or "") or None})
                        storage.record_human_decision(task_id, payload)
                        # 判定同时写进 gold.csv：否则"在评审台上判了几十条"和"能出四个数"永远接不上
                        row = storage.get_task(task_id)
                        state = json.loads(row["state_json"] or "{}")
                        candidate = next((c for c in (state.get("candidates") or []) if c.get("candidate_id") == decision.candidate_id), {})
                        route = state.get("route") or {}
                        # 「系统推荐」写的是系统原判断，不是被人工改过的可审范围
                        original_route = state.get("original_system_route") or route
                        gold_path = record_gold(
                            video_id=row["video_id"],
                            task_id=task_id,
                            candidate_id=decision.candidate_id,
                            headline=str(candidate.get("rendered_title") or candidate.get("headline") or ""),
                            verdict={"approved": "可用", "marginal": "勉强通过"}.get(decision.decision, "不可用"),
                            final_headline=payload.get("final_headline"),
                            criteria_id=str(body.get("criteria_id") or "") if body.get("criteria_id") else "",
                            rejection_reason=REJECTION_REASONS.get(str(body.get("reason_type") or ""), ""),
                            fallback_node=str(body.get("fallback_node") or ""),
                            reason=decision.reason,
                            reviewer=decision.reviewer or "unnamed",
                            route=str(route.get("action") or ""),
                            recommended="是" if original_route.get("recommended_candidate_id") == decision.candidate_id else "",
                        )
                    finally:
                        storage.conn.close()
                    self.send_json({"task_id": task_id, "status": "saved", "gold": str(gold_path) if gold_path else None})
                    return
                self.send_json({"error": "not found"}, 404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"标题任务调试台：http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("调试台已停止")
    finally:
        server.server_close()
