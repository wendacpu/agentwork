# BC07 全链路图鉴：节点 · 字段 · 本 case 的实际值

> **更正（后续复核）**：本文部分分析结论过度确定：三候选并非只有策略不同，不能据此排除上游影响；`semantic_units.meaning` 是模型概括，并非一手证据；标题不带问号不是绕过语义校验。原日志中 G3 两项均为 true，不能据此断言 issue 有效性导致本案放行。本文保留历史分析供追溯，修复效果仍需真实模型与人工评审验证。

> 配套 [bc07-log-fields-trace.md](bc07-log-fields-trace.md)（定位结论）与 [nodes.md](nodes.md)（编号来源）。
> 本文件的目标只有一个：**让你看到任何一条日志时，知道"这是哪个节点、这个字段是什么人话、这个值算正常还是异常"。**
> 所有值摘自 `runtime/bc07_zero_cost_baseline/original_case.json`，未补造。

---

## 0. 先记四段，再记编号

| 段 | 节点 | 干什么 | 一句话判读 |
|---|---|---|---|
| ① 读材料 | N00–N03V | 读视频、抽事实、可选外部核查 | 事实抽错 → 从这里错 |
| ② 划边界 | N04–N06 | 哪些能写、必须怎么写 | 把不该写的写成能写 → 从这里错 |
| ③ 写标题 | N07–N10 | 选看点、按槽位写、机械检查 | 主点选偏 / 表达跑偏 → 从这里错 |
| ④ 判好坏 | R00–R01–Q01–N12–H01 | 复核主旨、独立评审、程序裁决、交人 | 该拦没拦 → 从这里错 |

编号只在查日志时有用：`N05` = 边界判错了，`N07` = 看点选错了，`N09` = 写跑偏了，`R01` = 评审放水了，`Q01` = 路由判错了。

---

## 表 A · 节点字典（16 个，含本 case 结果）

| 编号 | 人话名 | 谁做 | 干什么 | 产出主要字段 | 本 case 结果 |
|---|---|---|---|---|---|
| **N00** | 受理任务 | 代码 | 建任务、冻配置、算哈希 | `run_manifest` `prompt_hashes` | ✅ 5 个提示词哈希锁定；模型 `deepseek-flash`，`reasoning_effort=off` |
| **N01** | 读素材并校验 | 代码 | 读 mp4/ASR/OCR/帧，判材料够不够 | `input_quality` | ✅ `high`：ASR 29 段、OCR 25 段、中文占比 0.813 |
| **N02** | 取本次规则 | 代码 | 按领域+形态挑规则 | `rules.rule_ids` | ✅ `U`（通用）+ `V11`（资讯·体育赛事）+ `F10`（访谈对谈） |
| **N03** | 理解：抽事实 | **模型** | 从材料抽 claim、语义单元、事件、对话轮、冲突 | `claims` `semantic_units` `events` `turns` `conflicts` | ✅ 8 claims / 4 单元 / 6 事件 / 2 turns / 3 conflicts；**u1 写对了因果方向** |
| **N03V** | 外部核查 | 代码+核查器 | 挑可核实的 claim 去外面查，证伪则排除 | `verification` `verification_risk` | ⚠️ 3 条全 `unverifiable`；检索结果全是**无关足球新闻**（"德文"被匹配成 Dembele），风险 `low`，未阻断 |
| **N04** | 规则路由 | 代码 | 只保留命中的 V/F 切片 | `rules.match_trace` | ✅ 命中 V11 / F10 |
| **N05** | 边界：哪句能写 | **模型** | 逐 claim 判可写/带限定/禁写，指定归属 | `boundary.decisions` `unit_decisions` | ✅ c1–c2 `usable`、c3–c8 `limited`，全部 `attributed`；u4 因空引用被判 `excluded` |
| **N06** | 可写信息路由 | 代码 | 一条可写都没有就停 | — | ✅ 有，继续（无独立事件） |
| **N07** | 看点：选主看点 | **模型** | 从语义单元选 1 主 + ≤2 备，写价值与钩子 | `highlights` `eliminated` `thesis_contract` | ✅ h1 主点 =「谦逊评价」，relation=`core` |
| **N08** | 装配生成上下文 | 代码 | 拼 N09 的入参 | — | ✅ 内联，无独立事件 |
| **N09** | 生成候选 | **模型** | 按 3 个槽位写 3 条标题 | `candidates[]`（`headline` `slot_id` `strategy` `claim_ids`） | ❌ cand-3 用 `疑问留答` 把已给结论改成待解悬念 |
| **N10** | 机械校验 | 代码 | 长度/问号结尾/引用白名单/政策 | `mechanical_checks` | ✅ 三条全 `pass`（13/19/17 字，上限 20） |
| **R00** | 主旨复核 | **模型** | 第二个人独立读一遍主旨 | `review_thesis_contract` `thesis_consistency` | ✅ `relation=aligned`，两份读法一致 |
| **R01** | 独立评审 | **模型** | 先写用户预期，再 G1–G3 + Q1–Q3，出 issues 与改写指导 | `reviews[]` `gates` `quality` `issues` | ❌ cand-3 `G3=pass`、两项布尔 `true`、`issues=[]`、`quality_sum=9` |
| **Q01** | 程序裁决 | 代码 | 合并评审+机械检查，定每条状态与总路由 | `candidate_decisions` `route` | ⚠️ cand-2/3 = `ready_for_human_review`；总路由 `needs_review`（`REFERENCE_AUDIT_INVALID`）→ 无推荐 |
| **N12** | 交给人 | 代码 | 落 `pending_human`、生成评审台 | `handoff` | ✅ `pending_human` |
| **H01** | 人工判定 | **人** | 三档 + 理由 | `human_feedback` | cand-1 `approved` / cand-2 `marginal` / cand-3 `rejected` |
| **R02** | 定向重写 | 模型 | 被拦后按 issues 改 | — | 未触发（`rewrite_round=0`） |
| **X01** | 技术失败 | 代码 | 异常兜底 | — | 未触发 |
| **X02** | 业务停止 | 代码 | 无可用陈述时停 | — | 未触发 |

