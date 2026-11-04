# -*- coding: utf-8 -*-
import io

fb = '''"""人工判定 → 分层回流。

每个判定都归到一个设计层，后续由该层的检查/规则承接，而不是"一个 case 补一条规则"。
- extraction：事实/证据抽取出错（数字、主体、证据）
- constraint：关键背景/锚点/限制条件缺失
- selection：看点选错（边角料）
- expression：语法/用词/表达
- unknown：需要人工定位
"""
from __future__ import annotations

LAYER_BY_REASON: dict[str, tuple[str, str]] = {
    "fact": ("extraction", "G1 事实/证据：数字/金额/主体是否与源标题一致"),
    "context": ("constraint", "约束层：关键背景/地域/主体锚点是否保留"),
    "point": ("selection", "选择层：主看点是否为视频核心（主线测试）"),
    "grammar": ("expression", "表达层：句子是否完整（主谓宾）"),
    "readability": ("expression", "表达层：用词/缩写是否可读"),
}


def classify(raw_reason_type: str, problem_type: str, reason: str = "") -> tuple[str, str]:
    key = str(raw_reason_type or "").strip()
    if key in LAYER_BY_REASON:
        return LAYER_BY_REASON[key]
    if problem_type == "事实与信源":
        return ("extraction", "G1 事实/证据：数字/金额/主体是否与源标题一致")
    if problem_type == "表达":
        return ("expression", "表达层：语法/用词")
    return ("unknown", "需要人工定位到具体层")
'''
io.open("/Users/wanting/program/复杂 agent/agent_system/feedback.py", "w", encoding="utf-8").write(fb)
print("feedback.py ok")

p = "/Users/wanting/program/复杂 agent/agent_system/storage.py"
doc = io.open(p, encoding="utf-8").read()
anchor = "        CREATE TABLE IF NOT EXISTS labels(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, payload_json TEXT);"
add = anchor + "\n        CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, case_id TEXT, video_id TEXT, layer TEXT, problem_type TEXT, raw_reason_type TEXT, reason TEXT, check_hint TEXT, created_at TEXT);"
if anchor in doc and "CREATE TABLE IF NOT EXISTS feedback" not in doc:
    doc = doc.replace(anchor, add, 1); print("feedback 表 ok")
else:
    print("feedback 表 MISS")
method = '''
    def record_feedback(self, task_id: str, case_id: str, video_id: str, layer: str, problem_type: str, raw_reason_type: str, reason: str, check_hint: str) -> None:
        self.case_conn.execute(
            "INSERT INTO feedback(task_id, case_id, video_id, layer, problem_type, raw_reason_type, reason, check_hint, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, case_id, video_id, layer, problem_type, raw_reason_type, reason, check_hint, now()),
        )
        self.case_conn.commit()

'''
mAnchor = "    def _auto_case(self, task_id: str, state: dict[str, Any], decision: dict[str, Any], candidate: dict[str, Any]) -> str | None:"
if mAnchor in doc and "def record_feedback" not in doc:
    doc = doc.replace(mAnchor, method + mAnchor, 1); print("record_feedback ok")
else:
    print("record_feedback MISS")
io.open(p, "w", encoding="utf-8").write(doc)
print("written")
