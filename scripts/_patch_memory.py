# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/memory.py"
doc = io.open(p, encoding="utf-8").read()
anchor = "    def search(self, query: str, problem_types: list[str], rule_version: str, limit: int = 5, scope: list[str] | None = None) -> list[dict[str, Any]]:"
method = '''    def search_for_video(self, video_id: str, rule_version: str, limit: int = 5) -> list[dict[str, Any]]:
        # 同一条视频此前被判过的案例（按 source_task_id 关联 tasks.video_id）
        if not video_id:
            return []
        rows = self.conn.execute(
            "SELECT c.payload, t.video_id FROM cases c LEFT JOIN tasks t ON t.task_id = json_extract(c.payload, '$.source_task_id') "
            "WHERE c.confirmed=1 AND (c.rule_version=? OR c.rule_version='*')",
            (rule_version,),
        ).fetchall()
        out = []
        for payload, vid in rows:
            data = json.loads(payload)
            if vid == video_id or str(data.get("video_id") or "") == video_id:
                out.append(data)
        return out[:limit]

'''
if anchor in doc and "search_for_video" not in doc:
    doc = doc.replace(anchor, method + anchor, 1); print("ok")
else:
    print("MISS")
io.open(p, "w", encoding="utf-8").write(doc)