> **为什么有些编号在日志里找不到独立事件**：`N01 / N02 / N06 / N08 / N10 / N11` 是 Python 函数内联，不是独立步骤。这是"设计编号"与"运行事件"不一一对应的原因，不是日志缺漏。

---

## 表 B · 字段词典（按节点分组）

> 列说明：**字面** = 日志里 JSON 的 key；**人话** = 这个字段回答什么问题；**本 case 值** = 实际内容；**判读** = ✅正常 / ❌异常 / ⚠️可疑。

### B1 输入层（N00–N02）

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `video.title` | 原视频标题（素材，不是事实许可） | 萨戈夫赛后采访：我不会说自己强于德文了，他是个传奇 | ✅ |
| `video.format` / `domain` | 形态 / 领域（决定挂哪条规则） | 访谈对谈 / 资讯·体育赛事 | ✅ → 命中 F10 + V11 |
| `input_quality.level` | 材料够不够：`high`/`medium`/`low` | `high` | ✅ 材料充足 |
| `input_quality.asr_segments` / `ocr_segments` | 口播段数 / 字幕段数 | 29 / 25 | ✅ |
| `input_quality.ocr_chinese_ratio` | 字幕中文占比（太低说明 OCR 不可用） | 0.813 | ✅ |
| `input_quality.advice` | 给下游的一句话建议 | 材料充足，正常提炼看点 | ✅ |
| `rules.rule_ids` | 这次挂了哪些规则 | `U` / `V11` / `F10` | ✅ |
| `prompt_hashes` | 5 个模型节点各自的提示词指纹 | understand/boundary/highlight/generate/review 各一个 64 位哈希 | ✅ 可复现的基线 |

