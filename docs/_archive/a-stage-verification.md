# A 阶段修复验证清单

> 范围：本轮（A 阶段）已经落到源码里的 10 项修复，逐条给出位置与验证方式。只写「代码里能指到行」的事实，不含计划项。
> 行号快照：2026-11-01 16:25 工作区。`agent_system/pipeline.py`、`scripts/eval.py`、`agent_system/batch_review.py` 在本轮被并行改动，行号会漂移；每节都附定位锚点，复核方式 `grep -n '<锚点>' <文件>`。
> 全量验证：`python3 -B -m pytest -q` → **289 passed, 0 xfailed**（2026-11-01 16:29 实测）。原先由 `xfail(strict=True)` 钉住的 7 个缺陷已全部修复并转为正常断言（见末节）。
> 约束：本文不改代码、不改测试、不联网、不调用模型、不读写凭据。

## 总览

| # | 修复 | 代码位置（行号+锚点） | 回归测试 |
|---|---|---|---|
| 1 | 规则 ID 字母后缀解析 | `agent_system/rules/loader.py:28` | `tests/test_rule_ids.py`（3 条）、`tests/test_thesis_final.py:41` |
| 2 | 候选/金标身份（任务 + 候选 + 标题哈希） | `goldsheet.py:77-83,100-125` / `storage.py:58,112-119` / `batch_review.py:22,75-107,195` / `eval.py:225-231` | `test_safety_contracts.py:663,675`、`test_identity_and_eval.py:68,62`、`test_batch_review.py:200,150` |
| 3 | 系统原推荐不可变快照 | `storage.py:233,169-177` / `eval.py:237` | `test_safety_contracts.py:561,579`、`test_identity_and_eval.py:126`、`test_batch_review.py:217` |
| 4 | 改判/撤回清理 | `storage.py:250-254,258-269,148-199` / `goldsheet.py:100-125` | `test_safety_contracts.py:542`、`test_identity_and_eval.py:145,81`、`test_decision_wiring.py:334` |
| 5 | 独立案例库随主库提交或回滚 | `storage.py:281-289,362-364` | `test_safety_contracts.py:614,597`、`test_identity_and_eval.py:114` |
| 6 | 严格率 / 推荐分母 / 长度统计口径 | `eval.py:203-208,245,191,253-260,261-266,270-272,381-389` | `test_safety_contracts.py:834,845,855,888`、`test_identity_and_eval.py:50-65` |
| 7 | 待核验 claim 不进机械白名单 | `pipeline.py:1371,1715,1768` | `test_safety_contracts.py:241`、`test_decision_wiring.py:73`、`test_signal_integration.py:149` |
| 8 | 显式全排除不被空集合复活 | `pipeline.py:1769-1774` | `test_safety_contracts.py:255` |
| 9 | 重写子结果风险路由不被丢弃 | `pipeline.py:1466-1486` | `test_safety_contracts.py:293` |
| 10 | thesis/slot 契约接入 | `thesis.py:15-17,44-93,96-118` / `pipeline.py:1431-1442,1501-1513,1806-1817` | `test_thesis_final.py`（10 条）、`test_safety_contracts.py:351,362,374`；管线级错配检测仍是 xfail（`:386`） |

> 行号前缀省略了 `agent_system/`；测试文件都在 `tests/` 下。

---

## 1. 规则 ID 字母后缀解析

**问题**：`rulepacks/common.md` 里有 `U-B03a`、`U-B07a–d` 这类带字母后缀的规则，旧正则只认 `U-[A-Z]\\d{2}`，评审引用合法规则被判 unknown，对题候选被误转 `needs_review`。
**修复**：合法 ID 正则加可选后缀 `[a-z]?`，后缀规则与同名基础规则各自成条、不互相吞掉。
**位置**：`agent_system/rules/loader.py:28`（锚点 `U-[A-Z]\\d{2}[a-z]?`）；规则正文 `rulepacks/common.md:14`、`:23`。
**如何验证**：`python3 -B -m pytest -q tests/test_rule_ids.py tests/test_thesis_final.py -k "rule or suffix"`
- `tests/test_rule_ids.py`：后缀 ID 全部进表、基础 ID 不丢、`U-B07` 与 `U-B07a` 共存且基础 ID 只出现一次；
- `tests/test_thesis_final.py:41`：`test_suffix_rule_ids_are_recognized` 再锁一次这条路径。

