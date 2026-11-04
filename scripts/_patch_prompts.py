# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/prompts.py"
doc = io.open(p, encoding="utf-8").read()

anchor = "归属只能给真实信源（人/机构/媒体/作者本人），不能给「视频/画面/标题/封面」。"
add = anchor + "用词与语法：个人博主的动作主体写「博主」，不写「作者」（「作者」只用于媒体/机构署名）；主语必须是能执行动作的主体，地点/事物不能当「逛/吃/做/拍」的主语；标题必须是完整可读的句子，不能是名词堆砌、残句或缺谓语/宾语；前后分句必须有明确逻辑关系，不能硬凑因果。"
if anchor in doc and "用词与语法" not in doc:
    doc = doc.replace(anchor, add, 1); print("GENERATION_PROMPT 已加")
else:
    print("GEN MISS")

anchor2 = "长度是硬约束：不得通过放宽字数、省略归属或删除限定来获取通过"
add2 = "用词与语法：个人博主的动作主体写「博主」，不写「作者」；主语必须是能执行动作的主体，地点/事物不能当动作主语；标题必须完整可读，不能是名词堆砌或缺谓语/宾语；前后分句必须有明确逻辑关系。" + anchor2
if anchor2 in doc and "用词与语法：个人博主的动作主体写「博主」，不写「作者」；" not in doc:
    doc = doc.replace(anchor2, add2, 1); print("REWRITE_PROMPT 已加")
else:
    print("REWRITE MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