### B2 理解层（N03）— 全链路最底层，错了会传染

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `claims[].claim_id` | 事实编号 | c1…c8 | ✅ |
| `claims[].text` | 这条事实本身 | c1 萨戈夫表示自己不认为已经强于德文；c2 萨戈夫称德文是传奇；c3 自己只赢过德文一次、德文赢过世界上所有顶尖选手 | ✅ |
| `claims[].claim_type` | 是"当事人说的话"还是"可观察事实" | 全部 `source_claim`（受访者观点） | ✅ 访谈类应如此 |
| `claims[].evidence_status` | 证据够不够：`supported`/`limited`/`unsupported` | 7 条 `supported`，c8 `limited` | ✅ |
| `claims[].assertion_permission` | 这话能怎么用：`direct`（可直接断言）/ `attributed`（必须标明谁说的） | 全部 `attributed` | ✅ 决定了标题必须带"萨戈夫" |
| `claims[].original_source` | 谁说的 | 萨戈夫（受访选手） | ✅ |
| `claims[].transmission_path` | 怎么传到观众：俄语口播→ASR→中文字幕 | 三条同一路径 | ✅ |
| `claims[].verification` | 外部核查结论（只对被挑中的 claim 有值） | c1–c3 均为 `verdict=unverifiable`、`confidence=0.05`、`sources=[]`；c4–c8 为 `null` | ⚠️ 中文名"德文"检索命中了足球新闻 |
| `semantic_units[].unit_id` | 语义单元编号（N07 只会从这里选看点） | u1–u4 | ✅ |
| `semantic_units[].meaning` | **多句话合起来的完整意思** | u1：「…不会声称已经强于德文，**因为**德文是传奇、赢过所有顶尖选手，而自己只赢过他一次」 | ✅ **全案唯一写清因果方向的地方** |
| `semantic_units[].kind` | 单元类型 | 全部 `source_opinion` | ✅ |
| `semantic_units[].role_in_video` | 这段在视频里扮演什么 | u1 核心表态 / u2 比赛过程自述 / u3 情绪与态度 / u4 未来对手话题 | ✅ |
| `semantic_units[].grounding` | 有原文撑着还是推断 | u1–u3 `grounded`，u4 `inferred` | ✅ u4 确实转写模糊 |
| `semantic_units[].corroboration.status` | 交叉印证 | u1–u3 `supported`，u4 `partially_supported` | ✅ |
| `semantic_units[].claim_ids` | 这个单元由哪些 claim 组成 | u1=[c1,c2,c3,c4]；**u4=[]** | ❌ u4 空值，后面引发审计阻断 |
| `events[]` | 事件（谁做了什么） | ev1 接受采访 / ev2 表示不认为强于德文 / ev3 称德文是传奇 / ev4 描述最后一轮 / ev5 德文在勾手赢他 / ev6 主持人问下一位对手 | ✅ |
| `turns[]` | 两个事件的关系 | ev5→ev2 `sequence`（赢了他却仍自认不强）；ev4→ev2 `causal`（选择不压制 → 不声称更强） | ✅ 方向都是"所以萨戈夫不认为自己更强" |
| `conflicts[]` | 材料内部矛盾及处理 | cf1 原题暗示"此前说过"但无证据，按当前表态为准；cf2 数字上下文模糊不纳入；cf3 人名转写不一致不确认 | ✅ 处理合理 |

### B3 边界层（N05）

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `boundary.decisions[].decision` | 这句能不能写：`usable`（可写）/ `limited`（可写但须带限定/归属）/ `excluded`（禁写） | c1/c2 `usable`，c3–c8 `limited` | ✅ |
| `required_limits` | 必须一起表达的限定 | c3「须保留归属为萨戈夫的说法」「不得写成德文赢过所有顶尖选手已被证实」 | ✅ |
| `forbidden_inferences` | 禁止的推断 | c1「不得写成萨戈夫在现实中已被证实弱于德文」 | ✅ |
| `attribution_required` + `required_source_phrase` | 要不要点信源、点哪个词 | 全部 `true` + `萨戈夫` | ✅ 这条后来误拦了 cand-1 |
| `unit_decisions[]` | 整个单元能不能用 | u4 → `excluded`，理由「引用为空，按不可写处理」 | ✅ 处理正确 |
| `unresolved_issues` | 没解决的边界问题 | `[]` | ✅ |

