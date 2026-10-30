# 五张表怎么用

**表格已经填完了，你现在不需要再改任何格子。** D02 / D05 / D08 按你的说明落地，其余按代码与真实数据填写。

下一步是判 15 条金标准（见文末）。

---

## 先搞清四个词

这四个词描述的是「系统对一条候选的结论」，不是「你要做什么」。

| 词 | 系统在说什么 | 后续动作 | 任务状态 |
|---|---|---|---|
| `handoff` | 这条我推荐，你点头就行 | 进人工队列，排第一 | `pending_human` |
| `needs_review` | 我不敢推荐（事实或政策没解），你来看 | 进人工队列，**不给推荐** | `pending_human` |
| `rewrite` | 这条还能修，我修一轮再评 | 回 N09 重写，≤2 轮 | 继续跑 |
| `reject` | 命中已批准的硬政策，直接毙 | 结束 | `stopped` |

**`needs_review` 不等于自动进入推荐链路。** 它的意思是：任务停在人工队列里，系统一条都不推荐，等人（H01）来判。和 `handoff` 的唯一区别是「系统敢不敢给推荐」。

## 一条候选怎么走到这四个词

```
程序机械校验（G4）
  ├─ 失败（长度/问号/引用非法）→ rewrite
  └─ 通过 ↓
独立评审（R01）出 G1–G3 + Q1–Q3
  ↓
程序裁决（Q01）
  ├─ G2 命中已批准政策规则      → reject
  ├─ 任一 Gate = needs_review   → needs_review
  ├─ 任一 revise 或 任一 Q < 2  → rewrite
  └─ 全部 pass 且 Q 全 ≥ 2      → handoff
```

## 整批候选怎么定

`_route_candidates` 的优先级是固定的，**先保守后修改**：

1. 只要有一条 `handoff` → 整批走 handoff（推荐 Q 分和最高的那条）
2. 没有 handoff，但有 `needs_review` → 整批 `needs_review`（哪怕另一条可修，也先交人）
3. 都没有，但有 `rewrite` → 整批进重写（**现在三条都会修，不是只修一条**）
4. 都没有 → `reject` / `stop`

第 2 条是刻意的：宁可交人，也不自动推一条「事实没解」的标题。

## 所以你要做的

不是填表，是判 15 条：

1. 打开 [gold.csv](gold.csv)
2. 对每条视频，看系统给的候选，写下 `可用 / 不可用 / 需改写`
3. 选「可用」就写你认为的最终标题；选「不可用」就填命中的判据 ID（[criteria.csv](criteria.csv) 里有 18 条，定义和反例都写好了）
4. 跑 `python scripts/eval.py`

判的时候你会自然发现两个口径问题：20 字够不够、哪些算「待人工」。那时候再改表，比现在拍脑袋准。

---

## 列字典（给你读，不用改）

`decision_register.csv`：

| 列 | 什么意思 |
|---|---|
| 决策者 | 模型 / 代码 / 人工。**机械校验必须写代码**，这是最容易错的地方 |
| 为什么不能用规则 | "这个决策为什么值得用模型"，被问到时靠这列 |
| 可选项 | 从哪几个里选；D05 已固定为 5 个策略枚举 |
| 判据与依据 | 凭什么选 |
| 必须留下的结果 | 决策要产出什么，供下游用 |
| 失败模式 | 它会怎么错 |
| 兜底 | 错了谁来救 |
| 指标 | 拿什么数它 |
| 现状 | 实现到哪一步 |

`criteria.csv`：

| 列 | 什么意思 |
|---|---|
| 定义（怎么判） | 具体看什么、比什么，两个人能判出同一结果 |
| 反例（长什么样） | 一条典型的不合格标题 |
| 归属 Gate | G1 事实 / G2 政策 / G3 交付 / G4 机械 / Q 质量 |
| 判定者 | 程序判还是模型判。程序能判的别交给模型 |
| 判定结果 | 不可用 / 待人工 / 排序靠后——**这是口径，不是代码开关** |
| 回退节点 | 这个问题该回 N05 / N07 / N09 哪一处修 |

> 注意：`判定结果` 这一列是给人看的统一口径，代码不读它。代码读的是 Gate 状态和 Q 分数。它的作用是让你判金标准时和评审用同一套标准，一致率才有意义。

## 已经落地的（不用你再管）

| 决策 | 落地 |
|---|---|
| D02 四档 | `verification_required` + 全暂缓时停止并产出 `verification_tasks` |
| D02 禁止推断 | 进 N09 的 `source_requirements` + `blocked_claims` |
| D05 五策略 | `rubric.STRATEGY_ENUM`，不符记 `Q2_STRATEGY_MISMATCH` |
| D05 限定表达 | `Candidate.qualifiers_used` |
| D08 回退节点 | `ReviewIssue.rollback_to`，缺省按维度推 N05/N07/N09 |
| D08 映射 | 事实→G1、业务→G2、匹配度→G3、语言→Q1、完整性→Q2、精彩度→Q3 |
| D03 淘汰原因 | `state.highlight_selection.eliminated` |
| 重写有效性 | `addressed_issue_ids` + `rewrite_outcomes`（resolved / repeated） |
| 批量重写 | 三条候选都修，不再只修 Q 分最高的一条 |
| 案例检索 | 中文 2-gram + scope 集合比较 + issue_code 加权 |
| 人工判定→案例 | 驳回带建议落 draft，采用且终稿不同落 confirmed |
| 业务政策 | [config.business-policy.example.json](../config.business-policy.example.json)：P1 低俗 / P2 暴力猎奇 / P3 营销导流 |
