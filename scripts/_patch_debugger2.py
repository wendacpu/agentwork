# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/debugger.py"
doc = io.open(p, encoding="utf-8").read()

oldA = "$('taskList').innerHTML=[...groups.entries()].map(([videoId,items])=>{const latest=items[0],open=expandedVideo===videoId||items.some(x=>x.task_id===active&&x.task_id!==latest.task_id);return"
newA = "const __rank={unreviewed:0,in_progress:1,done:2,no_candidates:3};const __name={unreviewed:'还未评',in_progress:'评审中',done:'已评完',no_candidates:'无候选'};const __groups=[...groups.entries()].sort((a,b)=>(__rank[a[1][0].review_status]??9)-(__rank[b[1][0].review_status]??9));$('taskList').innerHTML=__groups.map(([videoId,items])=>{const latest=items[0],open=expandedVideo===videoId||items.some(x=>x.task_id===active&&x.task_id!==latest.task_id);const __badge=latest.review_status?'<span class=\"badge '+(latest.review_status==='done'?'good':latest.review_status==='in_progress'?'warn':'')+'\">'+(__name[latest.review_status]||'')+(latest.review_status==='in_progress'?' '+latest.review_done+'/'+latest.review_total:'')+'</span>':'';return"
if oldA in doc:
    doc = doc.replace(oldA, newA); print("A ok")
else:
    print("A MISS")

oldB = "${esc(statusName[latest.status]||latest.status)}</span></button>"
newB = "${esc(statusName[latest.status]||latest.status)} ${__badge</span></button>"
if oldB in doc:
    doc = doc.replace(oldB, newB); print("B ok")
else:
    print("B MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