### B4 主旨层（R00 + N07 输出的 `thesis_contract`）

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `thesis_id` | 主旨编号（所有候选都绑它） | `t1` | ✅ |
| `topic` | 讲谁讲什么 | 萨戈夫赛后采访评价德文 | ✅ |
| `delivery_type` | 交付类型（访谈/教程/观点…） | 访谈 | ✅ |
| `core_promise` | **一句话核心观看承诺**（最该被兑现的东西） | 萨戈夫赛后表示自己不认为已强于德文，称德文是传奇、赢过所有顶尖选手，自己只赢过一次 | ✅ 是「谦逊评价」，不是「德文为何伟大」 |
| `required_semantics` | 删掉就变味的不可删语义 | [萨戈夫（受访选手）, 不认为自己强于德文, 称德文是传奇, 德文赢过所有顶尖选手、自己只赢过一次] | ✅ 四条，cand-3 只兑现了其中一部分 |
| `core_unit_ids` | 主单元 | [u1] | ✅ |
| `qualifiers` | 全局限定 | 萨戈夫个人观点需归属 / 不得写成德文是传奇已被证实 / 不得写成萨戈夫已被证实弱于德文 | ✅ |
| `review_thesis_contract` | 评审侧独立读出的主旨 | 与生成侧同义，多出「赛后接受采访」「赢过全世界」 | ✅ |
| `thesis_consistency.relation` | 两份主旨读法什么关系：`aligned`/`detail`/`missing_point`/`contradiction` | `aligned` | ✅ |

### B5 看点层（N07）

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `highlights[].highlight_id` | 看点编号 | h1 / h2 / h3 | ✅ |
| `highlights[].role` | `main`（主）/ `backup`（备） | h1 main，h2/h3 backup | ✅ |
| `highlights[].relation` | 与主旨的关系：`core`/`support`/`peripheral` | h1 `core`，h2/h3 `support` | ✅ |
| `highlights[].value` | **观看价值：点开能得到什么** | h1「了解萨戈夫赛后对德文的**谦逊评价**：自认不强于德文，称德文是传奇、赢过所有顶尖选手，自己只赢过一次」 | ✅ **主点选对了** |
| `highlights[].hook_type` | 勾人方式：`information`/`suspense`/`identity`/`conflict`/`emotion`/`contrast` | h1 `information`；h2 `suspense`；h3 `emotion` | ✅ |
| `highlights[].unit_id` / `claim_ids` | 绑定哪个单元、哪些事实 | h1=u1=[c1,c2,c3,c4] | ✅ |
| `highlights[].selection_reason` | 为什么选它（事后归因用） | **三条全空字符串** | ⚠️ 该有值却空着 |
| `highlights[].representativeness` | 代表性说明 | **三条全空字符串** | ⚠️ 同上 |
| `eliminated[]` | 淘汰了哪些角度、为什么 | 比赛细节「拿掉后视频核心意思仍在」/ 情绪「非主线核心」/ 未来对手「人名模糊且与主旨弱」 | ✅ 理由都成立 |

### B6 表达层（N09候选全字段）

| 字面 | 人话 | cand-1 | cand-2 | cand-3 | 判读 |
|---|---|---|---|---|---|
| `headline` | 标题正文 | 萨戈夫：我不认为已强于德文 | 萨戈夫赛后称德文是传奇，自己只赢过一次 | 赢过德文一次，萨戈夫为何仍称他传奇 | ❌ 只有 cand-3 |
| `slot_id` | 交付槽位 | `source_rewrite` | `core_summary` | `core_hook` | ✅ 三槽齐 |
| `strategy` | 表达策略（枚举 5 种） | 直接描述 | 直接描述 | **疑问留答** | ❌ cand-3 |
| `highlight_id` | 挂在哪个看点 | h1 | h1 | h1 | ✅ 同源 |
| `thesis_id` | 挂在哪个主旨 | t1 | t1 | t1 | ✅ 同源 |
| `claim_ids` | 用了哪几条事实 | [c1] | [c2,c3] | [c1,c2,c3] | ⚠️ cand-3 把 c1（结论）当 c2（结论）的解释 |
| `title_length` | 字数（含前缀） | 13 | 19 | 17 | ✅ 上限 20 |
| `qualifiers_used` | 必要限定实际怎么表达 | 归属为萨戈夫个人表态 | 归属为萨戈夫个人说法 | 归属为萨戈夫个人评价 | ✅ |
| `mechanical_checks.status` | 机械校验结论 | pass | pass | pass | ✅ |
| `programmatic_g1_flags` | 程序发现的事实类风险 | `G1_SOURCE_ATTRIBUTION_MISSING` | `[]` | `[]` | ❌ cand-1 被误判 |

