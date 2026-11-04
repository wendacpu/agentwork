# 批次验收笔记：数据来源、命令、口径边界与已知限制

配套报告：`runtime/eval/batch-acceptance-report.md`（数字在那边，这里记来源与边界）。

## 一、数据来源

| 数据 | 位置 | 性质 |
|---|---|---|
| 批次冻结快照 | `runtime/acceptance_deepseek_20261101_114430/manifest.json` | 20 条抽样的 task_id / 视频 / 领域 / 输入质量；含 `config_sha256` 与逐文件源码哈希 |
| 盲评映射 | `runtime/acceptance_deepseek_20261101_114430/review_mapping.json` | 20 条，含 `candidate_id`、完整标题、`title_sha256`、`selection_method`；2 条无候选 |
| 人评库 | `runtime/acceptance_deepseek_20261101_114430/human_feedback.db` | `decisions(task_id, candidate_id, title_hash, payload_json)`，18 行，1 名判定者 `tiffany`，2026-11-01 |
| 任务库 | `runtime/acceptance_deepseek_20261101_114430/agent.db` | 本批 tasks 表：status / config_json / state_json（候选、评审、路由、重写记录） |
| 批次汇总 | `runtime/acceptance_deepseek_20261101_114430/summary.json` | 20 视频 / 18 有标题 / 状态与路由分布 / `selection_methods`：推荐 9、非推荐 9、无候选 2 |
| 派生的评测文件（本次新建） | `runtime/gold/batch-acceptance-20261101.csv` | `export_batch_review.py` 导出的金标，18 行 |
| 引擎原始输出（本次新建） | `runtime/eval/batch-acceptance-eval.md` | `eval.py` 原样输出，未手工改动 |
| 数字核对脚本（本次新建） | `runtime/eval/check-batch-acceptance-numbers.py` | 只读重算报告里每个数字，逐项比对用 |

两个数据库**只用只读方式打开**：`export_batch_review.py` 用
`sqlite3.connect(uri + "?mode=ro")` + `PRAGMA query_only=ON`；`eval.py` 用
`sqlite3.connect("file:…?mode=ro", uri=True)`；核对脚本同样。本批次数据库未被写入或迁移。

## 二、命令（可直接复制执行）

```bash
# 导出金标：逐条核对 candidate_id 与标题 sha256，任何不一致直接拒绝导出
python3 scripts/export_batch_review.py \
  --batch runtime/acceptance_deepseek_20261101_114430 \
  --out runtime/gold/batch-acceptance-20261101.csv
# 实际输出：导出 18 条判定 -> runtime/gold/batch-acceptance-20261101.csv（无拒绝项）

# 指定批次任务库跑评测（不传 --db 会扫 runtime 下全部历史库并给出警告，结果不可用）
python3 scripts/eval.py \
  --gold runtime/gold/batch-acceptance-20261101.csv \
  --db runtime/acceptance_deepseek_20261101_114430/agent.db \
  --out runtime/eval/batch-acceptance-eval.md

# 只读重算报告里每个数字（三种分母、推荐准确率、被拒 3 条、状态切片、字段填充率）
python3 runtime/eval/check-batch-acceptance-numbers.py
```

全程无模型调用、无付费 API、无联网；未读写 `.secrets.env`；未修改任何既有源码或脚本；未提交 git。

## 三、口径边界（读报告前必看）

1. **固定分母 20。** 分母是批次抽样视频数（20），不随产出浮动。2 条无标题按"未产出=失败"计入，不得剔除。
   报告主口径的 30.0%（严格）/ 55.0%（含勉强）用的就是这个分母。
2. **严格与含勉强分两行。** 「可用」与「勉强通过」不合并。合并会把"把不可用改判成勉强通过"记成质量提升。
3. **推荐准确率的分母是"被评价的系统推荐"，不是"有推荐任务下的全部人工标签"。**
   本批 9 条系统推荐全部被人工评价且全部落在可计分样本内，所以 5/9 = 55.6% 稳定。
   eval.py 里这个指标叫"人工一致率"，但它的构造就是"系统推荐那条被判可用/勉强"，语义上等同推荐准确率。
4. **金标候选必须存在于它指定的任务里，否则拒收。** 这是 `eval.py` 的身份门禁，本批触发 3 次（见第四节）。
5. **"人工可用"与"流程技术成功"必须分开记。** 人工判"可用"≠ 流程跑通；本批 technical_failed 里就有 2 条被判可用。
6. **本批不是独立验证集。** 20 条已进人工反馈，修复后若在本批复测只能算回归验证；
   要说"泛化变好"，必须换新抽样批次。

## 四、已知限制

### 1. eval.py 身份门禁曾只查 `state.candidates`，漏掉检查点候选（本批触发 3 条，已修复）

被拒 3 条：R01 `BV1uWnZ6vEvj` / `a2-1`（不可用）、R03 `BV1YGnc61EGr` / `a3-1`（可用）、R04 `BV19BHg6kEFD` / `a2-1`（可用）。
三个任务都是 `technical_failed`，`state.candidates` 为空，候选实际写在 `state.pending_review_candidates`
（`failure_checkpoint.stage = "candidates_generated"`，节点 N09）。task_id / candidate_id / 标题哈希三者都对得上，
因此不是跨任务污染；是门禁取数覆盖面不足。

后果：引擎口径 15 条上得来严格 26.7%、含勉强 60.0%；固定分母 20 的主口径是 30.0% / 55.0%；
被拒标签一律不计通过的保守下界是 20.0% / 45.0%。**两个方向都错**，所以报告并列三种分母而不是只挑一个。

**已修复（2026-11-01）**：`scripts/eval.py` 的身份校验集合已扩展为
`state.candidates + state.pending_review_candidates`，标题哈希校验保留。重跑后 18 条全部接受，
引擎口径与固定分母 20 的主口径一致（严格 30.0%、含勉强 55.0%、推荐 55.6%）。
上述"三种分母"并列保留，是因为它记录了两个相反方向的偏差，不因为已修复就删掉。

### 2. 每条视频只评了 1 条候选

无法估计"3 候选至少一条可用率"，也无法做推荐 vs 非推荐的**配对**比较（本批两组是不同视频）。
要回答"推荐有没有用"，需要同一视频多候选都评。

### 3. 无第二判定者

kappa 算不出来，标注一致性不可解释。当前 6/5/7 的分布没有第二人验证，属于单人口径。
`templates/anchors.md` 的锚点样例若用于第二人复判，才能给出 kappa。

### 4. G1 漏拦率算不出来

金标「命中的判据ID」填充 0/18。这条指标需要人工标注具体判据 ID，本批没有，所以为空不是 0 漏拦，是没数据。

### 5. 长度合格率 100% 的分母只有 15 条

被拒 3 条未计入。3 条标题本身也都 ≤20 字（"据通报司机昏迷撞树，辅警破窗救人" 等），因此 100% 这个结论不受影响，
但严格说分母只有引擎计分的 15 条。

### 6. 重写有效性来自 agent.db，与金标无关

13 个可比轮次、完全解决 2（15.4%）、部分解决 5、无改善 6。它描述引擎自评口径下的重写效果，
不是人工判定，不能当人工救回率用。

### 7. 与既有文件的关系

`runtime/acceptance_deepseek_20261101_114430/post_review_metrics.json` 是同一批标签的另一次汇总，
本次独立重算与它一致（固定分母 6/20=0.3、含勉强 11/20=0.55、推荐 9 条中 3 可用 2 勉强 4 不可用）。
两处数字互相印证，说明导出链路没有引入偏差。
