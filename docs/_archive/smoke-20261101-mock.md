# ① 零成本全链路冒烟记录（2026-11-01）

- 目的：在花任何额度之前，确认新加的主旨锁等改动在运行时不会崩、不会静默零产出。
- 手段：`mock` 适配器（确定性模板，不调用任何付费模型）。
- 隔离：所有产物写在 `runtime/mock_smoke*_<时间戳>/`，未触碰历史批次库（`runtime/acceptance_deepseek_20261101_114430/`、`runtime/agent.db`）与既有评测结果。
- 配置：`config.mock-smoke.json`（本地文件，已 gitignore），provider/model 全为 `mock`，`extra.business_policy` 复制自 `config.deepseek.json`。

## 结论先说

冒烟**没白跑**：抓到一个会让付费批次静默损失 10% 样本的真实缺陷，已修；同时确认了 mock 的能力边界（它走不到交付路径，所以 ② 的钱不能省）。

## 发现 1（已修）：一条 ASR 时间码越界，让整条视频零产出

**现象**：第一批 3 条里 1 条 `候选=0`，路由 `needs_review / REFERENCE_AUDIT_INVALID`，任务是 `pending_human` 但没有任何候选可交给人工。

**根因**：`asr_raw.json` 末段终点比容器时长多出一点点，`evidence_audit._location` 判 `location_invalid`（"时间码超出视频时长"），`_blocking_reference_issues` 不放过它，N05 直接整条阻断。

**影响面**（只读扫描当前 `asset_root` 的 100 条素材池）：

| 项 | 数值 |
|---|---|
| ASR 越界视频 | 13 / 100（13.0%） |
| 越界幅度 | 中位 0.26 秒，最大 21.03 秒 |
| 幅度分布 | ≤0.5 秒 9 条 / 0.5–2 秒 1 条 / 2–5 秒 1 条 / >5 秒 2 条 |
| 评测 20 条样本内 | 2 条（`BV1CFnq68Ezt` 超 0.326 秒、`BV1oxnR6iEpJ` 超 **0.007 秒**） |

**为什么必须修**：`BV1oxnR6iEpJ` 只超出 7 毫秒就被判"证据位置无效"，整条视频产出为零。这是用一处取整换掉全部候选，与代码里已有的口径（"单条陈述的结构瑕疵不该升级成整条视频零产出"）直接冲突。付费批次里它表现为"系统处理不了这类视频"，会被误读成模型能力问题。

**第一次的修法是错的**：先试过"1 秒容差内剪到时长，超出继续阻断"。这是补丁，不是规则——只要越界超过 1 秒（池里有 4 条，最大 21 秒），整条视频照样零产出，而且"多少秒算可容忍"是一个凭空的阈值。

**现在的规则**（`agent_system/pipeline.py`）：
- `LOCATOR_ISSUE_CODES = {"location_unknown", "location_invalid"}` 里的问题**只记录、永不阻断**。时间码是定位元数据，不是信任凭据；它引用了什么才决定可信度，不是它标在第几秒。原材料允许误差。
- 记录落在 `state["reference_locator_notes"]` 与事件 `reference_locator_tolerated`，人工评审时看得到，不是悄悄吞掉。
- 仍然阻断的只剩提案侧的结构错误：编造引用（`dangling_ref`）、ID 重复、引用歧义——那些是模型/AI 提案写错，不是素材脏。
- `agent_system/data.py` 只保留 `_duration_seconds`（时长取素材元数据，与 ffprobe 一致，表格里的 3 位小数是四舍五入值），不做任何"剪齐"。

**验证**：拿池里越界最严重的两条实测——`BV1nBsZ6fEwU`（79.47 秒的视频，ASR 标到 80.3 秒，越界 21.03 秒，11 条 location_invalid）与 `BV1MPnV66ERu`（越界 10.96 秒）：审计仍然报 `location_invalid`，`_blocking_reference_issues` 返回空，**视频不再被判无效**。新增 `tests/test_locator_tolerance.py` 3 条用例（容忍集合、越界不阻断、结构性错误照旧阻断）。

## 发现 2：mock 走不到交付路径（能力边界，非缺陷）

mock 候选标题是"原文标题 + 模板后缀"，因此只引用 `e-title`，被 `SOURCE_TITLE_ONLY_UNVERIFIED` 拦在 handoff 之外；另有 `SIGNAL_SEMANTICS_UNVERIFIED`。

含义：**0 成本的 mock 只能验证 N00–N11 接线、状态落库、各道闸的判定分支，验证不了"交付层"**。真实行为（模型是否产出可用标题、主旨锁是否有效）只能在 ② 用真模型看。

## 发现 3：`gold_skeleton` 只收 eligible 候选

整批都停在 `needs_review` 时，current 臂的 gold CSV 是 0 行，后续 `eval_compare` 会因空交集退出。这是既定口径（未交付即未产出，计入固定分母的失败），**不是脚本坏了**。但实验报告里必须预先写明，否则会被误读。

## 发现 4：配置里没有 approved business policy = 全员待审

`config.example.json` 不带 `extra.business_policy`，此时 `_decide_candidate` 对所有候选给 `needs_review`，整批零交付。真跑必须用带 policy 的配置（`config.deepseek.json` 系列都带 8 条规则）。这是个会静默清零一次付费批次的配置陷阱，已写进 `config.mock-smoke.json` 供复用。

## 回归：测量链没坏

