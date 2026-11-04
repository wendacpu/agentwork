# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/prompts.py"
doc = io.open(p, encoding="utf-8").read()

anchor = "输入里的 source_title 是原视频标题，quality=source，是本条视频唯一的一手文本。理解时先读它讲了什么，再判断材料是否支持；它可以作为改写基线，但不是事实依据，不得因为没有其它证据就默认它成立。标题与材料冲突时以材料为准，并写进 conflicts。"
add = anchor + "\n**数字/事实基准（source title 校准）**：标题里的数字、金额、数量是 source 级基准；ASR 里与标题不一致的数字（尤其是顾客/旁白的疑问、误转写，如「800块钱」「272」）是引述或噪声，不得抽成事实 claim；标题与 ASR 冲突时以标题为准，并把冲突写进 conflicts。抽取标题里的陈述时写直接事实（如「四碗面872元」），不要写成「视频标题称…」「标题称…」这种元描述。"
if anchor in doc and "数字/事实基准" not in doc:
    doc = doc.replace(anchor, add, 1); print("理解 Prompt 已加")
else:
    print("理解 MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("prompts written")

# common.md 加 U-B07d
p2 = "/Users/wanting/program/复杂 agent/rulepacks/common.md"
d2 = io.open(p2, encoding="utf-8").read()
a2 = "U-B07c 材料不足时可写范围收窄，不得放大"
if a2 in d2 and "U-B07d" not in d2:
    # 放到 U-B07c 段之后
    idx = d2.find(a2)
    end = d2.find("\n\n", idx)
    if end == -1: end = len(d2)
    add2 = "\n\nU-B07d 标题里的数字/金额/数量是 source 级基准。ASR 里与标题不一致的数字（顾客/旁白疑问、误转写）是引述或噪声，不得抽成事实 claim；标题与 ASR 冲突时以标题为准并记录 conflicts。抽取标题陈述要写直接事实，不写「视频标题称…」这种元描述。"
    d2 = d2[:end] + add2 + d2[end:]
    io.open(p2, "w", encoding="utf-8").write(d2); print("common.md U-B07d 已加")
else:
    print("rule MISS")
