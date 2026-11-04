# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/prompts.py"
doc = io.open(p, encoding="utf-8").read()
anchor = 'memory_cases 里 polarity=negative 的条目是反例（good_example 为空），表示"这类表达被判过勉强通过或驳回"，用来避开同类写法；不要从反例里抄事实或句式。'
add = anchor + ' same_video_cases 是**同一条视频**此前被判过的案例，属于硬约束：上次被判不可用/勉强通过的写法这次不得再出现；上次被判可用的结构可以复用。'
if anchor in doc and "same_video_cases" not in doc:
    doc = doc.replace(anchor, add, 1); print("prompt ok")
else:
    print("prompt MISS")
io.open(p, "w", encoding="utf-8").write(doc)