对历史验收批次只读复算（`mode=ro`，未改任何文件）：

- `scripts/eval.py --db runtime/acceptance_deepseek_20261101_114430/agent.db --gold runtime/gold/batch-acceptance-20261101.csv --manifest ...`
- 结果与 `runtime/eval/batch-acceptance-eval.md` 记录逐项一致：严格 **30.0%**、含勉强 **55.0%**、人工一致率 **55.6%**。
- `runtime/eval/check-batch-acceptance-numbers.py` 同样复现 6/20、11/20、5/9。

说明：预注册文档里引用的基线数字，当前代码仍能复算出来。

## 测试

`python3 -B -m pytest -q -p no:cacheprovider` → **335 passed**。

## 下一步

① 完成。可以进 ②（3 条视频真实冒烟，约 30 次调用），但 ②③④ 都要额度，等预算上限。

## 另一个顺手处理的安全项

仓库根目录的 `.secret.env`（124 字节，未跟踪）此前**不在 `.gitignore` 里**——一次 `git add -A` 就会把凭据提交进历史。已在 `.gitignore` 补 `.secret.env` / `*.secret.env`（并确认生效）。文件内容未读取、未打印。

---

# ② 真实冒烟记录（2026-11-01 20:22–21:15）

- 手段：真模型（`deepseek-flash`），批次目录 `runtime/smoke_real*_<时间戳>/`，全部隔离，未触碰历史批次库。
- 累计：19 个任务、206 次调用、3.84M prompt / 0.28M completion → **¥2.67（谷时）～¥5.33（峰时）**。

## 结论

**基础设施层已经跑通：0 条 technical_failed。** 三个视频都跑到了产品决策，不再中途崩掉。

但**本轮没有一条真正交付**，卡在三个产品逻辑闸门上（不再是崩溃）：

| 视频 | 最后卡在 | 说明 |
|---|---|---|
| 二仙桥大爷被拦 | `HIGHLIGHT_RESELECTION_LIMIT` | 看点重选达到上限 |
| 中学共用针头 | `REWRITE_BUDGET_EXHAUSTED` | 3 条候选全部判"需重写"，重写 2 轮后预算耗尽 |
| 萨戈夫赛后采访 | `REWRITE_BUDGET_EXHAUSTED` | 同上，一个视频烧了 9 次重写调用 |

## 发现 5（已修）：评审 JSON 连续非法，重试等于白重试

**现象**：`R01 model_error` 三次，报错位置都在 `Expecting ',' delimiter` / `Expecting property name`，三次都坏。

**根因不止一个**，逐层修：

1. **重试没把断点回给模型**。原实现只回错误码，模型换个位置再坏一次。现在回错误 + 上次输出末 600 字 + 明确格式要求。
2. **未转义引号没有修复策略**。中文理由里直接写 `他说"我不同意"` 就会把 JSON 打断，而报错位置离真坏点几十字符。新增 `_escape_stray_quotes`（字符串内、后面不是 `,` `:` `}` `]` 的引号一律转义）。
3. **失败原因没留档**。新增 `_dump_bad_output` 落盘原始输出到 `runtime/model_debug/`——没有它就只能猜。顺带修了 `ModelError` 被外层 `except Exception` 再包一层、把 `raw` 弄丢的问题。
4. **整批评审坏了 = 三个候选全灭**。新增 `_review_with_fallback`：整批解析失败就退化为逐条评审，坏一条不再等于全灭。

## 发现 6（已修）：边界节点把决策写在信封外面

模型返回 `{"BoundarySet": {摘要...}, "claims": [{... "decision": "usable"}], "semantic_units": [...]}`。解析器原来是"换成内层"，于是拿到 0 条决策 → `NO_USABLE_CLAIM` → 整条视频静默停机（20 秒 + 2 万 token 白跑）。

改成**合并外层与内层、外层优先**；另外补两条兜底：
- 平铺形状的兜底只认真正的 `claim_id`，不再把 `claims_summary` 这种摘要容器当成一条边界（会凭空造出不存在于材料的决策）。
- 一条决策都没有但有陈述时，带着"必须逐条表态"的要求重问一次，而不是直接停机。

## 发现 7（已修）：同一类"时间码"判断写了两遍，只改了一处

`_blocking_reference_issues` 已放行 `location_invalid`，但 `_guard_handoff` 里另有一句 `if i.get("code") != "location_unknown"` —— 同一个口径的第二份实现，于是时间码偏差照样把整个视频判成 `REFERENCE_AUDIT_INVALID`。已改为共用 `LOCATOR_ISSUE_CODES`。

**教训**：同一条规则在两处各写一遍，改一处等于没改。这类重复判定值得后续统一收口。

## 发现 8（未修，产品层）：重写循环在空转

一个视频跑了 9 次重写调用（3 候选 × 2 轮 + 1），15 条候选全部仍是 `rewrite`，标题几乎逐字重复，最后 `REWRITE_BUDGET_EXHAUSTED`。这与历史统计一致（重写占比 21.8% 的 prompt，完全解决率 15.4%）——**重写既贵又基本无效**，是本轮最该动的地方。

## 已修未验证的遗留

`HIGHLIGHT_RESELECTION_LIMIT` 属于产品逻辑闸门（看点重选次数上限），不是崩溃，需要单独判断该放宽还是该改选点策略。

## 测试

`python3 -B -m pytest -q -p no:cacheprovider` → **349 passed**。

