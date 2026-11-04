# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/debugger.py"
doc = io.open(p, encoding="utf-8").read()

old1 = """$('taskList').innerHTML=[...groups.entries()].map(([videoId,items])=>{const latest=items[0],open=expandedVideo===videoId||items.some(x=>x.task_id===active&&x.task_id!==latest.task_id);return `<div class="task-group"><button class="task ${active===latest.task_id?'active':''}" data-id="${esc(latest.task_id)"><b>${esc(latest.title||videoId)</b><br><span class="small muted task-status">${esc(videoId) · ${esc(statusName[latest.status]||latest.status)</span></button>"""
new1 = """const __rank={unreviewed:0,in_progress:1,done:2,no_candidates:3};const __name={unreviewed:'还未评',in_progress:'评审中',done:'已评完',no_candidates:'无候选'};const __groups=[...groups.entries()].sort((a,b)=>(__rank[a[1][0].review_status]??9)-(__rank[b[1][0].review_status]??9));$('taskList').innerHTML=__groups.map(([videoId,items])=>{const latest=items[0],open=expandedVideo===videoId||items.some(x=>x.task_id===active&&x.task_id!==latest.task_id);const __badge=latest.review_status?`<span class="badge ${latest.review_status==='done'?'good':latest.review_status==='in_progress'?'warn':''}">${esc(__name[latest.review_status]||'')${latest.review_status==='in_progress'?' '+latest.review_done+'/'+latest.review_total:''</span>`:'';return `<div class="task-group"><button class="task ${active===latest.task_id?'active':''}" data-id="${esc(latest.task_id)"><b>${esc(latest.title||videoId)</b><br><span class="small muted task-status">${esc(videoId) · ${esc(statusName[latest.status]||latest.status) ${__badge</span></button>"""

if old1 in doc:
    doc = doc.replace(old1, new1); print("renderList ok")
else:
    print("renderList MISS")

old2 = """async function decide(candidate,decision,edited=false){const t=active;const body={task_id:t,candidate_id:candidate,decision,final_headline:edited?$('final-'+candidate)?.value:null,reviewer:$('reviewer-'+candidate)?.value||'',reason:''};try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});await openTask(t)}catch(e){alert(e.message)}}"""
new2 = """async function maybeAdvance(taskId){const t=tasks.find(x=>x.task_id===taskId);if(!t||t.review_status!=='done')return;const n=tasks.find(x=>x.task_id!==taskId&&['unreviewed','in_progress'].includes(x.review_status)&&x.status==='pending_human');if(n)await openTask(n.task_id)}
async function decide(candidate,decision,edited=false){const t=active;const body={task_id:t,candidate_id:candidate,decision,final_headline:edited?$('final-'+candidate)?.value:null,reviewer:$('reviewer-'+candidate)?.value||'',reason:''};try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}"""
if old2 in doc: doc = doc.replace(old2, new2); print("decide ok")
else: print("decide MISS")

old3 = """async function submitMarginal(event){event.preventDefault();const defect=$('marginal-detail').value.trim();if(!defect){alert('勉强通过必须写清缺陷');return}const body={task_id:active,candidate_id:marginalTarget,decision:'marginal',reason:defect,criteria_id:$('marginal-criteria').value,final_headline:$('marginal-title').value.trim()||null,reviewer:$('marginal-reviewer').value};try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});$('marginal-dialog').close();await openTask(active)}catch(e){alert(e.message)}}"""
new3 = """async function submitMarginal(event){event.preventDefault();const defect=$('marginal-detail').value.trim();if(!defect){alert('勉强通过必须写清缺陷');return}const body={task_id:active,candidate_id:marginalTarget,decision:'marginal',reason:defect,criteria_id:$('marginal-criteria').value,final_headline:$('marginal-title').value.trim()||null,reviewer:$('marginal-reviewer').value};const t=active;try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});$('marginal-dialog').close();await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}"""
if old3 in doc: doc = doc.replace(old3, new3); print("submitMarginal ok")
else: print("submitMarginal MISS")

old4 = """async function submitReject(event){event.preventDefault();const cause=$('reject-type').value,criteria=$('reject-criteria').value,detail=$('reject-detail').value.trim();if(!detail){alert('请写一句话说明标题哪里不合适');return}const body={task_id:active,candidate_id:rejectTarget,decision:'rejected',reason_type:cause||null,criteria_id:criteria||null,reason:detail,reviewer:$('reject-reviewer').value};try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});closeReject();await openTask(active)}catch(e){alert(e.message)}}"""
new4 = """async function submitReject(event){event.preventDefault();const cause=$('reject-type').value,criteria=$('reject-criteria').value,detail=$('reject-detail').value.trim();if(!detail){alert('请写一句话说明标题哪里不合适');return}const body={task_id:active,candidate_id:rejectTarget,decision:'rejected',reason_type:cause||null,criteria_id:criteria||null,reason:detail,reviewer:$('reject-reviewer').value};const t=active;try{await api('/api/decision',{method:'POST',body:JSON.stringify(body)});closeReject();await openTask(t);await maybeAdvance(t)}catch(e){alert(e.message)}}"""
if old4 in doc: doc = doc.replace(old4, new4); print("submitReject ok")
else: print("submitReject MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
