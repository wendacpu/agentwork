"""人身攻击用语拦截（业务规则 P9）。

业务方 2026-11-02 新增：标题不得直接出现针对具体个人的人身攻击、贬损或羞辱用语。
现实触发点是 BV1u9HA6JEdw（黄多多红毯采访）：视频顶部黑条写着「网友：她真的很欠扁
样子」，几轮生成里有候选把「网友称她欠扁」直接写进了标题——这条评价本身是视频的
对照素材，但不等于可以把攻击词复制进标题替它传播。

**转述也算出现**：「网友称她欠扁」「网友评她欠扁」照样命中。转述结构不改变一件事：
读者在标题里看到的就是这个词。可改写路径是把攻击内容抽象化（网友恶评 / 网友差评 /
网友嘲讽 / 争议评价），而不是把它搬进标题。

为什么词表要克制：误伤会直接把一条本来能交付的标题推进重写轮，重写轮是有限次数的。
所以这里只收「几乎不存在正常义」的词；有正常义的（垃圾、废物、白莲花、恶心、有病、
猪头）一律不收，宁可交给模型按语义判断。

扩展方式：直接往 PERSONAL_ATTACK_TERMS 里加；新增词必须逐条自问「这个词有没有正常
用法」，有就别放进来。规则文本在 config.business-policy.json 的 P9。
"""

from __future__ import annotations

from typing import Iterable

# 只收无正常义的、明确指向个人的贬损词。按主题分组只是为了便于维护与后续扩充。
PERSONAL_ATTACK_TERMS: tuple[str, ...] = (
    # 直接辱骂
    "欠扁", "欠揍", "傻逼", "傻b", "煞笔", "sb", "脑残", "智障", "弱智", "脑瘫",
    "白痴", "蠢货", "蠢猪", "贱人", "贱货", "婊子", "泼妇", "悍妇", "疯婆子",
    "神经病", "不要脸",
    # 外貌与身材羞辱
    "丑八怪", "肥猪", "死胖子", "土包子", "乡巴佬", "穷鬼",
    # 人格标签（贬损性身份评价）
    "绿茶婊", "心机婊", "作精", "戏精", "舔狗", "妈宝", "巨婴", "loser",
    "渣男", "渣女", "油腻男", "油腻女",
)

# 命中位置前两个字是这些否定词时不判命中：「不欠扁」「没人说她欠扁」不是在用攻击词。
NEGATION_PREFIXES: tuple[str, ...] = ("不", "没", "别", "无", "未", "非")


def find_personal_attacks(title: str, extra_terms: Iterable[str] = ()) -> list[str]:
    """返回标题里命中的人身攻击用语，按词表顺序去重；没有命中返回空列表。"""
    text = str(title or "")
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for term in tuple(PERSONAL_ATTACK_TERMS) + tuple(extra_terms):
        needle = str(term or "").lower()
        if not needle or needle in hits:
            continue
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            start = index + 1
            # 只看紧邻的那个字：「不欠扁」跳过，「没人说她欠扁」不跳过（后者确实在说欠扁）。
            if index > 0 and text[index - 1] in NEGATION_PREFIXES:
                continue
            hits.append(term)
            break
    return hits


def attack_rewrite_hint(hits: list[str]) -> str:
    """给重写轮的改写指引：说清「换成什么」，而不只是「不许用什么」。"""
    quoted = "、".join(f"「{hit}」" for hit in hits)
    return (f"标题直接使用了人身攻击用语 {quoted}（业务规则 P9）。转述网友评价同样不行。"
            "请把攻击内容抽象化：写「网友恶评」「网友差评」「网友嘲讽」「争议评价」这类"
            "不复制攻击内容的表述，或删掉这条评价、只写受访内容。")