### B7 评审层（R01）

| 字面 | 人话 | cand-3 的值 | 判读 |
|---|---|---|---|
| `expected_takeaway.subject` | 读者会认为主体是谁 | 萨戈夫 | ✅ |
| `expected_takeaway.content_nature` | 读者会认为这条内容是什么性质 | 以疑问形式呈现萨戈夫赢过一次却仍称德文传奇的态度 | ⚠️ 已在描述错位 |
| `expected_takeaway.viewing_promise` | **读者会期待看到什么**（评审的第一性承诺） | 看到萨戈夫解释为何仍称德文为传奇 | ❌ 承诺已被偷换 |
| `gates.G1.status` | 事实与断言许可 | `pass` | ✅ |
| `gates.G2.status` | 已批准业务政策 | `pass` | ✅ |
| `gates.G3.status` | 内容交付与承诺匹配 | **`pass`** | ❌ |
| `gates.G3.delivery_supported` | 视频确实交付了标题所说的内容？（布尔） | `true` | ❌ 判成"交付"，实为"出现过" |
| `gates.G3.core_covered` | 标题仍覆盖视频核心观看承诺？（布尔） | `true` | ❌ |
| `gates.G3.reason` | 判定理由（人的话） | 「疑问『为何仍称他传奇』在视频中有答案：萨戈夫称德文赢过全世界、自己只赢过一次，**故不认为自己更强**」 | ❌ 答案谓词落在"不认为自己更强"，问的是"称他传奇" |
| `gates.G4.status` | 机械校验（评审不得覆盖） | `pass` | ✅ |
| `quality.Q1/Q2/Q3.score` | 清晰度/信息价值/吸引力，各 1–3 | 3 / 3 / 3 | ❌ Q3 认为钩子"成立且克制" |
| `quality_sum` | 三项之和（最高 9） | 9 | ❌ 满分放行 |
| `issues[]` | 发现的问题（决定路由） | **`[]`** | ❌ 没有登记任何问题 |
| `issues[].issue_code` | 问题码（决定回退到哪） | —（cand-1 是 `G1_SOURCE_ATTRIBUTION_MISSING`） | — |
| `rewrite_guidance` | 改写指导：保留/修改/禁止 | preserve「赢过德文一次」「疑问结构」；forbidden「把疑问改成萨戈夫承认德文更强的事实」 | ❌ 指导在加固错误 |
| `memory_query` | 去案例库检索什么 | 「赛后采访疑问留答 赢过一次仍称传奇」 | — 未命中（`memory_trace.status=empty`） |
| `decision` | 这条候选的最终状态 | `ready_for_human_review` | ❌ |

### B8 裁决与人工（Q01 / N12 / H01）

| 字面 | 人话 | 本 case 值 | 判读 |
|---|---|---|---|
| `candidate_decisions[].status` | 每条候选状态：`ready_for_human_review`/`rewrite`/`needs_review`/`reject` | cand-1 `rewrite`；cand-2/3 `ready_for_human_review` | ⚠️ cand-1 误拦 |
| `candidate_decisions[].reason` | 状态原因码 | `REPAIRABLE_GATE_OR_QUALITY_ISSUE` / `ALL_GATES_PASS_AND_QUALITY_MEETS_MINIMUM` | ✅ |
| `route.action` | 总路由：`handoff`（有推荐）/ `needs_review` / `rewrite` / `reject` | `needs_review` | ⚠️ 因为程序缺陷，不是语义识别 |
| `route.recommended_candidate_id` | 推荐哪一条（首推） | `null` | — 无推荐 |
| `route.reason` | 为什么这样路由 | `REFERENCE_AUDIT_INVALID` | ⚠️ 与语义无关 |
| `reference_audit.issues[]` | 引用结构审计发现的问题 | `understanding.semantic_units[3].claim_ids: empty_refs` | ⚠️ u4 已被边界判 excluded，仍然报阻断 |
| `delivery_candidates` / `pending_review_candidates` | 交付面 / 待核实面 | `[]` / `[]` | — 两个池都空 |
| `human_feedback[].decision` | 人工三档：`approved`/`marginal`/`rejected` | cand-1 approved / cand-2 marginal / cand-3 rejected | ✅ |
| `human_feedback[].reason` | 人工理由 | cand-3：「视频展示了萨科夫受采访时的真实回应，而不是讲他为什么称德文是传奇。」 | ✅ 这就是 bad case 的定义源 |
| `human_feedback[].final_headline` | 人工终稿（可无） | 三条均无改写（null 或同原标题） | — 驳回不产生终稿，符合规范 |

