# 反馈驱动防线修复闭环

## 已接通的链路

人工判定保存 → 同事务 outbox → 提交后处理 → 缺陷+证据归因假设+修复草案 → 人工确认具体修复与回归 → 真实全量测试证据 → 人工审核原案例/正例/反例 → 人工批准当前源码版本 → 配置门禁的运行启动检查。

不会自动编辑、提交、部署或推送生产源码；自动部分是问题处理和证据组织，不是无限制自改规则。

## 数据与真实回填

runtime/repair/repair.db 是中央汇总账本。本次只读导入 real_run_300、real_run_flash_nothink、review_batch_001 三批任务，共112条判定：88条triage待定位，24条no_defect正例。重复回填处理0条。

88条调查方向：selection 35、expression 14、constraint 10、numeric 3、content_nature 3、unresolved 23。这些为规则启发式假设，不是已证实根因。保留人工理由、原标题、修改标题、节点快照、证据位置、规则与提示词版本。正例不变成缺陷，勉强通过和有改动的通过会进入缺陷调查。

源库路径登记在repair_sources。批准、运行检查之前重新读取源库；改判、撤回使旧 revision 对应提案失效。新判定会先写各任务库，中央可通过 backfill 同步；不是假装不同SQLite自动强一致。无需模型API。

## 日常入口

```bash
python3 -m agent_system.repairs backfill --db runtime/real_run_300/agent.db --db runtime/real_run_flash_nothink/agent.db --db runtime/review_batch_001/agent.db
python3 -m agent_system.repairs list
python3 -m agent_system.repairs sync
python3 -m agent_system.repairs drain
python3 -m agent_system.repairs analyse
python3 -m agent_system.repairs review --help
python3 -m agent_system.repair_release --help
```

每个缺陷的feedback_key和revision构成提案ID：feedback_key@revision。review要求填写reviewer、root-cause、change、regression-tests、counterexamples、acceptance，不接受无依据的“已经修复”。之后 validate --proposal ID 执行真实pytest，verify-cases绑定验证文件、提案和三项人工检查；approve必须--confirm和reviewer。revoke同样要求显式确认与理由。

人工checklist文件格式：

```json
{
  "original_case": {"verified": true, "evidence": "填写原案例ID、期望与实际结果及审核依据"},
  "positive_cases": {"verified": true, "evidence": "填写应触发样本和结果"},
  "negative_cases": {"verified": true, "evidence": "填写不应误伤样本和结果"}
}
```

这是格式，不是本次已审核声明。当前88条提案仍为草案，未代替用户确认，未批准发布。

## 门禁边界

release_gate使用SQLite作为审核状态来源，JSON为可读证据，逐个对照SHA256和存储内容；不能把失败JSON改成成功来批准。验证绑定源码、规则和测试配置指纹；文件漂移、空测试、跳过、失败、超时、缺案例审核或撤销均拒绝。指纹覆盖agent_system、rules、verification及rulepacks的相关文件。

extra.release_gate_dir配置后，运行必须存在active批准；extra.repair_ledger指定提案账本。未配置门禁保留开发运行方式，不声称所有现有本机命令已强制生产锁定。发布批准的是已经验证的当前源码，不是自动应用补丁；撤销不会自动恢复旧源码。

缺陷历史append-only保留修订；不做未验证的删除。事件失败可重试，过期失败事件不能覆盖更新的已处理改判。

## 案例回流：从「只存不用」到跨运行可见

**修复前的真实状态。** 案例默认写在 `<output_dir>/agent.db`，每个实验目录一份。扫全部库：282 次检索只命中 13 次（4.6%），同视频命中 0 次。142 条已确认案例锁死在 5 个实验目录里，`runtime/agent.db`（44 个任务）案例数为 0——新运行看到的是一个空库。

**三处代码缺陷。**

1. `case_bank` 默认空 → 案例随 `output_dir` 隔离。现在 `resolve_case_bank` 默认返回 workspace 级 `runtime/cases.db`；`case_bank_isolated` 保留隔离对照实验。
2. `search_for_video` 用 `LEFT JOIN tasks` 找同视频案例。共享案例库里没有 `tasks` 表，这个写法会直接 `no such table`。现在只看 payload 里的 `video_id`，`_auto_case` 也把它写进 payload。
3. 历史案例 payload 没有 `video_id`，`scripts/merge_case_banks.py` 汇入时按 `source_task_id` 从源库 `tasks` 回填（142 条中 130 条可回填，12 条源任务已不可考）。

**修完后的实测（同一批历史任务、同一检索逻辑）。**

