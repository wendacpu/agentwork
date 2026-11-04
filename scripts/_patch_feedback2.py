# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/storage.py"
doc = io.open(p, encoding="utf-8").read()
anchor = "        self.case_conn.execute(\"INSERT OR REPLACE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,?,?)\", (case.case_id, json.dumps(payload, ensure_ascii=False), int(confirmed), rule_version))\n        return case.case_id"
add = '''        self.case_conn.execute("INSERT OR REPLACE INTO cases(case_id,payload,confirmed,rule_version) VALUES(?,?,?,?)", (case.case_id, json.dumps(payload, ensure_ascii=False), int(confirmed), rule_version))
        # 自动回流：把这次判定归到一个设计层，写进 feedback 表（供后续按层修）
        from .feedback import classify
        video_id = str((state.get("video") or {}).get("video_id") or "")
        layer, hint = classify(raw_reason, problem_type, str(decision.get("reason") or ""))
        self.record_feedback(task_id, case.case_id, video_id, layer, problem_type, raw_reason, str(decision.get("reason") or ""), hint)
        return case.case_id'''
if anchor in doc and "record_feedback(task_id" not in doc:
    doc = doc.replace(anchor, add, 1); print("回流调用 ok")
else:
    print("回流调用 MISS")
io.open(p, "w", encoding="utf-8").write(doc)