---

## 表 C · 枚举速查（最容易被误读的 9 组）

| 字段 | 取值 | 人话 |
|---|---|---|
| `claim_type` | `source_claim` / `observable` / … | 当事人说的话 / 可观察事实 |
| `assertion_permission` | `direct` / `attributed` | 可直接断言 / 必须写明谁说的 |
| `evidence_status` | `supported` / `limited` / `unsupported` | 证据够 / 勉强够 / 不够 |
| `boundary.decision` | `usable` / `limited` / `excluded` | 可写 / 带限定可写 / 禁写 |
| `relation` | `core` / `support` / `peripheral` | 去掉就失去意义 / 支撑细节 / 边角 |
| `hook_type` | `information` / `suspense` / `identity` / `conflict` / `emotion` / `contrast` | 信息 / 悬念 / 身份认同 / 冲突 / 情绪 / 反差 |
| `strategy` | 问题方法 / 对照比较 / 前后变化 / **疑问留答** / 直接描述 | 视频给方法 / 有同条件对比 / 有前后两态 / **视频确实给答案** / 兜底 |
| `slot_id` | `source_rewrite` / `core_summary` / `core_hook` | 原题保守重述 / 整片主旨直述 / 同主旨的钩子表达 |
| gate/候选状态 | `pass` / `revise` / `needs_review` / `reject` / `rewrite` / `ready_for_human_review` | 通过 / 需修改 / 需人看 / 毙 / 回去重写 / 可交人 |

---

## 表 D · 本 case 流水账（真实时间线）

| 时刻 | 节点 | 发生了什么 | 耗时 |
|---|---|---|---|
| 07:50:43 | N00 | 受理、冻配置、存输入快照 | — |
| 07:50:43 | N01 | 材料校验 → `high` | — |
| 07:50:43→07:54:57 | N02 | **4 分 14 秒无事件覆盖**（读素材/加载是内联代码，耗时看不见） | ≈254s |
| 07:54:57→07:55:16 | N03 | 理解，抽 8 claims / 4 单元 / 3 conflicts | 19.1s |
| 07:55:16→07:55:26 | N03V | 分 3 次查 c1/c2/c3，**全部 unverifiable**（`sources=[]`、confidence 0.05） | 1.8+1.6+1.9s |
| 07:55:26 | N04 | 规则路由 → V11 + F10 | 8ms |
| 07:55:26→07:55:39 | N05 | 边界 8 条判定，u4 excluded | 12.9s |
| 07:55:39→07:55:44 | N07 | 看点：h1 主点=谦逊评价 | 5.2s |
| 07:55:44→07:55:48 | **N09** | **三槽生成，cand-3 = 疑问留答** | 3.3s |
| 07:55:48→07:55:52 | R00 | 主旨复核 → aligned | 4.5s |
| 07:55:52→07:56:03 | **R01** | **评审 cand-3 → pass / true / true / issues=[]** | 10.6s |
| 07:56:03 | Q01 | 合并裁决 → cand-2/3 ready，总路由 needs_review | 6ms |
| 07:56:03 | N12 | 交人 → `pending_human` | 3ms |
| 2026-11-02 08:37–08:38 | H01 | 人工三档判定 | 人 |

总耗时 5 分 20 秒，其中模型调用 60.9 秒，**素材读取约 254 秒**。

---

## 表 E · 三条候选并排：唯一变量是"表达策略"

