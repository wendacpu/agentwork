# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/rulepacks/common.md"
doc = io.open(p, encoding="utf-8").read()

anchor = "U-S09 标题面向用户、不面向审核者：不得以「视频/画面/标题/封面/片子/原题」等载体为主语或归属对象（如「视频称」「画面却是」「标题说」「据视频所述」），也不得写「与…不符/不一致」这类校验结论。归属只给真实信源（人/机构/媒体/作者本人）；载体元话语属于评审层的话，不进入成品标题。"
add = anchor + "\n\nU-B08 标题必须是完整可读的句子：主语必须是能执行动作的主体（人/机构），地点/事物不能当「逛/吃/做/拍」的主语；不得缺谓语或宾语；名词堆砌、残句不得进入候选。前后分句必须有明确逻辑关系，不得硬凑因果。\n\nU-S10 个人账号的创作者在标题里用「博主」；「作者」只用于媒体/机构署名或文学创作。信源归属（据X称）与创作者称谓要分开。\n\nU-P01 AI 生成/虚构的剧情，前缀必须能让用户看出「虚拟/AI生成」，不能只标「剧情演绎」让用户以为是真人演绎；理解阶段遇到 AI 生成/虚拟剧情，attributes 必须同时包含「AI生成」。"
if anchor in doc and "U-B08" not in doc:
    doc = doc.replace(anchor, add, 1); print("common.md 规则已加")
else:
    print("MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
