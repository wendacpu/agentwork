"""冻结批次的离线回归：只读原任务，人工意见仅落独立反馈库。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from agent_system.batch_review import BatchReviewStore, make_server


PREFIX = "【剧情演绎】"
ORIGINAL = PREFIX + "十个闹钟也挡不住早八"
EDITED = PREFIX + "十个闹钟没叫醒早八人"


def _hash(title: str) -> str:
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def test_stale_title_hash_is_rejected(batch):
    store = BatchReviewStore(batch[0])
    with pytest.raises(ValueError, match='哈希不一致'):
        store.record_decision(_body(title_hash='incorrect'))


def _body(task_id="task-01", candidate_id="shared-c1", decision="approved", **extra):
    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "decision": decision,
        "reason": "",
        "criteria_id": "",
        "reason_type": "",
        "final_headline": "",
        "reviewer": "测试评审员",
        **extra,
    }


@pytest.fixture
def batch(tmp_path, monkeypatch):
    """只造 tasks 表，不借 Storage 建库，不依赖真实资产、模型或案例库。"""
    from agent_system import goldsheet
    from agent_system.memory import MemoryStore
    from agent_system.storage import Storage

    def forbidden(*args, **kwargs):
        pytest.fail("冻结批次不得调用原任务写入、案例回流或全局 gold 写入")

    for name in ("record", "upsert", "remove"):
        monkeypatch.setattr(goldsheet, name, forbidden)
    monkeypatch.setattr(Storage, "record_human_decision", forbidden)
    monkeypatch.setattr(Storage, "reopen_human_decision", forbidden)
    monkeypatch.setattr(MemoryStore, "create_draft", forbidden)
    monkeypatch.setattr(goldsheet, "GOLD", tmp_path / "禁止写入的全局gold.csv")

    batch_dir = tmp_path / "frozen-batch"
    batch_dir.mkdir()
    source_db = batch_dir / "agent.db"
    mapping_path = batch_dir / "review_mapping.json"
    mapping, states = [], {}
    with sqlite3.connect(source_db) as conn:
        conn.execute(
            "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, "
            "config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)"
        )
        for index in range(1, 21):
            task_id = f"task-{index:02d}"
            cid = "shared-c1" if index in (1, 2) else f"c-{index:02d}"
            headline = "十个闹钟也挡不住早八" if index == 1 else f"第{index}条完整原标题"
            prefixes = ["剧情演绎"] if index == 1 else []
            rendered = ORIGINAL if index == 1 else headline
            candidate = {
                "candidate_id": cid,
                "headline": headline,
                "prefix_labels": prefixes,
                "rendered_title": rendered,
            }
            alternative = {
                "candidate_id": f"other-{index}",
                "headline": "不应展示或被判定的其他候选",
                "rendered_title": "不应展示或被判定的其他候选",
            }
            status = "technical_failed" if index in (3, 20) else "pending_human"
            route = {
                "action": "handoff",
                "reason": "原系统推荐理由，不得被人工覆盖",
                "recommended_candidate_id": alternative["candidate_id"],
                "eligible_candidate_ids": [cid, alternative["candidate_id"]],
            }
            state = {
                "video": {"video_id": f"video-{index:02d}", "title": f"来源标题{index}", "asset_paths": {}},
                "candidates": [alternative, candidate],
                "pending_review_candidates": [],
                "route": route,
                "reviews": [{"candidate_id": cid, "decision": "系统评分，不应出现在盲评材料"}],
                "candidate_decisions": [{"candidate_id": cid, "status": "reject"}],
            }
            if index == 3:
                # 审核模型失败前的生成检查点仍然是真实候选。
                state["candidates"] = [alternative]
                state["pending_review_candidates"] = [candidate]
            if index == 20:
                state["candidates"] = []
                state["route"] = None
            states[task_id] = state
            mapping.append({
                "blind_id": f"R{index:02d}", "index": index,
                "task_id": task_id, "video_id": f"video-{index:02d}",
                "category": "测试分类", "source_title": f"来源标题{index}",
                "candidate_id": cid if index != 20 else None,
                "headline": rendered if index != 20 else None,
                "title_sha256": _hash(rendered) if index != 20 else None,
                "task_status": status,
                "candidate_source": "pending_review_checkpoint_not_final_delivery" if index == 3 else "final_state",
                "route": state["route"],
            })
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (
                task_id, f"video-{index:02d}", status, "{}",
                json.dumps(state, ensure_ascii=False), "2026-01-01", "2026-01-01",
            ))
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    original_db = source_db.read_bytes()
    original_mapping = mapping_path.read_bytes()
    yield batch_dir, states
    # 每条测试（包括判定、撤回、HTTP）都必须满足源文件字节级不变。
    assert source_db.read_bytes() == original_db
    assert mapping_path.read_bytes() == original_mapping
    assert not goldsheet.GOLD.exists()


def test_list_includes_all_twenty_tasks_and_selected_candidate_only(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    tasks = store.list_tasks()
    assert len(tasks) == 20
    assert {task["task_id"] for task in tasks} == {f"task-{i:02d}" for i in range(1, 21)}
    assert next(task for task in tasks if task["task_id"] == "task-20")["review_status"] == "no_candidates"
    task = store.get_task("task-01")
    assert task["state"]["video"]["title"] == "来源标题1"
    assert [candidate["candidate_id"] for candidate in task["state"]["candidates"]] == ["shared-c1"]
    assert task["state"]["candidates"][0]["rendered_title"] == ORIGINAL
    for hidden in ("route", "reviews", "candidate_decisions"):
        assert hidden not in task
        assert hidden not in task["state"]
    # 未冻结但真实存在的候选按诊断口径可评（见诊断口径用例）；批次外的标识仍必须被拒。
    with pytest.raises(ValueError):
        store.record_decision(_body(candidate_id="invented-id"))
    assert store.feedback()["decisions"] == []
    assert store.feedback()["diagnostic_decisions"] == []


def test_marginal_edit_preserves_original_title_hash_and_feedback_fields(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    result = store.record_decision(_body(
        decision="marginal", reason="主语可更清楚", criteria_id="Q3", reason_type="grammar",
        final_headline=EDITED, original_headline="客户端伪造原标题", title_hash=_hash(ORIGINAL),
    ))
    assert isinstance(result, dict)
    assert (batch_dir / "human_feedback_v2.db").is_file()
    # 新实例重新读取，避免只验证内存缓存。
    saved, = BatchReviewStore(batch_dir).feedback()["decisions"]
    assert saved["task_id"] == "task-01"
    assert saved["candidate_id"] == "shared-c1"
    assert saved["decision"] == "marginal"
    assert saved["reason"] == "主语可更清楚"
    assert saved["criteria_id"] == "Q3"
    assert saved["reason_type"] == "grammar"
    assert saved["reviewer"] == "测试评审员"
    assert saved["original_headline"] == ORIGINAL
    assert saved["title_hash"] == _hash(ORIGINAL)
    assert saved["final_headline"] == EDITED
    assert store.get_task("task-01")["state"]["candidates"][0]["rendered_title"] == ORIGINAL
    with sqlite3.connect(batch_dir / "human_feedback_v2.db") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "cases" not in tables


def test_platform_prefix_is_kept_and_cannot_be_removed_by_edit(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    store.record_decision(_body())
    saved, = store.feedback()["decisions"]
    assert saved["original_headline"] == ORIGINAL
    assert saved["final_headline"] == ORIGINAL
    assert saved["title_hash"] == _hash(ORIGINAL)
    with pytest.raises(ValueError):
        store.record_decision(_body(final_headline="十个闹钟没叫醒早八人"))
    assert store.feedback()["decisions"][0]["final_headline"] == ORIGINAL


@pytest.mark.parametrize("reason", [None, "", "  \n\t"])
@pytest.mark.parametrize("decision", ["rejected", "marginal"])
def test_negative_or_marginal_decision_requires_written_reason(batch, reason, decision):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    with pytest.raises(ValueError):
        store.record_decision(_body(decision=decision, reason=reason))
    assert store.feedback()["decisions"] == []


def test_same_candidate_id_is_isolated_by_task_including_reopen(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    store.record_decision(_body())
    store.record_decision(_body(task_id="task-02", decision="rejected", reason="不是主看点", criteria_id="Q1"))
    by_task = {row["task_id"]: row for row in store.feedback()["decisions"]}
    assert set(by_task) == {"task-01", "task-02"}
    assert by_task["task-01"]["decision"] == "approved"
    assert by_task["task-02"]["decision"] == "rejected"
    assert by_task["task-01"]["title_hash"] != by_task["task-02"]["title_hash"]
    store.record_decision(_body(decision="reopen"))
    remaining, = store.feedback()["decisions"]
    assert remaining == by_task["task-02"]
    assert store.get_task("task-01")["decision"] is None
    assert store.get_task("task-02")["decision"]["decision"] == "rejected"


def test_reopen_keeps_original_system_route_and_allows_rejudgment(batch):
    batch_dir, states = batch
    store = BatchReviewStore(batch_dir)
    store.record_decision(_body(decision="rejected", reason="不可用"))
    saved, = store.feedback()["decisions"]
    assert saved["original_system_route"] == states["task-01"]["route"]
    store.record_decision(_body(decision="reopen"))
    assert store.feedback()["decisions"] == []
    assert store.get_task("task-01")["decision"] is None
    assert next(task for task in store.list_tasks() if task["task_id"] == "task-01")["review_status"] == "unreviewed"
    with sqlite3.connect(batch_dir / "agent.db") as conn:
        state_json, status = conn.execute("SELECT state_json,status FROM tasks WHERE task_id='task-01'").fetchone()
    assert json.loads(state_json) == states["task-01"]
    assert status == "pending_human"
    with sqlite3.connect(batch_dir / "human_feedback_v2.db") as conn:
        events = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM decision_events ORDER BY id")]
    assert [event["decision"] for event in events] == ["rejected", "reopen"]
    assert all(event["original_system_route"] == states["task-01"]["route"] for event in events)
    store.record_decision(_body(decision="marginal", reason="编辑后可用", final_headline=EDITED))
    saved, = store.feedback()["decisions"]
    assert saved["decision"] == "marginal"
    assert saved["original_system_route"] == states["task-01"]["route"]


def test_technical_failed_task_recovers_real_pending_candidate(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    task = store.get_task("task-03")
    assert task["source_status"] == "technical_failed"
    candidate, = task["state"]["candidates"]
    assert candidate["candidate_id"] == "c-03"
    assert candidate["rendered_title"] == "第3条完整原标题"
    store.record_decision(_body(task_id="task-03", candidate_id="c-03"))
    saved, = store.feedback()["decisions"]
    assert saved["source_status"] == "technical_failed"
    assert saved["original_headline"] == "第3条完整原标题"


@pytest.mark.parametrize("candidate_id", [None, "", "invented", "shared-c1"])
def test_no_candidate_task_cannot_receive_fabricated_decision(batch, candidate_id):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    assert store.get_task("task-20")["state"]["candidates"] == []
    with pytest.raises(ValueError):
        store.record_decision(_body(task_id="task-20", candidate_id=candidate_id, final_headline="伪造的标题"))
    assert store.feedback()["decisions"] == []


def test_explicit_feedback_database_is_independent_and_source_cannot_be_target(batch, tmp_path):
    batch_dir, _ = batch
    target = tmp_path / "feedback-only" / "human.db"
    store = BatchReviewStore(batch_dir, feedback_db=target)
    store.record_decision(_body())
    assert target.is_file()
    assert not (batch_dir / "human_feedback_v2.db").exists()
    assert len(BatchReviewStore(batch_dir, feedback_db=target).feedback()["decisions"]) == 1
    with pytest.raises(ValueError):
        BatchReviewStore(batch_dir, feedback_db=batch_dir / "agent.db")


def test_http_round_trip_and_generation_endpoints_are_unavailable(batch):
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    server = make_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    def request(method, path, body=None, headers=None):
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, payload = request("GET", "/api/tasks")
        assert status == 200
        assert len(payload["tasks"]) == 20
        status, payload = request("GET", "/api/task?task_id=task-03")
        assert status == 200
        assert len(payload["task"]["state"]["candidates"]) == 1
        assert request("POST", "/api/decision", _body())[0] == 403
        headers = {"X-Batch-Review": "1", "Content-Type": "application/json"}
        assert request("POST", "/api/decision", _body(), {**headers, "Origin": "https://untrusted.example"})[0] == 403
        status, payload = request("POST", "/api/decision", _body(decision="marginal", reason="编辑后可用", final_headline=EDITED), headers)
        assert status == 200
        assert payload["decision"]["final_headline"] == EDITED
        status, payload = request("GET", "/api/feedback")
        assert status == 200
        assert payload["decisions"][0]["original_headline"] == ORIGINAL
        for path in ("/api/run", "/api/execute", "/api/resume"):
            assert request("POST", path, {}, headers)[0] == 404
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_legacy_feedback_is_read_only(batch):
    batch_dir, _ = batch
    legacy = batch_dir / 'human_feedback.db'
    with sqlite3.connect(legacy) as conn:
        conn.execute('CREATE TABLE decisions(task_id TEXT PRIMARY KEY, payload_json TEXT)')
        conn.execute('INSERT INTO decisions VALUES (?, ?)', ('task-01', json.dumps({'task_id': 'task-01', 'decision': 'approved'})))
    before = legacy.read_bytes()
    store = BatchReviewStore(batch_dir)
    assert len(store.feedback()['legacy_decisions_read_only']) == 1
    store.record_decision(_body())
    assert legacy.read_bytes() == before


def test_frozen_three_candidates_each_counted_and_bound_by_hash(batch):
    batch_dir, _ = batch
    mapping_path = batch_dir / 'review_mapping.json'
    mapping = json.loads(mapping_path.read_text())
    # 测试中使用新的映射副本，避免修改夹具冻结文件。
    store = BatchReviewStore(batch_dir)
    item = dict(store.by_task['task-01'])
    candidates = [{'candidate_id': f'x{i}', 'rendered_title': f'标题{i}', 'slot_id': f's{i}'} for i in range(3)]
    item['candidates'] = [{'candidate_id': c['candidate_id'], 'title_sha256': _hash(c['rendered_title'])} for c in candidates]
    store.mapping = [item]
    store.by_task = {'task-01': item}
    original_source = store._source
    def source(task_id):
        row, state = original_source(task_id)
        state['delivery_candidates'] = candidates + [{'candidate_id': 'extra', 'rendered_title': '未冻结'}]
        return row, state
    store._source = source
    assert len(store.get_task('task-01')['state']['candidates']) == 3
    for c in candidates:
        store.record_decision(_body(candidate_id=c['candidate_id']))
    assert store.list_tasks()[0]['review_status'] == 'done'
    assert all(c['decision'] for c in store.get_task('task-01')['state']['candidates'])
    store.record_decision(_body(candidate_id='x0', decision='reopen'))
    assert len(store.feedback()['decisions']) == 2


def test_page_three_candidate_flow_and_legacy_feedback_offline():
    """在 Node 的隔离 DOM 替身中执行页面脚本，不访问服务或真实数据。"""
    import shutil
    import subprocess
    from agent_system.batch_review_page import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 执行离线页面交互测试")
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    # 初始化会访问网络；测试独立驱动同一份页面处理函数。
    script = script[:script.rfind("(async()=>")]
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const page = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const elements = {};
function element(id) {
  return elements[id] ||= {value:'', textContent:'', innerHTML:'', className:'', disabled:false,
    reset(){}, focus(){}, close(){}, showModal(){this.open=true}};
}
const tasks = [
 {task_id:'a', blind_id:'R1', category:'测试', title:'标题', review_status:'unreviewed', candidate_count:3, decision:null},
 {task_id:'b', blind_id:'R2', category:'测试', title:'标题2', review_status:'unreviewed', candidate_count:1, decision:null},
 {task_id:'c', blind_id:'R3', category:'测试', title:'无标题', review_status:'no_candidates', candidate_count:0}
];
let count = 0, opened = [];
const candidates = [0,1,2].map(i=>({candidate_id:'c'+i, title_hash:'h'+i, rendered_title:'标题'+i, decision:null}));
const detail = {task_id:'a', video_id:'v', blind_id:'R1', category:'测试', state:{video:{}, candidates,
 delivery_slots:{missing:['core_hook','<unsafe>']}}};
const context = {document:{getElementById:element, querySelectorAll:()=>[]}, URLSearchParams,
 location:{search:''}, confirm:()=>true, console,
 fetch:async (url, options={})=>({ok:true,json:async()=>{
   if(url==='/api/tasks') return {tasks};
   if(url.startsWith('/api/task?')) {const id=new URL('http://localhost'+url).searchParams.get('task_id'); opened.push(id); return {task:id==='a'?detail:{...detail,task_id:id}};}
   if(url==='/api/decision') {count++; candidates[count-1].decision={decision:'approved'}; tasks[0].decision=candidates[0].decision; if(count===3)tasks[0].review_status='done'; return {};}
   if(url==='/api/feedback') return {decisions:[],legacy_decisions_read_only:[{task_id:'old',decision:'rejected',original_headline:'旧标题',reason:'旧意见'}]};
   throw Error(url);
 }})};
vm.createContext(context);
vm.runInContext(page,context);
(async()=>{
 await vm.runInContext("refresh(); openTask('a')",context);
 assert.match(element('progress').textContent,/0\/2 个有候选视频/);
 assert.match(element('content').innerHTML,/未交付槽位：核心钩子、&lt;unsafe&gt;/);
 for(let i=0;i<3;i++) {
   vm.runInContext(`openDecision(${i},'approved')`,context);
   await element('decision-form').onsubmit({preventDefault(){}});
   if(i<2) {assert.equal(opened.at(-1),'a'); assert.match(element('taskList').innerHTML,/待评/);}
 }
 assert.equal(opened.at(-1),'b');
 assert.match(element('progress').textContent,/1\/2 个有候选视频/);
 await element('feedbackButton').onclick();
 assert.match(element('feedback-content').textContent,/旧人工意见（只读，不计入当前进度）/);
 assert.match(element('feedback-content').textContent,/旧标题/);
 assert.match(element('feedback-content').textContent,/旧意见/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps(script), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_highlight_is_an_extra_flag_on_approved_not_a_fourth_verdict(batch):
    """精彩是「可用」之上的附加标记：判定域不变，只多一个布尔字段。"""
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    saved = store.record_decision(_body(highlight=True))
    assert saved["decision"]["decision"] == "approved"
    assert saved["decision"]["highlight"] is True
    assert store.feedback()["decisions"][0]["highlight"] is True
    with pytest.raises(ValueError):
        store.record_decision(_body(decision="highlight"))


def test_highlight_is_dropped_when_verdict_is_not_approved(batch):
    """「不可用但精彩」是自相矛盾的入库来源，必须在写入时就挡掉。"""
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    saved = store.record_decision(_body(task_id="task-03", candidate_id="c-03",
                                       decision="rejected", reason="没写到核心看点",
                                       highlight=True))
    assert saved["decision"]["decision"] == "rejected"
    assert saved["decision"]["highlight"] is False


def test_highlight_can_be_cleared_while_keeping_the_verdict(batch):
    """取消精彩不能顺手把「可用」也撤掉，否则一次误点要重评。"""
    batch_dir, _ = batch
    store = BatchReviewStore(batch_dir)
    store.record_decision(_body(highlight=True))
    cleared = store.record_decision(_body(highlight=False))
    assert cleared["decision"]["decision"] == "approved"
    assert cleared["decision"]["highlight"] is False
    assert len(store.feedback()["decisions"]) == 1


def test_page_highlight_flow_locks_verdict_and_sends_the_flag():
    """点「精彩」时判定锁为可用、提交带 highlight；取消时判定放开、flag 为 false。"""
    import json as _json
    import shutil
    import subprocess
    from agent_system.batch_review_page import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 执行离线页面交互测试")
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    script = script[:script.rfind("(async()=>")]
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const page = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const elements = {};
function element(id) {
  return elements[id] ||= {value:'', textContent:'', innerHTML:'', className:'', disabled:false,
    placeholder:'', reset(){}, focus(){}, close(){}, showModal(){this.open=true}};
}
const tasks = [{task_id:'a', blind_id:'R1', category:'测试', title:'标题',
  review_status:'unreviewed', candidate_count:1, decision:null}];
const candidates = [{candidate_id:'c0', title_hash:'h0', rendered_title:'标题0', decision:null}];
const detail = {task_id:'a', video_id:'v', blind_id:'R1', category:'测试',
  state:{video:{}, candidates, delivery_slots:{}}};
const sent = [];
const context = {document:{getElementById:element, querySelectorAll:()=>[]}, URLSearchParams,
  location:{search:''}, confirm:()=>true, console,
  fetch:async (url, options={})=>({ok:true, json:async()=>{
    if(url==='/api/tasks') return {tasks};
    if(url.startsWith('/api/task?')) return {task:detail};
    if(url==='/api/decision') {sent.push(JSON.parse(options.body)); return {};}
    throw Error(url);
  }})};
vm.createContext(context);
vm.runInContext(page, context);
(async()=>{
  await vm.runInContext("refresh(); openTask('a')", context);
  vm.runInContext("openDecision(0,'approved',true)", context);
  assert.equal(element('verdict').value, 'approved');
  assert.equal(element('verdict').disabled, true);
  assert.match(element('dialog-title').textContent, /精彩/);
  assert.match(element('reason').placeholder, /案例库/);
  await element('decision-form').onsubmit({preventDefault(){}});
  assert.equal(sent[0].highlight, true);
  assert.equal(sent[0].decision, 'approved');

  vm.runInContext("openDecision(0,'approved',false)", context);
  assert.equal(element('verdict').disabled, false);
  assert.equal(element('dialog-title').textContent, '人工意见');
  await element('decision-form').onsubmit({preventDefault(){}});
  assert.equal(sent[1].highlight, false);
  assert.equal(sent[1].decision, 'approved');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", harness], input=_json.dumps(script), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


# --- 诊断口径：冻结映射之外的候选可以评，但不得计入冻结进度与冻结指标 ---

DIAG_FROZEN_TITLE = "冻结：十个闹钟没叫醒早八人"


@pytest.fixture
def diagnostic_batch(tmp_path):
    """1 条冻结候选 + 状态里 2 条诊断候选 + 专属表 1 条诊断候选 + 全局表诱饵。"""
    batch_dir = tmp_path / "diagnostic-batch"
    batch_dir.mkdir()
    frozen = {"candidate_id": "frozen-1", "rendered_title": DIAG_FROZEN_TITLE, "slot_id": "core_summary"}
    state_alt = {"candidate_id": "alt-1", "rendered_title": "状态里的另一条草稿", "slot_id": "core_hook"}
    blocked = {"candidate_id": "blocked-1", "rendered_title": "超字数的草稿", "slot_id": "core_summary",
               "mechanical_checks": {"errors": [{"code": "G4_LENGTH_EXCEEDED"}]}}
    table_candidate = {"candidate_id": "hist-1", "rendered_title": "专属表里的历史草稿", "slot_id": "source_rewrite"}
    state = {"video": {}, "candidates": [frozen, state_alt, blocked],
             "pending_review_candidates": [], "route": {}}
    with sqlite3.connect(batch_dir / "agent.db") as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, "
                     "config_json TEXT, state_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE candidates_by_task(task_id TEXT, candidate_id TEXT, payload_json TEXT)")
        # 全局 candidates 表跨任务，不得作为回落来源（潜在碰撞诱饵）。
        conn.execute("CREATE TABLE candidates(candidate_id TEXT, payload_json TEXT)")
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (
            "t1", "v1", "pending_human", "{}", json.dumps(state, ensure_ascii=False), "2026-01-01", "2026-01-01"))
        conn.execute("INSERT INTO candidates_by_task VALUES(?,?,?)",
                     ("t1", table_candidate["candidate_id"], json.dumps(table_candidate, ensure_ascii=False)))
        conn.execute("INSERT INTO candidates_by_task VALUES(?,?,?)",
                     ("other-task", "foreign-1", json.dumps({"candidate_id": "foreign-1", "rendered_title": "别的任务的候选"},
                                                            ensure_ascii=False)))
        conn.execute("INSERT INTO candidates VALUES(?,?)",
                     ("global-1", json.dumps({"candidate_id": "global-1", "rendered_title": "全局表候选"}, ensure_ascii=False)))
    mapping = [{"task_id": "t1", "video_id": "v1", "blind_id": "R01", "category": "测试",
                "task_status": "pending_human",
                "candidates": [{"candidate_id": "frozen-1", "headline": DIAG_FROZEN_TITLE,
                                "title_sha256": _hash(DIAG_FROZEN_TITLE)}]}]
    (batch_dir / "review_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return batch_dir


def test_length_excluded_frozen_and_diagnostic_are_read_only(diagnostic_batch):
    store = BatchReviewStore(diagnostic_batch)
    item = store.by_task['t1']
    item['candidates'].append({'candidate_id': 'blocked-1', 'title_sha256': _hash('超字数的草稿')})
    historical = {'batch': store.batch_dir.name, 'task_id': 't1', 'candidate_id': 'blocked-1',
                  'title_hash': _hash('超字数的草稿'), 'original_headline': '超字数的草稿',
                  'decision': 'rejected', 'reason': '旧意见', 'scope': 'frozen'}
    with store._feedback_conn() as conn:
        conn.execute('INSERT INTO candidate_decisions_v2 VALUES(?,?,?,?,?)',
                     (store.batch_dir.name, 't1', 'blocked-1', historical['title_hash'], json.dumps(historical)))
    task = store.get_task('t1')
    assert [c['candidate_id'] for c in task['state']['candidates']] == ['frozen-1']
    assert task['length_excluded_candidates'][0]['decision']['reason'] == '旧意见'
    assert store.list_tasks()[0]['candidate_count'] == 1
    assert store.feedback()['reviewed'] == 0
    assert store.feedback()['decisions'][0]['reason'] == '旧意见'
    for action in ('approved', 'marginal', 'rejected', 'reopen'):
        with pytest.raises(ValueError, match='超长候选'):
            store.record_decision(_body(task_id='t1', candidate_id='blocked-1', decision=action,
                                        reason='意见', final_headline='修改后的短标题', scope='diagnostic'))
    store.record_decision(_body(task_id='t1', candidate_id='frozen-1'))
    assert store.list_tasks()[0]['review_status'] == 'done'
    # 原冻结映射不变；将超长候选视作诊断时也必须拒绝。
    item['candidates'].pop()
    with pytest.raises(ValueError, match='超长候选'):
        store.record_decision(_body(task_id='t1', candidate_id='blocked-1', decision='rejected', reason='意见'))


def test_frozen_decision_payload_carries_frozen_scope(diagnostic_batch):
    store = BatchReviewStore(diagnostic_batch)
    saved = store.record_decision(_body(task_id="t1", candidate_id="frozen-1"))
    assert saved["decision"]["scope"] == "frozen"
    assert saved["decision"]["original_headline"] == DIAG_FROZEN_TITLE
    assert store.feedback()["decisions"][0]["scope"] == "frozen"


def test_diagnostic_decision_is_saved_with_diagnostic_scope(diagnostic_batch):
    """状态候选与专属表候选都能评；身份与哈希一律取服务端真实标题。"""
    store = BatchReviewStore(diagnostic_batch)
    for cid, title in (("alt-1", "状态里的另一条草稿"),
                       ("hist-1", "专属表里的历史草稿")):
        saved = store.record_decision(_body(task_id="t1", candidate_id=cid,
                                            decision="rejected", reason="没写到核心看点"))
        assert saved["decision"]["scope"] == "diagnostic"
        assert saved["decision"]["original_headline"] == title
        assert saved["decision"]["title_hash"] == _hash(title)
    fb = store.feedback()
    assert [d["candidate_id"] for d in fb["diagnostic_decisions"]] == ["alt-1", "hist-1"]
    assert fb["decisions"] == []          # 冻结口径仍为空
    assert fb["reviewed"] == 0            # 默认 reviewed 不计诊断
    assert fb["reviewed_frozen"] == 0 and fb["reviewed_diagnostic"] == 2
    assert len(store.feedback(scope="diagnostic")["decisions"]) == 2
    detail = store.get_task("t1")
    assert [(c["candidate_id"], c["decision"]["scope"]) for c in detail["diagnostic_candidates"]] == [
        ("alt-1", "diagnostic"), ("hist-1", "diagnostic")]
    assert [c['candidate_id'] for c in detail['length_excluded_candidates']] == ['blocked-1']
    assert detail["decision"] is None
    # 全局 candidates 表不是本任务候选来源，不能据此造身份。
    with pytest.raises(ValueError):
        store.record_decision(_body(task_id="t1", candidate_id="global-1"))


def test_diagnostic_decisions_do_not_move_frozen_progress(diagnostic_batch):
    store = BatchReviewStore(diagnostic_batch)
    store.record_decision(_body(task_id="t1", candidate_id="alt-1", decision="marginal", reason="勉强可用"))
    store.record_decision(_body(task_id="t1", candidate_id="hist-1"))
    task = store.list_tasks()[0]
    assert task["review_status"] == "unreviewed"
    assert task["candidate_count"] == 1 and task["diagnostic_count"] == 2
    assert task['length_excluded_count'] == 1
    assert task["diagnostic_reviewed"] == 2
    store.record_decision(_body(task_id="t1", candidate_id="frozen-1"))
    task = store.list_tasks()[0]
    assert task["review_status"] == "done"
    fb = store.feedback()
    assert fb["reviewed"] == 1 and fb["reviewed_frozen"] == 1 and fb["reviewed_diagnostic"] == 2
    # 撤回诊断判定不影响已完成的冻结进度。
    store.record_decision(_body(task_id="t1", candidate_id="alt-1", decision="reopen"))
    assert store.list_tasks()[0]["review_status"] == "done"
    assert store.feedback()["reviewed_diagnostic"] == 1


def test_forged_hash_cannot_establish_identity(diagnostic_batch):
    """客户端哈希不能替不存在的候选造身份，也不能改变入库哈希与标题。"""
    store = BatchReviewStore(diagnostic_batch)
    forged = _hash("伪造的标题")
    with pytest.raises(ValueError):
        store.record_decision(_body(task_id="t1", candidate_id="invented", title_hash=forged))
    assert store.feedback()["decisions"] == [] and store.feedback()["diagnostic_decisions"] == []
    with pytest.raises(ValueError, match='哈希不一致'):
        store.record_decision(_body(task_id="t1", candidate_id="alt-1", title_hash=forged))
    saved = store.record_decision(_body(task_id="t1", candidate_id="alt-1", title_hash=_hash("状态里的另一条草稿")))
    assert saved["decision"]["scope"] == "diagnostic"
    assert saved["decision"]["title_hash"] == _hash("状态里的另一条草稿")
    assert saved["decision"]["original_headline"] == "状态里的另一条草稿"
    with pytest.raises(ValueError):
        store.record_decision(_body(task_id="t1", candidate_id="foreign-1"))
    assert [d["candidate_id"] for d in store.feedback()["diagnostic_decisions"]] == ["alt-1"]


def test_wrong_candidate_id_and_unknown_scope_are_rejected(diagnostic_batch):
    store = BatchReviewStore(diagnostic_batch)
    for cid in ("", "frozen-2", "hist-2"):
        with pytest.raises(ValueError):
            store.record_decision(_body(task_id="t1", candidate_id=cid))
    with pytest.raises(ValueError):
        store.record_decision(_body(task_id="t1", candidate_id="alt-1", scope="other"))
    # 客户端声明 diagnostic 也不能把冻结候选降级成诊断口径。
    saved = store.record_decision(_body(task_id="t1", candidate_id="frozen-1", scope="diagnostic"))
    assert saved["decision"]["scope"] == "frozen"
    assert store.feedback()["diagnostic_decisions"] == []


def test_diagnostic_decision_never_becomes_a_highlight_exemplar(diagnostic_batch):
    """诊断候选不是合格交付：即使客户端传 highlight，也不能进案例库口径。"""
    store = BatchReviewStore(diagnostic_batch)
    saved = store.record_decision(_body(task_id="t1", candidate_id="hist-1", highlight=True))
    assert saved["decision"]["scope"] == "diagnostic"
    assert saved["decision"]["decision"] == "approved"
    assert saved["decision"]["highlight"] is False
    # 冻结候选的精彩标记行为不变。
    frozen = store.record_decision(_body(task_id="t1", candidate_id="frozen-1", highlight=True))
    assert frozen["decision"]["scope"] == "frozen" and frozen["decision"]["highlight"] is True


def test_page_diagnostic_block_is_submittable_and_marked_out_of_scope():
    """诊断区块给出三档按钮与意见输入，提交带 scope=diagnostic。"""
    import json as _json
    import shutil
    import subprocess
    from agent_system.batch_review_page import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node.js 执行离线页面交互测试")
    script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    script = script[:script.rfind("(async()=>")]
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const page = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const elements = {};
function element(id) {
  return elements[id] ||= {value:'', textContent:'', innerHTML:'', className:'', disabled:false,
    placeholder:'', reset(){}, focus(){}, close(){}, showModal(){this.open=true}};
}
const tasks = [{task_id:'a', blind_id:'R1', category:'测试', title:'标题', review_status:'unreviewed',
  candidate_count:1, diagnostic_count:2, diagnostic_reviewed:0, decision:null}];
const candidates = [{candidate_id:'c0', title_hash:'h0', rendered_title:'冻结标题', decision:null}];
const diagnostics = [
  {candidate_id:'d0', title_hash:'dh0', rendered_title:'诊断标题一', slot_id:'core_hook', status:'rewrite',
   blocking_reasons:[{code:'G1_MISSING_LIMIT', text:'缺限定'}], codes:[], decision:null},
  {candidate_id:'d1', title_hash:'dh1', rendered_title:'诊断标题二', slot_id:'', status:'unknown',
   blocking_reasons:[], codes:[], decision:null}
];
const detail = {task_id:'a', video_id:'v', blind_id:'R1', category:'测试',
  state:{video:{}, candidates, delivery_slots:{missing:['core_hook']}}, diagnostic_candidates: diagnostics,
  length_excluded_candidates:[{candidate_id:'blocked', rendered_title:'超长失败标题', status:'blocked',
    decision:{decision:'rejected',reason:'历史意见'}}]};
const sent = [];
const context = {document:{getElementById:element, querySelectorAll:()=>[]}, URLSearchParams,
  location:{search:''}, confirm:()=>true, console,
  fetch:async (url, options={})=>({ok:true, json:async()=>{
    if(url==='/api/tasks') return {tasks};
    if(url.startsWith('/api/task?')) return {task:detail};
    if(url==='/api/decision') {sent.push(JSON.parse(options.body)); return {};}
    if(url==='/api/feedback') return {decisions:[], diagnostic_decisions:[{task_id:'a', candidate_id:'d0', blind_id:'R1', decision:'rejected', original_headline:'诊断标题一', reason:'看不到看点', scope:'diagnostic'}], legacy_decisions_read_only:[]};
    throw Error(url);
  }})};
vm.createContext(context);
vm.runInContext(page, context);
(async()=>{
  await vm.runInContext("refresh(); openTask('a')", context);
  assert.match(element('content').innerHTML, /超长失败轨迹（只读）/);
  assert.match(element('content').innerHTML, /历史人工意见（只读）：不可用 历史意见/);
  assert.match(element('content').innerHTML, /未交付槽位：核心钩子/);
  assert.match(element('content').innerHTML, /诊断口径/);
  assert.match(element('content').innerHTML, /不计入冻结评审指标/);
  assert.ok(element('content').innerHTML.includes('data-dverdict="0|approved"'));
  assert.ok(element('content').innerHTML.includes('data-dverdict="1|rejected"'));
  vm.runInContext("openDecision(0,'rejected',false,'diagnostic')", context);
  assert.match(element('dialog-title').textContent, /诊断口径/);
  assert.match(element('dialog-scope').textContent, /不计入冻结评审指标/);
  element('reason').value = '看不到核心看点';
  await element('decision-form').onsubmit({preventDefault(){}});
  assert.equal(sent[0].candidate_id, 'd0');
  assert.equal(sent[0].decision, 'rejected');
  assert.equal(sent[0].scope, 'diagnostic');
  assert.equal(sent[0].highlight, false);
  assert.match(element('live').textContent, /诊断口径/);
  // 冻结候选仍走 frozen 口径，文案不变。
  vm.runInContext("openDecision(0,'approved',false,'frozen')", context);
  assert.equal(element('dialog-title').textContent, '人工意见');
  assert.equal(element('dialog-scope').textContent, '');
  await element('decision-form').onsubmit({preventDefault(){}});
  assert.equal(sent[1].candidate_id, 'c0');
  assert.equal(sent[1].scope, 'frozen');
  await element('feedbackButton').onclick();
  assert.match(element('feedback-content').textContent, /诊断口径意见（不计入冻结评审指标）/);
  assert.match(element('feedback-content').textContent, /诊断标题一/);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", harness], input=_json.dumps(script), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