| | cand-1 | cand-2 | cand-3 |
|---|---|---|---|
| 同一主看点 | h1 | h1 | h1 |
| 同一主旨 | t1 | t1 | t1 |
| 用了哪几条事实 | c1 | c2 + c3 | c1 + c2 + c3 |
| 策略 | 直接描述 | 直接描述 | **疑问留答** |
| 槽位 | source_rewrite | core_summary | core_hook |
| 系统判定 | rewrite（被 `G1_SOURCE_ATTRIBUTION_MISSING` 拦） | ready（9 分） | ready（9 分） |
| 人工 | ✅ 可用 | ⚠️ 勉强 | ❌ 不可用 |

**这张表本身就是一个结论**：三条候选的事实来源、主旨、看点完全相同，槽位是设计好的分工，**唯一能解释好坏差别的变量就是 `strategy`**。所以根因不必往上游找——上游三条都吃的是同一份材料。

---

## 表 F · 空值清单（"该有值却空着"也是信息）

| 字段 | 本 case 值 | 说明 |
|---|---|---|
| `highlights[].selection_reason` | `""` ×3 | 提示词要求写选择理由；空值让"主点为什么是 h1"事后无法归因 |
| `highlights[].representativeness` | `""` ×3 | 同上 |
| `candidates[].thesis_basis` | `""` ×3 | 候选没写"我服务主旨的哪一句" |
| `candidates[].change_reason` | `""` ×3 | 首轮无重写，合理为空 |
| `delivery_candidates` / `pending_review_candidates` | `[]` / `[]` | 交付面与待核实面都空 → 所以无推荐 |
| `memory_trace[0].status` | `empty` | 案例库未命中，降级为"仅正式规则"（符合设计） |
| `primary_evidence_audit.claims` | `{}` | 一手证据审计无 flagged |
| `reference_audit.semantic_truth` | `not_evaluated` | 审计只查结构，**不查语义**——所以它拦住 cand-3 的是引用格式，不是内容 |
| `reviews[].issues`（cand-3） | `[]` | ❌ 最危险的"空"：全部判据都空着通过 |
| `verification_risk.level` | `low` | 与"3 条全 unverifiable"看似矛盾，实际是"没证伪就不算风险"的口径 |

---

## 一句话收口

看懂这套日志只需要三件事：**表 A 记节点在干什么，表 B 记字段在问什么，表 E 记变量只有一个**。本 case 的错误在表 B 的两行——`strategy=疑问留答` 与 `G3.delivery_supported=true`——其余节点都正常，包括被怀疑的看点提取。

---

## 表 G · 表达层与评审层的实证（2026-11-03 补，回答"为什么传到表达层就变味"）

### G1 · N09 实际拿到 19 个键，不是只有看点

`events[].payload_json.user_payload` 的键：`video` `claims` `semantic_units` `unit_decisions` `boundary` `source_requirements` `blocked_claims` `unresolved_issues` `highlights` `thesis_contract` `matched_rules` `input_quality` `source_evidence_usage` `display_config` `strategy_enum` `target_count` `rewrite_round` `risk_signals` `memory_cases`。

**结论：主旨（`thesis_contract` 9 个字段）和一手语义（`semantic_units[].meaning`，含 u1 那句带「因为」的原句）都在入参里。** 所以"表达层只拿到看点"不成立。

### G2 · 传得不完整的三处（这才是变味的原因）

| # | 缺什么 | 证据 | 后果 |
|---|---|---|---|
| 1 | `strategy_triggers` 没传 | `agent_system/rules/rubric.py:68` 定义了每个策略的触发条件，`agent_system/pipeline.py:3672` 只把 `strategy_enum`（5 个名字）传进去 | 模型只知道有"疑问留答"这个名字 + 系统提示词一句「视频确实给答案」，看不到结构化判据 |
| 2 | 结构选择的锚点是 `highlights[].value`，不是 `semantic_units[].meaning` | `highlights[].value` 是二手概括，把 u1 的「因为 A 所以 B」抹平成「B、A、C 并列」；提示词要求 core_hook「从主看点自身具备的条件里选结构」 | 模型在并列句里找到两组"可比要素"，于是造反差，而不是复述因果 |
| 3 | 一手语义没有"不可删"标记 | `semantic_units` 传了原文，但没标哪半句是答案、哪半句是限定 | 模型删「德文赢过所有顶尖选手」时，没有任何字段告诉它这是答案 |

