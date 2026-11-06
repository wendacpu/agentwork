#!/usr/bin/env python3
"""从历史隔离反馈构造本地评审选型开发题库；不改源库、不调用模型。"""
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'runtime/cost_ab_20261105'
SUPP = ROOT / 'runtime/cost_ab_supplement_review'
OUT = ROOT / 'runtime/review_selection_development_v2'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def build():
    with connect(BASE / 'agent.db') as db:
        tasks = {t: (v, json.loads(s)) for t, v, s in db.execute('select task_id,video_id,state_json from tasks')}
        pools = {}
        for tid, payload in db.execute('select task_id,payload_json from candidates_by_task'):
            x = json.loads(payload)
            pools[(tid, str(x.get('candidate_id') or ''), digest(str(x.get('rendered_title') or x.get('headline') or '')))] = x
    with connect(BASE / 'human_feedback_v2.db') as db:
        feedback = [json.loads(row[0]) for row in db.execute('select payload_json from candidate_decisions_v2 where batch_id=?', (BASE.name,))]
    manifest = json.loads((SUPP / 'supplement_manifest.json').read_text(encoding='utf-8'))
    supplements = {}
    for t in manifest:
        for c in t['candidates']:
            supplements[f"{t['task_id']}|{c['candidate_id']}|{c['hash']}"] = (t, c)
    with connect(SUPP / 'supplement_feedback.db') as db:
        supplemental = list(db.execute('select key,decision,reason,updated_at from decisions'))
    records = []; seen = set()
    for f in feedback:
        key = (str(f['task_id']), str(f['candidate_id']), str(f['title_hash']))
        if key in seen: raise ValueError(f'重复标签 {key}')
        seen.add(key)
        records.append((key, str(f['original_headline']), f['decision'], f.get('reason',''), '原批_'+str(f.get('scope') or 'frozen'), f.get('updated_at'), f.get('reviewer'), f.get('final_headline')))
    for raw, verdict, reason, updated in supplemental:
        if raw not in supplements: raise ValueError('补评候选未找到快照')
        t, x = supplements[raw]; key=(t['task_id'], x['candidate_id'], x['hash'])
        if key in seen: raise ValueError(f'跨库重复标签 {key}')
        seen.add(key)
        records.append((key, x['title'], verdict, reason, '补评_内容判断', updated, None, None))
    blind=[]; labels=[]; video_hashes={}
    for (tid,cid,h), title, verdict, reason, scope, updated, reviewer, final in records:
        if digest(title)!=h: raise ValueError(f'标题哈希不匹配 {tid}/{cid}')
        if tid not in tasks: raise ValueError(f'任务不存在 {tid}')
        vid,state=tasks[tid]; video=state.get('video') or {}
        if video.get('video_id') != vid: raise ValueError(f'视频身份不一致 {tid}')
        candidate=pools.get((tid,cid,h))
        if candidate is None:
            for x in (state.get('candidates') or [])+(state.get('delivery_candidates') or [])+(state.get('pending_review_candidates') or []):
                if str(x.get('candidate_id') or '')==cid and digest(str(x.get('rendered_title') or x.get('headline') or ''))==h:
                    candidate=x; break
        if candidate is None: raise ValueError(f'候选身份不匹配 {tid}/{cid}')
        item_id=digest(canonical([tid,cid,h]))[:20]
        fields=('video_id','title','author','domain','format','description','caption','asr_segments','ocr_segments','evidence')
        materials={k:video.get(k) for k in fields}
        provenance=video.get('provenance') or {}
        materials['source_quality']={'description_source':provenance.get('description_source','unknown'),'caption_source':provenance.get('caption_source','unknown'),'ocr_mode':provenance.get('ocr_mode','unknown')}
        vh=digest(canonical(materials)); video_hashes[vid]=vh
        blind.append({'item_id':item_id,'video_id':vid,'candidate_title':title,'materials':materials})
        checks=candidate.get('mechanical_checks') or {}; codes=[e.get('code') for e in checks.get('errors',[]) if isinstance(e,dict)]
        decisions={str(d.get('candidate_id')):d for d in state.get('candidate_decisions') or []}; old=decisions.get(cid) or {}
        labels.append({'item_id':item_id,'source_task_id':tid,'candidate_id':cid,'title_sha256':h,'video_id':vid,'materials_sha256':vh,'human_decision':verdict,'human_reason':reason,'human_final_headline':final,'feedback_scope':scope,'feedback_updated_at':updated,'reviewer':reviewer,'historical_candidate_status':old.get('status'),'historical_candidate_reason':old.get('reason'),'mechanical_error_codes':codes,'overlength':len(title)>20,'is_supplement_label':scope=='补评_内容判断','semantic_label_eligible':not (scope=='补评_内容判断' and len(title)>20 and '字数超' in str(reason)),'claim_ids':candidate.get('claim_ids') or [],'evidence_ids':candidate.get('evidence_ids') or [],'highlight_id':candidate.get('highlight_id'),'parent_candidate_id':candidate.get('parent_candidate_id')})
    blind.sort(key=lambda x:x['item_id']); labels.sort(key=lambda x:x['item_id'])
    return blind,labels,video_hashes


def main():
    blind,labels,video_hashes=build(); OUT.mkdir(parents=True,exist_ok=True)
    files={'blind_inputs.jsonl': ''.join(canonical(x)+'\n' for x in blind),'labels_provenance.jsonl': ''.join(canonical(x)+'\n' for x in labels)}
    for name,text in files.items():
        path=OUT/name
        if path.exists() and path.read_text(encoding='utf-8')!=text: raise RuntimeError(f'已有不同内容，拒绝覆盖：{path}')
        if not path.exists(): path.write_text(text,encoding='utf-8')
    summary={'purpose':'开发诊断；非独立验收集','sources':['runtime/cost_ab_20261105/agent.db','runtime/cost_ab_20261105/human_feedback_v2.db','runtime/cost_ab_supplement_review/supplement_manifest.json','runtime/cost_ab_supplement_review/supplement_feedback.db'],'items':len(labels),'videos':len(video_hashes),'labels':dict(Counter(x['human_decision'] for x in labels)),'scopes':dict(Counter(x['feedback_scope'] for x in labels)),'overlength':sum(x['overlength'] for x in labels),'supplement_labels':sum(x['is_supplement_label'] for x in labels),'semantic_label_ineligible':sum(not x['semantic_label_eligible'] for x in labels),'source_status_missing':sum(not x['historical_candidate_status'] for x in labels),'video_material_sha256':video_hashes,'output_sha256':{k:digest(v) for k,v in files.items()}}
    text=json.dumps(summary,ensure_ascii=False,indent=2)+'\n'; path=OUT/'manifest.json'
    if path.exists() and path.read_text(encoding='utf-8')!=text: raise RuntimeError('已有不同清单，拒绝覆盖')
    if not path.exists(): path.write_text(text,encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
