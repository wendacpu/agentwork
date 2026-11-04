# 模型选型执行手册（20 条起步 · A 生成 / B 评审）

> 版本 v3.0 · 2026-11 · **执行层**。候选、口径、决策规则以 [model-selection-plan.md](model-selection-plan.md) 为准；本文只写「怎么做」。
> 读者：下一个执行对话的 Agent。第一段半天做完，第二段可选。

## 0. 一句话

**第一段**：B0 快筛（10 分钟）定裁判 → smoke 3 条 → 20 条 × 3 候选 + 参考臂横评 → 人工 240 条（可先判 120 条）→ 出生成冠军。
**第二段（可选）**：从冠军 state 重跑 R01，比 4 个评审。

```
§5 B0 快筛（先做，10 分钟，<¥1）──┐
§6 smoke 3 条 × 3 候选 ──────────┤
§7 横评 20 条 × 4 系统 ───────────┼→ §8 人工判定 → 结论
                                  └→ §9 第二段（可选）
```

**顺序约束**：B0 必须在 §8 人工判定前出结果——V4-Pro 是 A 的裁判，裁判不合格则 A 的结论作废（plan §3.1）。

## 1. 读前须知

1. 不改 prompt / 规则包 / rubric。只允许改 `model_provider` / `generation_model` / `review_model` / `request_extras` / `output_dir`。
2. 不为了跑通调 prompt。某模型 JSON 坏得多，那是它的结论。
3. 样本固定 20 条，不扩不换（`templates/eval_manifest.json`）。
4. 单价是 2026-11 定价页快照，写进 `templates/model_prices.json`。

## 2. 前置

### 2.1 密钥迁移（硬前置）