### G3 · `疑问留答` 的判据原文（确认它确实缺条件）

`agent_system/rules/rubric.py:15-22`：

```
STRATEGY_ENUM = ["问题方法", "对照比较", "前后变化", "疑问留答", "直接描述"]
STRATEGY_TRIGGERS = {
    "问题方法": "视频给出方法、步骤或可照做的操作",
    "对照比较": "材料包含两个及以上对象的同条件对比",
    "前后变化": "材料展示同一对象的前后两个状态",
    "疑问留答": "视频确实在该片段内给出答案",
    "直接描述": "以上条件都不满足时的兜底",
}
```

四个策略里，前三个都限定了**材料结构**（有方法/有对比/有前后态）；`疑问留答` 唯一限定的是**"有答案"**——不问"问题是谁提的""问题是否存在"。

规则包里另有两条相关规则（`rulepacks/common.md:65-67`）：

- **U-W02**：「有真实变化时突出前后变化；有可比条件时呈现比较；**有完整回答时可以保留问题**……没有相应材料，不强行套结构。」
- **U-W03**：「悬念必须有明确对象、真实前提与**视频内可兑现的答案**；问句本身也不能夹带未经支持的事实。」

U-W03 的三个条件本案前两个成立，第三个"可兑现"**没有操作化定义**——模型按"视频里有能拼成答案的句子"执行。这不是规则缺失，是判据没落到可判定动作。

### G4 · 禁止问号 + 允许「为何」= 一条规避路径

`rubric.py:60` `ending_question_mark_forbidden: True`，G4 有对应错误码 `G4_ENDING_QUESTION_MARK`。
cand-3 没有问号，用「为何」实现疑问 —— 机械校验查不到，G4 判 pass。

**系统一边禁止问号（说明产品不想用问句），一边允许"为何/为什么"造疑问（效果等同问句）。** 这是本案能绕过 G4 的直接原因。

### G5 · 候选状态判定：只看 status 与分数，`issues` 不参与

`agent_system/pipeline.py:2305-2311` 的判定优先级：

```
任一 gate = needs_review          → needs_review
G4 fail / 任一 gate = revise / 任一 Q 分 < 2  → rewrite
三个 gate 全 pass 且每项 Q ≥ 2     → ready_for_human_review
其余                              → needs_review (REVIEW_RESULT_INCOMPLETE)
```

阈值：`quality_min_each = 2`、`sum_is_gate = False`（`rubric.py:56`）——**总分不是门，只有每项 ≥2**。

`issues` 数组在这段判定里**只被原样带出**（`pipeline.py:2311`），不参与任何一条分支。它的实际用途只有两个：
1. 给人看；
2. 给 R02 定向重写决定"改表达还是改看点"（`pipeline.py:3650` / `3691`：命中 `G3_CORE_POINT_OMITTED` 或 `rollback_to==N07` 走"看点"，否则走"表达"）。

**所以要拦住一条候选，模型必须把问题降级成 `gate.status` 或把 Q 分打到 2 以下。只写 issue 不降级 = 没有效力。**

### G6 · 本案三道防线的实际状态

| 防线 | 应然 | cand-3 实然 | 谁失守 |
|---|---|---|---|
| N09 策略门槛 | 「疑问留答」须视频内真有问题被回答 | 条件被读成"有答案即成立" | 表达层 |
| N10 机械校验 | 禁问号、禁元话语、长度 | 无问号 → pass | 校验层（规则本身可绕） |
| R01 G3 | `delivery_supported`/`core_covered` 任一 false 即不得 pass | 两项都 true | 评审层 |
| R01 `issues` | 发现问题须登记并降级 gate | `[]` | 评审层（且该通道无效力） |
| Q01 判定 | 三 gate pass + Q≥2 → ready | 必然 ready | 判定层（按设计工作） |

失守的不是一处判错，是**三层各有一个"只差一格"的口子**：策略门槛差"问题是否存在"、机械校验差"没有问号的疑问"、评审差"答案是否回答此疑问"。
