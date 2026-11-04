# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/pipeline.py"
doc = io.open(p, encoding="utf-8").read()
anchor = '            user["memory_cases"] = cases'
add = anchor + '''
            same_video = self.memory.search_for_video(video.video_id, self.config.prompt_pack_version)
            if same_video:
                user["same_video_cases"] = same_video
                state.setdefault("memory_trace", []).append({"query": video.video_id, "status": "same_video", "used_case_ids": [c.get("case_id") for c in same_video]})'''
if anchor in doc and "same_video_cases" not in doc:
    doc = doc.replace(anchor, add, 1); print("注入 ok")
else:
    print("注入 MISS")
io.open(p, "w", encoding="utf-8").write(doc)