**✅ 已于 2026-11-02 完成。** `SILICONFLOW_API_KEY` 原在 `.secret.env`（少一个 s），
[scripts/run.sh](../scripts/run.sh#L18) 只 source `.env` + `.secrets.env`，因此脚本读不到它。
现已并入 `.secrets.env` 并删除旧文件，四个 key（SILICONFLOW / DEEPSEEK / JUFLOW_API_KEY / JUFLOW_BASE_URL）均验证可加载。

**更正一处旧说法：不存在泄露风险。** [.gitignore](../.gitignore) 第 9 行本来就忽略 `.secret.env`，
且该文件从未被 git 跟踪（`git ls-files` 无记录）。旧表述「可能被 `git add .` 提交」是错的。

### 2.2 余额

第一段最多 ¥73（裁判用 V4-Pro）、最少 ¥27（用 V4-Flash）。**余额 ≥ ¥100 就够跑完第一段**；两段都跑按 ¥150 备。

## 3. 代码改动

### 3.1 加 provider（4 处）

[agent_system/cli.py](../agent_system/cli.py#L33)、[scripts/model_review.py](../scripts/model_review.py#L24)、[scripts/baseline_run.py](../scripts/baseline_run.py#L27)、[scripts/verify_constraint_ab.py](../scripts/verify_constraint_ab.py#L34) 各一份 `PROVIDER_DEFAULTS`，都加：

```python
"siliconflow": ("https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
```

（`grep -rn "PROVIDER_DEFAULTS" --include=*.py` 逐个核对，别漏。）

### 3.2 adapter 支持「按模型注入请求参数」

**实测：思考开关是厂商特定的**

| 系列 | 正确写法 | 无效写法 |
|---|---|---|
| Qwen3 / 3.5 / 3.6 / 3.8 | `{"enable_thinking": false}` | `thinking`、`chat_template_kwargs`（不报错，静默失效） |
| DeepSeek V4 系 | `{"thinking": {"type": "disabled"}}` | — |
| GLM-5.3-Flash | **无解**（5 种参数全试过，reasoning 仍有 70–360 tokens） | 全部 |

改动 1：[agent_system/config.py](../agent_system/config.py#L37) `RunConfig` 加字段（接在 `extra` 之后）

```python
request_extras: dict[str, dict] = field(default_factory=dict)   # {model_id: {extra_payload}}
```

改动 2：[agent_system/model.py](../agent_system/model.py#L157) `OpenAICompatibleAdapter`（payload 在 #L169 构造）

```python
def __init__(self, base_url, api_key_env, model, timeout=300, reasoning_effort="", request_extras=None):
    ...
    self.request_extras = request_extras or {}

# complete() 里，payload 构造完、发请求前：
if self.reasoning_effort == "off":
    payload["thinking"] = {"type": "disabled"}
elif self.reasoning_effort:
    payload["reasoning_effort"] = self.reasoning_effort
payload.update(self.request_extras.get(self.model, {}))    # 显式配置优先
```

改动 3：[agent_system/cli.py](../agent_system/cli.py#L48) `make_pipeline` 两个 adapter 都传 `request_extras=config.request_extras`。

### 3.3 三个脚本

| 脚本 | 用在哪 | 规格 |
|---|---|---|
| `scripts/build_review_bench.py` | §5 B0 | 从 `cases` 表拼 48 条评审基准 |
| `scripts/cost_report.py` | §7、§12 | 扫 `agent.db` 出成本/耗时表 |
| `scripts/review_ab.py` | §9 第二段 | 从 state 重跑 R01，只换评审 |

**不改 `eval_compare.py`**（被 eval-spec 引用，别动）。

## 4. config 清单

### 4.1 生成（3 个 + 参考臂）

以 GLM-5.3-Flash 为例 `config.sf-glm53-flash.json`：

```json
{
  "asset_root": "/Users/wanting/program/新项目/复杂agent/recent-portrait-100",
  "output_dir": "runtime/sf-glm53-flash",
  "model_provider": "siliconflow",
  "generation_model": "zai-org/GLM-5.3-Flash",
  "review_model": "deepseek-ai/DeepSeek-V4-Pro",
  "request_extras": {
    "deepseek-ai/DeepSeek-V4-Pro": {"thinking": {"type": "disabled"}}
  },
  "prompt_pack_version": "red-dot-v0.2-modular",
  "ocr_mode": "filtered",
  "target_count": 3,
  "max_chars": 20,
  "max_rewrite_rounds": 2,
  "max_rewrite_candidates": 3,
  "memory_call_limit": 6,
  "reasoning_effort": ""
}
```

| config | generation_model | request_extras（该模型） | output_dir |
|---|---|---|---|
| `config.sf-glm53-flash.json` | `zai-org/GLM-5.3-Flash` | 无（关不掉） | `runtime/sf-glm53-flash` |
| `config.sf-qwen35-122b.json` | `Qwen/Qwen3.5-122B-A10B` | `{"enable_thinking": false}` | `runtime/sf-qwen35-122b` |
| `config.sf-qwen3-32b.json` | `Qwen/Qwen3-32B` | `{"enable_thinking": false}` | `runtime/sf-qwen3-32b` |
| `config.sf-ref-deepseek-flash.json` | `deepseek-flash`（`model_provider: deepseek`） | 用 `reasoning_effort: "off"` | `runtime/sf-ref-deepseek-flash` |

`review_model` 第一段**全部**是 `deepseek-ai/DeepSeek-V4-Pro`（若 B0 判定改用便宜裁判，四个 config 一起换，别只换一个）。

### 4.2 评审（B0 / 第二段用）

`config.review-<slug>.json`：`model_provider: siliconflow`、**`generation_model` 填评审模型 id**（`model_review.py` 用 `generation_model` 调 API，历史遗留）、`request_extras` 按 §3.2。

| slug | model id | request_extras |
|---|---|---|
| `v4pro` | `deepseek-ai/DeepSeek-V4-Pro` | `{"thinking": {"type": "disabled"}}` |
| `v4flash` | `deepseek-ai/DeepSeek-V4-Flash` | 同上 |
| `glm53` | `zai-org/GLM-5.3-Flash` | 无 |
| `qwen35-122b` | `Qwen/Qwen3.5-122B-A10B` | `{"enable_thinking": false}` |

## 5. B0：判别力快筛（48 条，10 分钟，<¥1）

### 5.1 构造基准

`scripts/build_review_bench.py`：

- 从 `runtime/real_run_flash_nothink/agent.db` 读 `cases` 表（63 条）。
- 用 `source_task_id` → `tasks.video_id`，**只保留 video_id 在 manifest 20 条内的**（63 → 48 条）。
- 分三类（写进 CSV，供事后分层）：
  - `硬错误` 19 条：`problem_type ∈ {root_cause_unresolved, 事实与信源}`
  - `软瑕疵` 18 条：`problem_type ∈ {表达, expression:wording_unclear}`
  - `正例` 11 条：`polarity == positive`

输出 `runtime/review/bench-b0.csv`，列名照抄 `gold_skeleton` 的格式（否则 `model_review.py` 读不到）：

```
video_id, candidate_id, 标题, kind
```

**泄题红线**：CSV 里只能有 `video_id / candidate_id / 标题 / kind`。`applicability`、`problem_type`、`case_type`、`trigger_issue_codes` 一律不许出——那是把答案写进题干。`kind` 只用于事后算指标，不进 payload。

### 5.2 跑

```bash
for pair in "v4pro:DeepSeek-V4-Pro" "v4flash:DeepSeek-V4-Flash" "glm53:GLM-5.3-Flash" "qwen35-122b:Qwen3.5-122B-A10B"; do
  slug=${pair%%:*}
  python3 scripts/model_review.py --gold runtime/review/bench-b0.csv \
    --config config.review-$slug.json --output runtime/review/b0-$slug.json
done
```

实测单条 **997 prompt tokens / 3.0 秒**（标题 + 20 条 ASR + 20 条 OCR）。48 条 × 4 模型 ≈ 192 次调用 ≈ 10 分钟、合计 <¥1。

### 5.3 判据（相对比较，不是绝对门槛）

| 指标 | 算法 |
|---|---|
| 硬错误拦截率 | 19 条`硬错误`里判「不可用」的比例 → **越高越好** |
| 正例误杀率 | 11 条`正例`里判「不可用」的比例 → **越低越好** |
| 软瑕疵分布 | 18 条只记录判定分布，**不进门槛**（「表达不清」判可用不算错） |

**决策**：把 4 个模型的「硬错误拦截率 - 正例误杀率」排序。**若便宜评审与 V4-Pro 差距 < 10pp，A 的裁判就用便宜的**（省 ¥46）；差距大就用 V4-Pro。

> 别用「60% 拦截率」这类硬门槛——案例库极性参差（见 plan §8），绝对门槛会误杀所有模型。

产出 `runtime/eval/b0-table.md`。

## 6. smoke（3 条 × 3 候选）

```bash
for slug in glm53-flash qwen35-122b qwen3-32b; do
  python3 scripts/run_manifest.py --config config.sf-$slug.json --limit 3 \
    --output runtime/sf-$slug/eval-smoke.json
done
```

**门槛**：3 条都出结果、≥2 条产出候选、`usage.prompt_tokens > 0`、completion 中位 < 3,000（GLM 除外）、单条 < 3 分钟。

不过 → 查 §14，换候选。**smoke 不判质量**。

## 7. 横评（20 条 × 4 系统 = 80 任务，~40 分钟）

```bash
for slug in glm53-flash qwen35-122b qwen3-32b; do
  python3 scripts/run_manifest.py --config config.sf-$slug.json \
    --output runtime/sf-$slug/eval-v1.json
done
python3 scripts/run_manifest.py --config config.sf-ref-deepseek-flash.json \
  --output runtime/sf-ref-deepseek-flash/eval-v1.json

python3 scripts/cost_report.py --runs runtime/sf-* --out runtime/eval/cost-a.md
```

并行 2 路即可（别全开，避免 429）。

## 8. 人工判定（240 条，40–60 分钟）

```bash
for slug in glm53-flash qwen35-122b qwen3-32b ref-deepseek-flash; do
  python3 scripts/gold_skeleton.py --run runtime/sf-$slug/eval-v1.json \
    --output runtime/review/$slug-todo.csv
done
```

1. **按 video 分组判**（同一视频 3 条候选放一起看），三档锚点照 [eval-spec.md](eval-spec.md) §2.1。
2. 填完另存 `runtime/review/gold-<slug>.csv`，原始 todo 不覆盖。
3. 逐系统转 labels：

```bash
for slug in <列表>; do
  python3 scripts/judgments_to_labels.py --gold runtime/review/gold-$slug.csv \
    --outdir runtime/review/$slug
done
```

**人工量**：20 条 × 3 候选 = 60 条/系统 × 4 系统 = 240 条，约 40–60 分钟。

**省力路径（plan §3.3）**：先只判「最有希望的候选 + 参考臂」= 120 条（20 分钟）。
- 差距 ≥ 3 条 → 直接出结论，剩下 120 条不判。
- 差距 ≤ 2 条 → 再补其余；此时结论本来就是「打平」，补判是确认，不是找赢家。

### 8.1 判定表（按 plan §5 实验 A）

```
Step 1 淘汰：JSON 通过率 < 18/20，或 超长流入率 > 0
Step 2 一次通过率（严格），差 ≥ 3 条分胜负
Step 3 打平 → 端到端耗时中位 → 元/条 → JSON 通过率
```

## 9. 第二段（可选）：B1 系统内 A/B + B2 稳定性

### 9.1 `scripts/review_ab.py` 规格

```
输入：--source-run runtime/sf-<冠军>    --review-model <评审 id>
      --config config.review-<slug>.json    --output runtime/eval/b1-<slug>.json
每个 task：
  state = tasks.state_json
  candidates = state["candidates"]                       ← 冻结，不许重生成
  checks = [{candidate_id, **c["mechanical_checks"],
             programmatic_g1_flags: c["programmatic_g1_flags"]} for c in candidates]   ← 从 state 取，不重算
  payload = pipeline._review_input(video, state, candidates, checks)
  raw = adapter.complete(task="review", system=<R01 SYSTEM>, user=payload, schema="ReviewResultBatch")
  reviews = _normalize_reviews(raw, [c["candidate_id"] for c in candidates])
  reviews, decisions = pipeline._validate_and_decide_reviews(video, state, candidates, reviews, checks)
  route = pipeline._route_candidates(decisions, 0)
输出：每 task 的 route.action + 每 candidate 的 verdict / issue_codes
```

要点：
- `pipeline` 用 `model_provider=mock` 的实例拿方法即可（生成侧不会被调用），review adapter 单独构造。
- R01 的 SYSTEM prompt 从 [agent_system/prompts.py](../agent_system/prompts.py) 取，**不许手写新的**。
- `_review_input` 缺字段时该任务记 `skipped`，计入分母，不许悄悄丢。

```bash
for slug in v4pro v4flash glm53 qwen35-122b; do
  python3 scripts/review_ab.py --source-run runtime/sf-<冠军> --review-model <id> \
    --config config.review-$slug.json --output runtime/eval/b1-$slug.json
done
```

### 9.2 指标

| 指标 | 来源 |
|---|---|
| 放行准确率 | `route.action ∈ {handoff, needs_review}` 的候选 ∩ 人工「可用」/ 放行数 |
| 合格召回率 | 人工「可用」∩ 系统放行 / 人工「可用」总数 |
| kappa | 系统 verdict vs **A 阶段冠军那 60 条的人工三档**（复用，不重判） |
| 归因准确性 | 系统 `issue_code` vs 人工「命中判据 ID」 |

> 复用前提：B1 的 candidate_id 必须与 A 冠军那批完全一致（state 是从冠军 db 读的，天然一致）。

### 9.3 B2 稳定性

同一 config 再跑一次，算 verdict 完全一致的比率。**门槛 ≥ 90%**，低于则 kappa 不可信。

## 10. 组合确认

生成冠军 + 不同家族的评审冠军 → `config.sf-final.json` → 跑 20 条：

```bash
python3 scripts/run_manifest.py --config config.sf-final.json --output runtime/sf-final/eval-v1.json
```

拷成 `config.current.json`，按 [eval-spec.md](eval-spec.md) §4 验收，结论回填 plan §10。

## 11. `scripts/cost_report.py` 规格

- 输入 `--runs runtime/sf-*`、`--prices templates/model_prices.json`。
- 数据：`events` 里 `event_type='model_reply'` 的 `payload_json`（`usage` + `latency_ms`）。
- 价目表（¥/百万 token，2026-11）：

```json
{"models": {
  "zai-org/GLM-5.3-Flash": {"input": 0.8, "output": 2.8},
  "Qwen/Qwen3.5-122B-A10B": {"input": 0.8, "output": 6.4, "note": "输入 <128k 档"},
  "Qwen/Qwen3-32B": {"input": 1.0, "output": 4.0},
  "deepseek-ai/DeepSeek-V4-Pro": {"input": 12.0, "output": 24.0},
  "deepseek-ai/DeepSeek-V4-Flash": {"input": 1.5, "output": 4.5, "note": "谷时"},
  "deepseek-flash": {"input": null, "output": null, "note": "必填：DeepSeek 官方定价"}
}}
```

- 输出列：系统 / 任务数 / 失败数 / JSON 通过率 / 交付候选 / 超长数 / 超长流入率 / 单任务 prompt·completion（均值·中位）/ 端到端 p50·p90 / 元/条。
- **红线**：① 剔除 mock 跑批；② 价目表 `null` 的模型成本留空并 warning，不许按 0 计。

## 12. 汇总

`runtime/eval/model-compare.md`：

```markdown
# 实验 A：生成选型（20 条 · 2026-11）

| 模型 | JSON 通过 | 一次通过(严格) | 一次通过(含勉强) | 超长流入 | 耗时 p50 | 元/条 | 结论 |
|---|---|---|---|---|---|---|---|

生成冠军 = `<model id>`：赢在哪 / 代价是什么 / 什么情况下会翻盘（三句）

# 实验 B：评审选型（B0 48 条 + B1 20 条）

| 评审 | 硬错误拦截 | 正例误杀 | 软瑕疵分布 | 放行准确率 | 召回 | kappa | 自洽率 | 元/条 |
|---|---|---|---|---|---|---|---|---|

评审冠军 = `<model id>`

# 结论
组合：<生成> + <评审>，单条 ¥x.xx，端到端 xx 秒
小样本声明：A 用 20 条、B0 用 48 条案例、B1 用 20 条；≤2 条差异判打平，不宣称统计显著。
```

## 13. 验收

- [ ] `.secrets.env` 有 key，`.secret.env` 已删
- [ ] 4 处 `PROVIDER_DEFAULTS` 都有 `siliconflow`
- [ ] B0 的 CSV 里没有 `applicability` / `problem_type`（泄题自查）
- [ ] B0 出表，裁判选型有结论（用贵还是便宜）
- [ ] smoke 3 个候选过门槛，`completion_tokens` 中位 < 3000（GLM 记录异常）
- [ ] 80 个任务跑完，`agent.db` 未被覆盖
- [ ] 人工判定 ≥120 条（理想 240），`task_id` 无空缺
- [ ] `model-compare.md` 有结论 + 小样本声明
- [ ] `config.current.json` 落盘，跑两次结果一致

## 14. 已知坑（实测）

| 坑 | 现象 | 处理 |
|---|---|---|
| Qwen 系默认开思考 | completion 涨 10–30 倍 | `enable_thinking: false`（顶层，不是 `chat_template_kwargs`） |
| `thinking` 对 Qwen 无效 | 不报错，思考照开 | 只给 DeepSeek 系 |
| **GLM-5.3-Flash 关不掉思考** | 5 种参数都试过，reasoning 仍有 70–360 tokens | 接受；成本按含 reasoning 估；延迟单独记 |
| `V4-Pro` 的 `max_tokens` 下限 | 传 64 报 `{"code":20015,"message":"parameter is invalid"}` | adapter 不传 `max_tokens`，无需处理 |
| 硅基错误结构 | `{"code","message"}` 而非 `{"error":{}}` | 用「有没有 `choices`」判断 |
| model id 大小写敏感 | `qwen/qwen3-32b` 404 | 照抄 §4 |
| `/v1/models` 无价格 | 只有 id/owned_by | 以定价页为准 |
| B0 单条约 1000 token / 3 秒 | 别按 pipeline 的 5 万估 | 48 条 × 4 模型 ≈ 10 分钟、<¥1 |
| 案例库极性参差 | 一条「表达不清」的负例被判「可用」 | 只对 19 条硬错误设门槛，软瑕疵只记录 |

## 15. 中止条件

- 余额 < ¥30 → 停。
- 某候选 smoke 3 条里 2 条 `technical_failed` 且失败在 `N03` → 换候选，重试不超过 3 次。
- B0 里 4 个评审都判不出硬错误（拦截率全 < 30%）→ 停，先人工复核案例库（plan §10 第 3 条），别急着跑 A。
- 人工判定做不动 → 按 §8 序贯路径只判 120 条。
