#!/usr/bin/env python3
"""离线统计：N07 生成侧主旨契约与 R00 独立复核契约的分歧率。

背景（2026-11-01 canary，BV1PwsR6oEKW）：同一条视频，两个独立读材料的节点给出
两个不同的故事——

  N07：女生夜里给男生送吃的、叮嘱他好好休息，男生体贴回应
  R00：女生因没定闹钟错过约定、心怀愧疚，男生以体贴态度回应

误差从理解层注入，下游全部忠实继承；而程序侧**没有任何跨契约一致性检查**，
`validate_highlight_alignment` 只做「看点 vs 单份契约」的对齐。`review_thesis_contract`
确实传给了 R01，提示词也要求「不一致就标 needs_review」，但 R01 没有执行。
结果：两套契约讲两个故事，一致性检查报零错误，错的那条被推荐给人工。

本脚本只读、只统计，不调用模型、不改路由。**触发判据直接复用
`agent_system.thesis.contracts_disagree`**（管线的唯一事实源），避免两处口径漂移；
下面只是在该判据之外补充用于人工阅读的形态线索。

**实测（2026-11-01，51 任务 / 11 视频，阈值 0.35）**：触发 14 任务（27.5%）/ 6 视频（55%），
其中 6 条当时已 handoff。**目标与误报在数值上重叠到无法用阈值分开**——真分歧那次
overlap=0.333，人读定性为详略的那次也是 overlap=0.333（promise 0.154 vs 0.117）。
词法判别分不清「详略差异」和「真分歧」：

  BV19AHd6PEAP ×3  N07 写「从功能到配饰」，R00 写「产品从功能走向配饰化」——
                   同一个意思，字符串只共享「配饰」两字，仍然会被判成触发。
  BV1u9HA6JEdw ×2  N07 写「红色裙子」，R00 写「酒红长裙」——同上（这一例在收紧为
                   「两个指标都低」后不再触发，但那是巧合，不是判别力）。
  BV1PwsR6oEKW     才是真分歧（送吃的 vs 睡过头），而它在词法上与上面那些长得一样。

所以这三类**只有读语义才能分开**（同义改写 / 一侧更详尽 / 互斥读法）。把这个判据
当**拦截条件**用会得到约一半误报（6 个视频里 3 个是详略差异）；当**摘推荐章**用则误报
几乎无代价——所以管线只在 handoff 时降级为待核，不阻断交付。

分两档报告：

  A 硬冲突：同一动作在两侧被判给不同主体（性别/角色）。规则可判但极窄——对
    真实故障（BV1PwsR6oEKW）就是 0 命中，因为两侧的动作词根本不同（送 vs 睡过头）。
  B 触发：`contracts_disagree` 判为分歧。含大量详略差异，须人看才能定性。

用法：
  python3 scripts/contract_disagreement_scan.py                # 扫 runtime/**
  python3 scripts/contract_disagreement_scan.py --json out.json
  python3 scripts/contract_disagreement_scan.py --verbose      # 打印每条的分歧证据
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_system.thesis import contracts_disagree  # noqa: E402  与管线同源，避免判据漂移

# 主体词：只保留能把动作归到某个具体角色的词。他/她单独出现时指代不明，仅在
# 同句另有明确角色词时才用于消歧，否则跳过，避免把「他们」之类算成男性。
ROLE_TOKENS = {
    "男生": "男", "男友": "男", "男方": "男", "男朋友": "男", "老公": "男", "丈夫": "男",
    "女生": "女", "女友": "女", "女方": "女", "女朋友": "女", "老婆": "女", "妻子": "女",
}
SUBJECT_ONLY = {"男": "男", "女": "女", "男性": "男", "女性": "女"}

# 动作词：只收能把「谁做了什么」区分开的动词。太泛的词（是/有/在）不进，
# 否则会把「女生很体贴」和「男生很体贴」判成冲突。
ACTION_VERBS = (
    "送", "带", "买", "做饭", "叮嘱", "提醒", "关心", "照顾", "安慰", "回应", "答复",
    "道歉", "解释", "表白", "拒绝", "买单", "结账", "接", "等", "追", "夸", "抱怨",
    "邀请", "问", "答", "帮", "教", "陪", "迟到", "睡过头", "错过", "自责", "原谅",
    "生气", "哭", "笑", "煮", "端", "递", "收下", "留下",
)

CLAUSE_SPLIT = "，。；、！？,;!? \n（）()「」“”\"'"

# 必保语义两两字符二元组 Jaccard 低于此值即算「几乎不相交」。0.2 是宽松线：
# 宁可在 B 档多报，也不要把真分歧漏进「看起来一致」。
LOW_OVERLAP = 0.2

# 「这条必保语义被对面某条覆盖了吗」用包含率判定：本条的二元组有多大比例出现在对面。
# 0.7 允许同义换词（酒红/红色、送/带），又不至于把只共享一个词的两句算成覆盖。
COVERAGE_RATIO = 0.7


def _clauses(text: str) -> list[str]:
    out, cur = [], []
    for ch in str(text or ""):
        if ch in CLAUSE_SPLIT:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _roles_in(clause: str) -> set[str]:
    roles = set()
    for token, role in ROLE_TOKENS.items():
        if token in clause:
            roles.add(role)
    return roles


def verb_actor_map(contract: dict) -> dict[str, set[str]]:
    """从契约文本抽出 {动作: {可能的主体角色}}。

    只在「一个小句里只出现一种角色」时归属，出现两种角色就跳过——那种句子
    本来就要靠语序消歧，规则判不准，硬判只会制造误报。
    """
    text_parts = [str(contract.get("core_promise") or "")]
    text_parts += [str(x) for x in (contract.get("required_semantics") or [])]
    out: dict[str, set[str]] = {}
    for part in text_parts:
        for clause in _clauses(part):
            roles = _roles_in(clause)
            if len(roles) != 1:
                continue
            role = next(iter(roles))
            for verb in ACTION_VERBS:
                if verb in clause:
                    out.setdefault(verb, set()).add(role)
    return out


def _bigrams(text: str) -> set[str]:
    # 只留中日韩文字与字母数字：斜杠、顿号、引号、括号都不该进入二元组，
    # 否则「小包装/小样」会生成「装/」「/小」这类噪声，把包含率压低到覆盖阈值以下。
    clean = "".join(ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))} or ({clean} if clean else set())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def semantics_overlap(left: dict, right: dict) -> float:
    """两侧必保语义的最大两两相似度：任一条对得上就算这条契约还在讲同一件事。"""
    left_items = [str(x) for x in (left.get("required_semantics") or []) if str(x).strip()]
    right_items = [str(x) for x in (right.get("required_semantics") or []) if str(x).strip()]
    if not left_items or not right_items:
        return 0.0
    return max(_jaccard(_bigrams(a), _bigrams(b)) for a in left_items for b in right_items)


def coverage(left: dict, right: dict) -> tuple[list[str], list[str]]:
    """两侧各自「对方没覆盖到的必保语义」。

    这是比相似度更有判别力的指标：如果一侧的必保语义全被另一侧覆盖、
    只有另一侧多出内容，那这不是分歧而是**详略差异**（R00 读得更全），
    处置应是让 N07 补齐，而不是拦住交付。只有两侧都有对方覆盖不到的条目时，
    才可能是互斥的两种读法，那才需要人来断。
    """
    def uncovered(src: dict, dst: dict) -> list[str]:
        dst_items = [_bigrams(x) for x in (dst.get("required_semantics") or []) if str(x).strip()]
        out = []
        for item in (src.get("required_semantics") or []):
            text = str(item).strip()
            if not text:
                continue
            grams = _bigrams(text)
            # 用包含率而非 Jaccard：一侧写「小包装/小样」、另一侧写
            # 「小包装、小样、mini美妆等「小」形态商品成为消费热点」，后者包含前者，
            # 但 Jaccard 会被长句稀释到 0.2 以下，误判成「对方没覆盖」。
            best = max((len(grams & d) / len(grams) for d in dst_items), default=0.0)
            if best < COVERAGE_RATIO:
                out.append(text)
        return out
    return uncovered(left, right), uncovered(right, left)


def compare(left: dict, right: dict, evidence_by_id: dict | None = None) -> dict:
    left_verbs, right_verbs = verb_actor_map(left), verb_actor_map(right)
    conflicts = []
    for verb in sorted(set(left_verbs) & set(right_verbs)):
        shared = left_verbs[verb] & right_verbs[verb]
        if shared:
            continue
        conflicts.append({
            "verb": verb,
            "n07_actor": sorted(left_verbs[verb]),
            "r00_actor": sorted(right_verbs[verb]),
        })
    only_n07, only_r00 = coverage(left, right)
    overlap = semantics_overlap(left, right)
    promise_sim = _jaccard(_bigrams(left.get("core_promise")), _bigrams(right.get("core_promise")))
    # 触发与否以管线实际判据为准：词法低重叠只是线索，不是判定本身。
    triggered = bool(contracts_disagree(left, right, evidence_by_id))
    # 一侧是另一侧的子集 → 详略差异；两侧都有独有内容 → 可能是互斥读法。
    if conflicts:
        shape = "contradiction"
    elif only_n07 and only_r00:
        shape = "both_have_unique"
    elif only_r00:
        shape = "r00_superset"
    elif only_n07:
        shape = "n07_superset"
    else:
        shape = "aligned"
    return {
        "hard_conflicts": conflicts,
        "shape": shape,
        "only_n07": only_n07,
        "only_r00": only_r00,
        "semantics_overlap": round(overlap, 3),
        "promise_similarity": round(promise_sim, 3),
        "low_overlap": overlap < LOW_OVERLAP,
        "tier": "A" if conflicts else ("B" if triggered else "ok"),
    }


def _tasks(root: Path):
    """按 task_id 去重：同一份库会被复制进评审批次目录，重复计数会虚高。"""
    seen: dict[str, dict] = {}
    for path in sorted(glob.glob(str(root / "**" / "agent.db"), recursive=True)):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT task_id, video_id, status, state_json FROM tasks").fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            task_id = str(row["task_id"] or "")
            if not task_id or task_id in seen:
                continue
            try:
                state = json.loads(row["state_json"] or "{}")
            except json.JSONDecodeError:
                continue
            left, right = state.get("thesis_contract"), state.get("review_thesis_contract")
            if not (isinstance(left, dict) and isinstance(right, dict) and left and right):
                continue
            seen[task_id] = {"task_id": task_id, "video_id": row["video_id"], "status": row["status"], "db": str(path), "state": state}
        conn.close()
    return list(seen.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="runtime")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    rows = _tasks(Path(args.root))
    if not rows:
        print("没有找到同时具备两套契约的任务。")
        return 0

    results = []
    for row in rows:
        evidence_by_id = {str(e.get("evidence_id")): e for e in ((row["state"].get("video") or {}).get("evidence") or []) if isinstance(e, dict) and e.get("evidence_id")}
        verdict = compare(row["state"]["thesis_contract"], row["state"]["review_thesis_contract"], evidence_by_id)
        verdict.update({k: row[k] for k in ("task_id", "video_id", "status", "db")})
        verdict["action"] = ((row["state"].get("route") or {}) or {}).get("action")
        results.append(verdict)

    tier_a = [r for r in results if r["tier"] == "A"]
    tier_b = [r for r in results if r["tier"] == "B"]
    ok = [r for r in results if r["tier"] == "ok"]
    total = len(results)

    videos = {r["video_id"] for r in results}
    flagged_videos = {r["video_id"] for r in tier_a + tier_b}
    print(f"扫描 {total} 条同时具备两套契约的任务，覆盖 {len(videos)} 个视频（按 task_id 去重，仅统计，未调用模型）\n")
    print(f"  A 硬冲突（同一动作归给不同主体）  {len(tier_a):3d}  {len(tier_a)/total:6.1%}   视频 {len({r['video_id'] for r in tier_a})}")
    print(f"  B 低重叠（须人看才能定性）        {len(tier_b):3d}  {len(tier_b)/total:6.1%}   视频 {len({r['video_id'] for r in tier_b})}")
    print(f"  一致                              {len(ok):3d}  {len(ok)/total:6.1%}   视频 {len(videos - flagged_videos)}")
    print(f"\n  涉及分歧的视频合计 {len(flagged_videos)}/{len(videos)}（{len(flagged_videos)/max(1,len(videos)):.0%}）")
    delivered = [r for r in tier_a + tier_b if (r.get("action") == "handoff")]
    print(f"  其中已把标题交付出去的（action=handoff） {len(delivered)} 条")

    if tier_a:
        print("\nA 档证据：")
        for r in tier_a:
            print(f"  {r['video_id']} ({r['status']}) {r['task_id']}")
            for c in r["hard_conflicts"]:
                print(f"      「{c['verb']}」N07→{','.join(c['n07_actor'])}  R00→{','.join(c['r00_actor'])}")
            print(f"      N07：{r['state']['thesis_contract'].get('core_promise')}")
            print(f"      R00：{r['state']['review_thesis_contract'].get('core_promise')}")

    if args.verbose and tier_b:
        print("\nB 档明细（低重叠不等于分歧，可能是同义改写）：")
        for r in tier_b:
            print(f"  {r['video_id']} 重叠={r['semantics_overlap']} promise相似={r['promise_similarity']}")
            print(f"      N07：{r['state']['thesis_contract'].get('core_promise')}")
            print(f"      R00：{r['state']['review_thesis_contract'].get('core_promise')}")

    if args.json_out:
        payload = [{k: v for k, v in r.items() if k != "state"} for r in results]
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