---

## 2. 候选/金标身份：任务 + 候选 + 标题哈希

**问题**：金标与历史对照原来只按 `video_id + candidate_id` 覆盖/撤回。同一视频被不同模型臂、不同规则版本跑过时，同名候选会把别的任务的标签写成当前任务结果。
**修复**：
- 金标覆盖键改成「视频 + 任务 + 候选」；撤回必须带 `task_id`，不传时只撤回任务列为空的历史行（`agent_system/goldsheet.py:77-83,100-125`）。
- 任务库新增按任务隔离的候选表 `candidates_by_task`，旧 `candidates` 表保留只为兼容历史读取，不再作身份来源（`agent_system/storage.py:58,112-119`）。
- 冻结批次评审库：标题用 `sha256` 记指纹，候选从 `delivery_candidates` / 任务 state 还原并要求完整标题一致，判定按「批次 + 任务 + 候选」落 `candidate_decisions_v2`（`agent_system/batch_review.py:22,75-107,`195`）；导出侧再用 `review_mapping.json` 的 `title_sha256` 与冻结快照比对，不一致拒绝（`scripts/export_batch_review.py:60`）。
- 评测侧：金标候选必须真的存在于它指的那个任务（含停在检查点的 `pending_review_candidates`），对不上按「身份不一致」拒绝计分（`scripts/eval.py:222-228`）。
**如何验证**：
- `python3 -B -m pytest -q tests/test_safety_contracts.py -k "goldsheet or identity"` —— `test_goldsheet_identity_includes_task`（`:663`）、`test_goldsheet_remove_is_scoped_by_task`（`:675`）。
- `python3 -B -m pytest -q tests/test_identity_and_eval.py -k "gold"` —— `test_gold_upsert_keeps_distinct_tasks`（`:68`）、`test_gold_remove_is_task_scoped`（`:81`）。
- `python3 -B -m pytest -q tests/test_batch_review.py -k "isolated_by_task or title_hash"` —— `:200`、`:150`。

---

## 3. 系统原推荐不可变快照

**问题**：人工驳回会改写 `route.eligible/recommended`。评测若继续读 `route.recommended_candidate_id`，「推荐准确率」就变成用人工改过的结果证明系统自己。
**修复**：首次人工判定时把 `route` 深拷贝进 `original_system_route`（只写一次）；撤回时从快照恢复 recommended；评测/CLI/调试台/冻结评审库写「系统推荐」列一律读快照。
**位置**：`agent_system/storage.py:233`（锚点 `首次人工判定时冻结系统原推荐`）、`:169-177`；`scripts/eval.py:234`；`agent_system/cli.py:113`；`agent_system/debugger.py:337`；`agent_system/batch_review.py:195`。
**如何验证**：
- `python3 -B -m pytest -q tests/test_safety_contracts.py -k "original"` —— `test_all_rejected_keeps_immutable_original_system_route`（`:561`）、`test_reopen_restores_original_recommendation`（`:579`）。
- `python3 -B -m pytest -q tests/test_identity_and_eval.py -k original_system` —— `:126`。
- `python3 -B -m pytest -q tests/test_batch_review.py -k original_system_route` —— `:217`。

---

## 4. 改判/撤回清理

**问题**：候选先「通过」再改判「驳回」时，旧终稿与 `approved` 状态会留在任务上，报告和金标继续读过期结论；撤回也可能连带删掉别的任务。
**修复**：
- 改判为驳回时清掉该候选残留的 `human_decision / final_headline`（`agent_system/storage.py:250-254`）。
- 驳回只缩小可审范围 `eligible_candidate_ids`，不改写 `recommended`；全部驳回才转 `stop`（`:258-269`）。
- 撤回删本任务的 `human_decisions / labels / H01 事件` 与对应金标行；调试台撤回时传 `task_id`（`:148-199`、`agent_system/goldsheet.py:100-125`、`agent_system/debugger.py:316`）。
**如何验证**：
- `python3 -B -m pytest -q tests/test_safety_contracts.py -k "stale_approval"` —— `test_reject_after_approve_clears_stale_approval`（`:542`）。
- `python3 -B -m pytest -q tests/test_identity_and_eval.py -k rejudging` —— `test_rejudging_to_rejected_clears_stale_approval`（`:145`）。
- `python3 -B -m pytest -q tests/test_decision_wiring.py -k reopen` —— `:334`（撤回只删匹配行，可重判）。
- 已闭合：撤回同时删除 `cases` 与 `feedback` 行，回归见 `tests/test_safety_contracts.py` 的 `test_reopen_drops_stale_feedback_rows`。

---

## 5. 独立案例库随主库提交或回滚

**问题**：配了 `case_bank`（独立案例库）时案例写入原来会先提交；主库随后回滚，案例却已入册，检索会把一条不存在的通过/驳回当有效经验。
**修复**：案例写入不自行提交，由 `record_human_decision` 在主库 `commit` 成功后统一提交；主库异常则 `case_conn.rollback()`。`_auto_case` 内注明「不在此处提交」（`agent_system/storage.py:281-289`、`:362-364`）。同一候选重新判定时先删旧自动案例（`:234-244`）。
**如何验证**：`python3 -B -m pytest -q tests/test_safety_contracts.py -k "case"`
- `test_main_rollback_does_not_confirm_case`（`:614`）：主库失败后案例库不残留；
- `test_separate_case_bank_reopen_is_committed`（`:597`）：独立案例库在撤回时正常提交；
- `tests/test_identity_and_eval.py:114` 用 SQLite 触发器制造主库失败，断言 `cases` 表 0 行。

---

## 6. 严格率 / 推荐分母 / 长度统计口径

**问题**：旧口径把「勉强通过」并进通过率、把「有推荐任务下的全部人工标签」当推荐准确率分母、把整任务所有候选的长度重复计入，三个数都会虚高。
**修复**：
- 严格率只数「可用」；「含勉强通过」单列一档，两档分开报（`scripts/eval.py:261-266`）。
- 推荐准确率分母 = 被评价的系统推荐本身（`:270-272`）。
- 长度只统计被人工评价的那一条候选（`:245`）。
- 不传 `--db` 扫全库时脚本显式警告「不能作为验收结论」（`:203-208`）；给了 `--manifest` 就用清单全集做预注册分母，未产出/未评的样本留在分母里计失败（`:191`、`:253-260`）。
- 身份对不上的标签单独成节列出，不进任何比率（`:381-389`）。
**如何验证**：`python3 -B -m pytest -q tests/test_safety_contracts.py tests/test_identity_and_eval.py -k "rate or denominator or missing_candidate or marginal"`
- `test_strict_pass_rate_excludes_marginal_verdict`（`:834`）、`test_recommendation_agreement_denominator_counts_only_recommended`（`:845`）、`test_label_for_missing_candidate_is_not_counted`（`:855`）、`test_eval_compare_paired_denominator_counts_only_paired_videos`（`:888`）；
- `tests/test_identity_and_eval.py:50-62` 是同一口径的独立复现。
- 已闭合：`inter_rater` 聚合键加入 `task_id`；`eval_compare` 只统计配对集合内标签，且交集为空直接报错不出表。

---

## 7. 待核验 claim 不进机械白名单

**问题**：「暂缓核验」的陈述（`verification_required=true`）既不是「已排除」也不是「可写」。旧代码把它留在可写集合里，机械校验就当有据事实放行。
**修复**：边界一旦存在 `verification_required` 决策就停机 `PENDING_VERIFICATION` 并保留结构化核验任务（`agent_system/pipeline.py:1371`）；可写集合 `allowed_claims` 显式排除它（`:1768`）；暂缓核验的陈述不产生「来源/限定」要求（`:1715`）。
**如何验证**：
- `python3 -B -m pytest -q tests/test_safety_contracts.py -k mechanical_whitelist` —— `test_pending_verification_claim_cannot_enter_mechanical_whitelist`（`:241`）：只有 c1 正常、c2 待核验，候选只引 c2 时机械失败（`G4_INVALID_REFERENCE`）且不 handoff。
- `python3 -B -m pytest -q tests/test_decision_wiring.py -k pending_verification` —— `:73`（停机 + `verification_tasks` 非空）。
- `python3 -B -m pytest -q tests/test_signal_integration.py -k numeric_claim_cannot_bypass` —— `:149`（无据数字 → excluded + verification_required，不产出候选）。

---

## 8. 显式全排除不被空集合复活

**问题**：「边界没输出 `unit_decisions`」与「所有单元都被明确排除」都会让 `allowed_units` 变空；旧写法在空集时回退成「全部单元」，显式排除被静默撤销。
**修复**：只有 `boundary.unit_decisions` 为空才回退到全部单元；非空就严格按排除结果算，空就是空。
**位置**：`agent_system/pipeline.py:1769-1774`（锚点 `边界未输出 unit_decisions`）。
**如何验证**：`python3 -B -m pytest -q tests/test_safety_contracts.py -k fully_excluded` —— `test_fully_excluded_units_are_not_revived_by_empty_allowed_set`（`:255`）：唯一单元 u1 被判 excluded，候选仍引用 u1 时机械失败且不 handoff。
**已闭合**：`rereview` 现在同样按边界收窄可引用单元（传 `allowed_units`），回归见 `test_rereview_enforces_excluded_units`。

---

## 9. 重写子结果风险路由不被丢弃

**问题**：重写子调用可能返回 `needs_review / reject`（内容性质待审、核心事实未决）。这些信号只落在子 `route` 上、不在 `decisions` 里，外层只用 `decisions` 重新聚合时会把它覆盖成 `handoff`。
**修复**：重写循环收集子 `route` 的 `needs_review/reject` 为 `escalations`；非空时外层直接转 `needs_review`，reason 前缀 `SUBROUTE_ESCALATION`，并合并子路由的 `pending_signal_ids`。
**位置**：`agent_system/pipeline.py:1466-1486`（锚点 `escalations`、`SUBROUTE_ESCALATION`）。
**如何验证**：`python3 -B -m pytest -q tests/test_safety_contracts.py -k subroute_escalation` —— `test_rewrite_subroute_escalation_is_not_dropped`（`:293`）：子结果转 `needs_review` 后外层仍是 `needs_review` 且保留 `pending_signal_ids`。

---

## 10. thesis/slot 契约接入

**问题**：诊断批（`docs/root-cause-18-review-batch.md`）里 18/18 条主旨在 state 为空、主看点 `relation` 无校验，边角事实能直接当主看点；候选也没有「同一主旨下三个表达槽」的结构。
**修复**：
- 新增 `agent_system/thesis.py`：必填字段与 `RELATIONS / SLOTS` 常量（`:15-17`）；`normalize_thesis_contract`（`:44-93`）校验字段、`required_semantics` 非空、`core_unit_ids` 不悬空且不引用已排除单元；`validate_highlight_alignment`（`:96-118`）校验「恰好一个主看点、relation=core、thesis_id 与主旨卡一致」。
- N07 后接契约：规范化 + 绑定看点 + 记 `thesis_contract_errors / thesis_alignment_errors`（`agent_system/pipeline.py:1431-1442`）；三槽交付按 `slot_id` 取、缺额写进 `delivery_slots.missing`、不复制标题凑数（`:1501-1513`）；新增独立 R00 主旨复核（只读原材料、首轮一次、失败回退到生成侧主旨）（`:1806-1817`）。
- 数据结构与提示词：看点带 `relation / thesis_id`（`agent_system/types.py:215-217`）；提示词要求看点标 `relation/thesis_id`、候选键名与三个 `slot_id`、R00 输出 `thesis_contract`（`agent_system/prompts.py:59-61,74,105-111`）；离线 mock 也能产出契约与 slot（`agent_system/model.py:134-136,151`）。
**如何验证**：`python3 -B -m pytest -q tests/test_thesis_final.py tests/test_safety_contracts.py -k "thesis or contract or slot"`
- `tests/test_thesis_final.py`：`:48`（合法契约规范化）、`:85`（主看点必须 core）、`:91`（主看点 thesis_id 必须一致）、`:120`（槽固定三条）；
- `tests/test_safety_contracts.py`：`:351`（缺字段/引用已排除单元）、`:362`（悬空单元/空契约）、`:374`（主看点必须 core）。
**已闭合**：改为先用模型自报的 `thesis_id` 做对齐校验、再给未标注的看点补齐，错配现在能被检出；同时主看点被标成 support/peripheral 时路由降级为 needs_review。回归见 `test_pipeline_detects_highlight_thesis_mismatch`。

---

## 原 xfail 缺陷：本轮已全部修复（7 条，现为正常断言）

这 7 条原先由 `pytest.mark.xfail(strict=True)` 钉住。本轮修复后标记已移除，变成会真正失败的回归断言；当前全量为 **289 passed, 0 xfailed**。

| # | 缺陷 | 位置 | 影响 |
|---|---|---|---|
| 1 | pipeline 先覆写 `highlight.thesis_id` 再做对齐校验，主旨错配查不出 | `tests/test_safety_contracts.py:386` | §10 的契约在管线里只挡字段缺失，不挡错配 |
| 2 | `rereview` 的 `_mechanical_check` 未传 `allowed_units` | `tests/test_safety_contracts.py:475` | 重评会放行已排除单元（§8 的口径只覆盖首轮生成） |
| 3 | 撤回不清旧 `feedback` 行 | `tests/test_safety_contracts.py:691` | 历史驳回理由继续留在案例库，撤回后仍可能被检索到 |
| 4 | marginal 判据 ID 只进 `gold.csv`、不进 DB `human_decision` | `tests/test_safety_contracts.py:780` | 按判定库统计判据会漏掉勉强通过这一档 |
| 5 | `inter_rater` 只按 (video_id, candidate_id) 聚合 | `tests/test_safety_contracts.py:867` | 跨任务同名候选被当成双标注，kappa 可比样本虚高 |
| 6 | `eval_compare` 的超长率与判定分布统计配对集合外的标签 | `tests/test_safety_contracts.py:899` | 对比表的分母与判定分布不是同一批样本 |
| 7 | 配对交集为空时 `eval_compare` 回退成两臂各自的视频集合 | `tests/test_safety_contracts.py:915` | 非配对批次也能报出「配对对比」 |

> 这 7 条都直接或间接影响评测读数。它们已在本轮修掉；修复前的历史报告（含 compare-human）仍带着这些偏差，不能与新读数混用。

---

## 重跑全部验证

```bash
python3 -B -m pytest -q                                   # 全量：289 passed, 0 xfailed
python3 -B -m pytest -q tests/test_rule_ids.py            # §1
python3 -B -m pytest -q tests/test_identity_and_eval.py   # §2–§6
python3 -B -m pytest -q tests/test_batch_review.py        # §2–§4（冻结评审台）
python3 -B -m pytest -q tests/test_decision_wiring.py     # §4、§7
python3 -B -m pytest -q tests/test_signal_integration.py  # §7
python3 -B -m pytest -q tests/test_safety_contracts.py    # §2–§10 的契约与安全回归
python3 -B -m pytest -q tests/test_thesis_final.py        # §1、§10
```