| 库 | 任务 | 命中 | 跨任务命中 | 同视频命中 |
|---|---:|---:|---:|---:|
| real_run_flash_nothink | 26 | 26 | 24 | 19 |
| real_run_300 | 34 | 34 | 31 | 30 |
| review_batch_001 | 6 | 6 | 6 | 4 |

这是用完整共享库回放的**上界**：任务运行时看不到「未来」才产生的案例，真实增益要等共享库启用后的新一批运行。可信的部分是跨任务/同视频命中不再是 0，说明检索能跨视频泛化，不是只认自己那条。

**88 条提案一次决策一组。** `repairs group` 按根因族聚合（实测 88 条 → 6 组）：selection 35 条/17 个视频、unresolved 23/14、expression 14/11、constraint 10/8、content_nature 3/2、numeric 3/1。聚合只读、不改提案状态；`review` 对代表案例给出根因、修复、回归与反例即可，其余随版本回归覆盖。

**观测口。** `scripts/memory_health.py` 输出共享库规模、历史命中率、同视频命中数。没有这个观测，「经验库到底有没有在反哺」只能靠感觉——这正是缺陷能潜伏这么久的原因。

## 反馈分流：一条判定往哪走

人工判定有六种，去向不同。**没有一种能自动改源码或提示词**——这是这套闭环唯一的铁律。

| 人工判定 | 系统当时怎么做 | 去向 | 谁确认 | 如何撤回 |
|---|---|---|---|---|
| 可用 | 也推荐了 | 案例库**正例**（自动写，`confirmed=1`） | 判定即确认 | `reopen` 重判 |
| 可用 | 没推荐 / 被拦 | **误拦工单** → 规则或路由修复提案 | 规则运营 | `revoke` |
| 不可用 | 推荐了 | **误放工单** → 评审规则修复提案 | 规则运营 | `revoke` |
| 不可用 | 也没推荐 | 案例库**反例**（`bad_example`） | 判定即确认 | `reopen` 重判 |
| 勉强通过 | — | **不进案例库** | — | — |
| **精彩** | — | 案例库 **good case**（需显式跑 `scripts/import_good_cases.py`） | 判定即确认 | 取消精彩后重跑，脚本自动清理该批旧行 |

三条通道的自动化程度不同，别混：

| 通道 | 触发 | 自动化 | 落库 |
|---|---|---|---|
| 正例 / 反例 | 人工判定 | 判定即写 | `runtime/cases.db` |
| 精彩 good case | 人工标「精彩」 | **需显式跑脚本** | `runtime/cases.db` |
| 规则 / 路由修复 | 误拦、误放工单 | **不自动改代码**，只出提案 | `runtime/repair/repair.db` |

**「勉强通过」为什么不进案例库**：它既不是可复用的好示范，也不是明确的反例。写进去只会稀释检索结果——案例库靠的是精确，不是量大。

**误拦有一条专门约束**：被机械检查判否的候选如果被人评过，判定照样计入——否则误杀的代价会被系统自己藏起来（见 [bad-case-spec.md](bad-case-spec.md) §10.2）。

## 发布后观察与结案

批准后中央账本登记 observing；其他视频出现同族新负反馈时标 recurrence_suspected，连同反馈修订与材料保留证据，不把同族直接断言为同根因。已 closed 的修复也会被重开为疑似复发。resolve需要明确审核理由才能恢复观察；close需要明确审核与观察证据，疑似复发未处理不能结案。revoke使门禁拒绝运行，不自动回退代码。

```bash
python3 -m agent_system.repair_release observations
python3 -m agent_system.repair_release close --help
python3 -m agent_system.repair_release resolve --help
python3 -m agent_system.repair_release revoke --help
```

反馈原判撤回会把相应观察记录标invalidated。当前不自动按运行量计算结案，也不以“没有反馈”证明没有错误；观察证据必须明确审核。中央观察以已汇入的事件为范围，新增任务库仍需登记同步。

## 当前验证与清单

最终全量测试：217 passed。真实账本sync再次处理0条、失败0条；88条待定位草案已刷新为具体缺陷族工作项。runtime/repair/backlog-001.md为可读工作清单，可用scripts/repair_report.py在新路径再次导出。没有把现有草案假装审核通过，没有代替用户批准发布。

## 验收与剩余局限

测试覆盖新反馈、撤回再判、事务失败回滚、重复历史回填、提案改判失效、发布JSON篡改、源码和测试漂移、撤销和验证复用。测试通过只证明这些机制，不证明每个缺陷已修好。

当前归因是确定性证据整理+分层假设，不是通用语义诊断模型；提案草案需要具体实现与测试审核。未读取完整材料的旧评测无法补造事实。发布审核是本机显式确认，不提供远程身份认证；能改SQLite或Python环境的本地管理员不在防篡改承诺范围。
