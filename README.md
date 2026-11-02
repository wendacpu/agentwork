# 短视频推荐文案质量优化 Agent

这是一个可替换模型的本地 Agentic Workflow。仓库里的默认配置仍是确定性的 `mock` 适配器（`config.example.json`），用于验证完整状态流转；真实模型链路已在 `deepseek-flash`（关闭思考）上跑通并批量验证，Rubric 仍标记为未校准，不能替代人工质量标准。

实现与证据边界（状态概述沿用 2026-10-31 盘点；测试结果为本次整理实测）：

- 生成、独立评审、程序裁决、人工三档判定（可用 / 勉强通过 / 不可用）已闭环，判定写入 `templates/gold.csv`。
- 标题前缀标注位（`agent_system/labels.py`）：剧情演绎 / AI生成由程序按形态与属性判定，前缀与正文合计 ≤ `max_chars`，前缀不豁免。
- 外部搜索核查（`agent_system/verification/`，节点 N03V）：已接线但**默认关闭**，需 `.env` 里显式开启；当前只覆盖「资讯」域。选择方式分 `off` / `static` / `dynamic` 三档（默认 `static`，即按品类路由），见 `docs/verification-dynamic-routing.md`。
- 测试命令：`python3 -m pytest -q`。信号守卫、反馈修复闭环、身份与评测口径回归实测 **480 passed, 0 xfailed**（原 7 条 xfail 缺陷已在本轮修复并转为正常断言，见 `docs/a-stage-verification.md`）；历史运行与数据快照见 `runtime/`（不入库），不能替代后续重测。

## 快速开始

```bash
# 推荐入口：自动加载 .env / .secrets.env，不把密钥写进命令行
scripts/run.sh audit
scripts/run.sh --config config.deepseek.json batch BV1Ltsf6xEpV

python3 -m pytest -q                                  # 当前实测：480 passed, 0 xfailed
python -m agent_system.cli audit
python -m agent_system.cli run BV1Ltsf6xEpV
python -m agent_system.cli batch --limit 10
python -m agent_system.cli report <task-id> --output runtime/report.html
python -m agent_system.cli rereview <task-id> [--db <批次任务库>]
python -m agent_system.cli human <task-id> <candidate-id> approved --reviewer tiffany
python -m agent_system.cli case <task-id> case-001 expression low_density before after "表达型案例" --confirm
python -m agent_system.cli debug
python scripts/review_queue.py                       # 摊开待审清单（每个视频一条）
python scripts/eval.py                               # 读 gold.csv 出四个数 + kappa
```

默认读取最终 100 条主表，并通过配置中的 `asset_root` 关联原始视频、抽帧、ASR 与 OCR。API 适配器支持 DeepSeek、Qwen 兼容接口和 Kimi；API key 只从环境变量读取（本地放在 `.secrets.env`，已被 `.gitignore` 忽略；用 `scripts/run.sh` 启动会自动加载），不会写入日志。外部事实核查已提供腾讯元宝搜索 WSA Provider（`AGENT_VERIFICATION_PROVIDER=wsa`，密钥放 `WSA_API_KEY` 环境变量；不要写入配置文件或提交进 Git），调用前还需显式启用 `AGENT_VERIFICATION_ENABLED=true`；选择方式用 `AGENT_VERIFICATION_MODE`（`off` / `static` / `dynamic`，缺省 `static`）。

项目数据文件已经统一为英文命名并放在 `data/source/`；`docs/design/` 保存流程设计稿和思维导图。原始视频目录不复制进项目，使用 `config.example.json` 的 `asset_root` 关联。

规则正文按职责拆在 `rulepacks/common.md`、`rulepacks/domains.md` 和 `rulepacks/forms.md`。`agent_system/rules/` 负责规则模型、路由、加载和 Rubric；运行时由 `load_rule_bundle()` 按领域和内容形态选择 U/V/F 规则，并把 `rule_ids`、匹配原因和缺口写入任务状态。理解节点先把整条视频整合成跨片段、跨模态的语义单元（而非逐句 ASR/OCR 拆看点），每个单元聚合其原子陈述与证据并记录描述/意图/上传者说明的交叉印证；P3 看点节点再从语义单元中挑主/备选看点。生成、重写和独立评审共享本次规则切片，但评审不读取生成自评。

标题前缀与正文分开存：`agent_system/labels.py` 按「形态 × 属性」判定必须渲染的平台标注位（当前枚举：剧情演绎 / AI生成），模型自报的枚举外标签一律丢弃；长度按渲染后的完整串计算，前缀计入 `max_chars`（业务方 2026-10-31 定，前缀不豁免），因此 `body_budget()` 会实时扣掉前缀占位。

外部搜索核查（`agent_system/verification/`）在理解之后、边界判定之前以 N03V 节点运行：按领域路由到权威站点白名单，只查可核查的事实性断言，模型把检索结果判为 supported / refuted / unverifiable；`refuted` 且来源数达阈值的断言在边界阶段被强制 `excluded`、所属语义单元降级，并记入 `state.verification_risk`。默认 `verification_enabled=false`，不开启时该节点不产生任何调用。

「查不查」由 `verification_mode` 决定：`off` 停用、`static` 按品类路由（默认，历史行为）、`dynamic` 由理解节点给出建议、程序按「强制条件 ∪ 模型建议」决定。dynamic 下命中 `verification_mandatory_domains` / `verification_mandatory_keywords` 的项**模型无权关闭**；建议解析失败一律回退 `static`，绝不读成「模型说不用查」；点名要查但品类无权威白名单时不退回全网检索。每次判定写入 `verification_decision` 事件并带 `decider`（`off`/`disabled`/`mandatory`/`model`/`static`/`static_fallback`），`static_fallback` 用于统计回退率。强制清单默认为空，需业务确认后配置——详见 `docs/verification-dynamic-routing.md`。

## 独立风险信号（第一期）

`agent_system/signals.py` 独立扫描原始 evidence，记录内容性质、数字、必要限定和时效词的原文位置、来源质量与抽取器版本。明确指向本视频的肯定 AI／虚构声明可补齐前缀，不依赖模型 attributes；否定和话题讨论不直接触发标注。模型衍生 caption 的命中只作待核验线索，存在未解内容性质信号时，生成后不能走普通 handoff，而转 needs_review。虚构不等于 AI。

数字支持中文／阿拉伯数字和万亿元归一化，区分金额、总价／单价、人数、年龄、年份等指标及疑问角色。引用证据未支持的数字陈述进入核验，程序不再以原题为数值白名单，也不恢复已被排除的原题陈述。当前不做实体指代消解和跨单位换算；不同实体同数值的错配仍需语义评审。相对时效措辞须有引用陈述支持，但历史材料中的相对时间尚需进一步验证，不能视作已完成时间核验。

信号会传入理解、生成与评审，写入任务状态；抽取器文件纳入运行指纹。引用材料含否定、范围、条件或不确定限定，而标题缺少同类表达时，保守转语义待审，不按缺词直接判错；相对时间即使出现在引用陈述中，也需人工确认事件日期。重新评审会重算前缀、数字和时间守卫，不复用历史通过结论。当前同类限定词不能证明完整语义等价，仍须独立评审；这不是通用语义证明器。本期不包含案例自动发布生产规则。

「限定是否已表达」按类别判定，不靠一张全局限定词表：时间／范围类看标题里有没有时间落点，不确定类看有没有争议标记，条件类看有没有范围词，数值类看有没有量级词；认不出类别的退回锚点比对，锚点也抽不出来时放行交人工评审。禁令（「不得推断…」）与「须保留／限定为」这类只约束「别改写什么」的限定不要求标题复述——过去把同一 claim 上已被豁免的限定重新摊回正文要求，等于要求标题写出「本条不得推断细节」这种审核话，候选永远过不了闸（2026-11-02 对照批次 202 条候选里 73 条因此被判否，人工判定这些标题可用）。归属限定绑定了具体子事实时（「但病因须归属」）只在该子事实出现在标题里时才要求归属。

边界节点输出跨层合并：模型会把完整 claim 对象放外层、决策分组（`usable_claims`／`limited_claims`／`excluded_claims`，别名含 `allowed_claims`）放 `boundary_set`，或反过来。解析器保留所有层级的完整对象，分组只提供 decision 覆盖，不再用其中一层替换另一层——分组摘要只有 decision，覆盖上去会丢掉 `required_limits`、来源与传播路径。模型对某条 claim 完全没表态时仍按 `excluded` 保守兜底，但记入 `unresolved_issues` 并重问一次，不再静默兜底（静默会让「模型漏答」看起来像「材料真的不可用」）。实测两个因此停机的任务：BV1j6JR6AEAh 由 2 条 excluded 恢复为 1 usable／4 limited／2 excluded，BV13ZHT6AEYW 由 13 条全 excluded 恢复为 11 usable／2 limited。

## 案例回流与共享案例库

案例默认写入 workspace 级共享库 `runtime/cases.db`（`agent_system/config.py: resolve_case_bank`），不再随 `output_dir` 一实验一份。此前每次新实验都从零积累，历史人工判定锁死在各自目录里：实测 282 次检索只命中 13 次（4.6%），同视频命中 0 次，「只存不用」。做隔离对照实验时把 `case_bank_isolated` 置 true，或用 `case_bank` 显式指定路径。

已有实验目录里的确认案例用 `scripts/merge_case_banks.py` 一次性汇入共享库：只读源库、只补不覆盖、跳过 `_legacy`、重复执行幂等，并回填 `video_id`。同视频检索因此不再 JOIN `tasks`——共享库里没有那张表，旧写法会直接 `no such table`。

```bash
python3 scripts/merge_case_banks.py                     # 汇入并回填 video_id
python3 scripts/memory_health.py                        # 回流健康度：命中率、同视频命中
```

`scripts/memory_health.py` 是这一环的观测口：没有它，「经验库到底有没有在反哺」只能靠感觉。修复前的历史记录仍显示低命中，判断效果要看共享库启用后的新运行。

检索按「与当前视频有实际词重合」入选（`MemoryStore.search(min_overlap=1)`），重叠更多的案例排在前面。此前只要打分大于 0 就注入，于是「问题类型标签相同、内容零重合」的案例也会进生成提示词——实测占命中的 18%，全是噪声。2-gram 切分产生的虚词（一个/这个/就是）同样只制造假命中，已在切词阶段剔除。剔除后：零重合命中 18% → 0，实质重合（重叠 ≥2）42% → 55%，仍有案例可用的任务 84% → 83%。重叠只有 1 的残余命中多为领域词（美食/餐厅/做法），是否为有效参考尚无证据，需靠对照实验判定，不按假设先砍。

### Memory 对照实验（两臂）

案例到底有没有让标题变好，只能靠对照实验回答：A 臂读案例、B 臂案例库为空，模型/规则/代码/材料全同，盲评三档。

```bash
python3 scripts/make_memory_ab_snapshot.py     # 生成留一法快照 + 独立复核零泄漏
python3 scripts/run_manifest.py --config config.memory-ab-arm-a.json --manifest templates/memory_ab_directional.json
python3 scripts/run_manifest.py --config config.memory-ab-arm-b.json --manifest templates/memory_ab_directional.json
```

快照必须做「留一」：剔除评测集视频**自己**的案例。实测 D 集 20 条视频贡献了 142 条案例中的 **56 条（39%）**，不剔除 A 臂就是在抄答案。无 `video_id` 的案例无法判断归属，一并丢弃（12 条）。

`templates/memory_ab_directional.json` 是**条件化样本**：只保留 A 臂确实能检索到他人案例的 6 条视频。选样按条件筛选、非随机，结论只对「有案例可用」的工况成立，不能外推。之所以先跑 6 条而不是完整 20 条：D 集多数是资讯类，而案例库集中在美食类，A 臂对其覆盖只有 6/20，完整实验会有 14 条两臂完全相同、稀释到测不出差异。先花小成本判方向，方向为零就停。

生成与评审分开：`config.memory-ab-review.json` 案例库隔离，判定只写本批任务库，不污染 A 臂快照。

## 反馈驱动修复闭环

新人工判定由 `Storage.record_human_decision` 在同一事务写入 `repair_outbox`，提交后自动处理为缺陷、证据归因假设与修复草案；撤回或改判使旧修订失效。失败事件持久保留，可重试，不依赖案例检索。每个任务库有自己的账本；历史记录可只读汇总到中央账本。`approved` 原样通过为正例，`rejected`、`marginal`、编辑后通过为待处理缺陷，不会直接变成正式规则。

```bash
python3 -m agent_system.repairs backfill --db runtime/real_run_300/agent.db --db runtime/real_run_flash_nothink/agent.db --db runtime/review_batch_001/agent.db
python3 -m agent_system.repairs list
python3 -m agent_system.repairs drain
python3 -m agent_system.repairs analyse
python3 -m agent_system.repairs group
```

`group` 把逐条提案按根因族聚成少数几组（实测 88 条 → 6 组，按覆盖视频数排序）。逐条审 88 个提案不会发生；族内根因相同，修一处覆盖一批，`review` 只需对代表案例给出修复与反例。

`review` 命令需明确填写根因、修复、回归测试、反例与验收条件。`repair_release` 的 validate / verify-cases / approve 分别完成真实全量 pytest、逐提案人工案例审核与人工发布确认，详情见 `python3 -m agent_system.repairs --help` 和 `python3 -m agent_system.repair_release --help`。提案标识为 `feedback_key@revision`，草案或已撤回修订不能验证发布。审批绑定源码和测试指纹，JSON 篡改、源码漂移、缺人工案例审核、测试失败或跳过均拒绝批准。此处发布是批准当前本地源码版本，不会自动打补丁、部署或推送。

生产运行可显式配置 `extra.release_gate_dir` 和 `extra.repair_ledger`：配置门禁后必须有有效 active 批准，启动时拒绝漂移版本和已改判提案；不配置时保留开发运行方式。中央账本审批及启动检查会同步已登记源库，源库不可读即失败，不把旧提案继续当有效。人工审核为本机显式确认，不是身份认证；能任意改 SQLite 或运行环境的本地管理员不在防篡改边界内。

本轮历史回填112条判定，88条待归因、24条正例；112条重复回填处理0条。归因是可审查假设，不是已确认修复；本次没有批准生产发布。

## 评测口径与身份契约

`scripts/eval.py` 出的数要按下面前五条读；缺一条就不是验收结论：

- **严格率只数「可用」**：一条视频至少 1 条候选被判「可用」才算通过；「勉强通过」不并进严格率，单列为「含勉强通过」一档，两档分开报（`scripts/eval.py:261-266`）。
- **推荐准确率的分母是「已评价的系统推荐」**：只数人工真的评过的那条系统推荐，不是有推荐任务下的全部人工标签（`scripts/eval.py:270-272`）。
- **系统推荐取不可变快照**：首次人工判定时冻结 `original_system_route`，之后的驳回/改判只缩小可审范围，不改写推荐本身；否则推荐准确率会变成用人工改过的结果证明系统自己（`agent_system/storage.py:233`、`scripts/eval.py:237`）。
- **金标必须带 task_id，候选必须存在于该任务**：同一视频被多个版本跑过时，缺 task_id 直接报 ambiguous、拒绝计分；task_id 对不上或 candidate_id 不在该任务候选中，按「身份不一致」拒绝，不计成功（`scripts/eval.py:225-231`）。
- **扫全库的结果不能当验收结论**：不传 `--db` 会扫 `runtime/**/agent.db`，把多个版本的任务混在一起，脚本会显式警告；正式验收必须显式传批次任务库（`scripts/eval.py:203-208`）。

长度合格率同样只算被人工评价的那一条候选，不把整任务候选重复计入（`scripts/eval.py:245`）；判定口径与身份校验的回归见 `tests/test_identity_and_eval.py`。

## 理解节点为空时不许下游替它编造事实

理解提示词原先把 `candidate_claims` 描述成「语义单元内的原子事实层」，模型据此只在 `semantic_units[*]` 里写 `claim_ids`、整批省略 claim 对象；解析端只认顶层 `claims`/`candidate_claims`/`facts`，于是得到 **0 条 claim** 却照常记录 `claim_count=0` 继续往下走。边界节点拿到空 claim 集合后**自己编了一套 claim_id**（c1..c33），引用审计再判定它们全部悬空，把每条陈述都归为 `excluded`，整条视频最终停在 `NO_USABLE_CLAIM` / `PENDING_VERIFICATION`——一个把人引向「引用不全，按不可写处理」的**误导性死因**，而真实原因是理解节点空转（2026-11-01，BV1WaHN6GEE1 与 BV1Kcso6KEdr 同时命中）。

现在的三处约束：

- **解析端兜底**：`_claims_nested_in_units` 把嵌在语义单元里的 claim 对象提上来，能救回的输出去救；顶层已有 `claims` 时不启用兜底，避免两份陈述混在一起。
- **理解节点快速失败**：单元声明了 `claim_id` 却没有任何 claim 对象定义它们时，先带着「必须在顶层输出 candidate_claims 数组」的要求**重问一次**（事件 `understanding_claims_missing`）；仍拿不到陈述就停在 N03，以 `UNDERSTANDING_CLAIMS_MISSING` 结束，**不再调用边界节点**替它编造事实。
- **提示词去掉歧义**：明确要求**顶层** `candidate_claims` 数组（与 `semantic_units` 同级），并禁止「只写 c1、c2 这类 ID 而不给出对应 claim 对象」。

回归见 `tests/test_understanding_claims.py` 与 `tests/test_pipeline.py` 的两条管线级用例（断言重问一次、边界零调用、停机理由正确，以及嵌套输出被救回后流程继续）。

## 核查请求的瞬时网络故障要重试，不许打死整条任务

本机 TUN 代理下实测：同一域名连续握手约一半概率拿到 `SSL: UNEXPECTED_EOF_WHILE_READING`。旧实现的 WSA 请求一次失败就抛 `RuntimeError`——它没有 `retryable` 属性，上层 `getattr(exc, "retryable", False)` 得到 `False`，于是这次抖动被记成**不可重试技术失败**，`batch` 还会因此中断整批（事件 `batch_stopped`）。同一故障在模型调用上被自带重试救回，在核查节点上却直接判死，口径不一致。

现在 `WsaSearchProvider._http_post_json`：

- **瞬时故障重试 3 次**（`URLError` / `TimeoutError` / `SSLError` / `OSError`），退避 1.5s×(次数+1) 并带抖动，避免并发核查同时重试再撞一起；
- **凭据与请求错误不重试**（400/401/403/404/422 直接抛 `ModelError(retryable=False)`），限流与服务端临时故障（408/429/500/502/503/504）才重试；
- **重试用尽仍失败时标 `retryable=True`**，错误信息注明疑似本机网络或代理链路，让上层决定续跑而不是把任务判死；
- HTTP 错误详情仍不含请求头与密钥。

回归见 `tests/test_verification.py` 的 `WsaTransportRetryTests`（重试后成功、耗尽后标可重试、4xx 不重试、5xx 重试、错误详情不含凭据）。

## 纯机械缺陷走定向修复，不与人评缺陷抢预算

三条 canary 暴露出一个倒挂：五条候选只因**超 1–2 个字**被判 `repairable: True`，但 `_route_candidates` 见到任意一条候选 ready 就直接 handoff，`max_rewrite_rounds` 一次都没用上——被判「可修」的候选被原样丢弃。反过来资讯那条一条都不合格，语义重写用满 2 轮也修不动缺归属的问题。

两类缺陷被混在同一个开关下：**超字数**是目标确定的机械缺陷（压到预算内，改法唯一）；**缺归属、看点跑了**是语义缺陷（要重新判断写什么）。现在分开：

- `_compressible_defects` 只认长度与问号结尾，**且要求 G1/G2/G3 全 pass、没命中业务政策**。任一语义闸未通过就不进这条通道——否则「压缩」会变成偷偷改题；命中 P9 这类禁用词的候选必须换词而不是砍字。
- `_repair_mechanical_pass` 在「有没有候选合格」判断**之前**执行，按候选独立计数（`max_mechanical_repairs`，默认 1），不占用也不抢占 `max_rewrite_rounds`；压不动就停，不反复掷骰子。
- 压缩请求给出确定信息而非让模型自估：`length_repair` 里写明超出多少字、正文预算多少、当前正文多少字。提示词新增压缩模式，规定删词顺序（先删自造分类标签与冒号前缀，再删修饰与同义重复），并禁止为省字数删掉必要归属、限定、否定、范围。
- 修复动作落 `N09 mechanical_repair` 事件，含父标题、缺陷码、修复后标题，可追。

实测选择结果（2026-11-01 三条 canary 的真实状态）：剧情 `cand-2`/`cand-3`、科普 `cand-3` 进通道；资讯三条因 G1 未 pass **全部不进**——这正是期望的边界。回归见 `tests/test_pipeline.py` 的三条用例（有 ready 候选时仍修、归属缺陷不进压缩、任一闸未 pass 即拒绝）。

## 两套独立主旨解读不一致时不推荐，但不替谁翻案

生成侧 N07 与独立复核侧 R00 各自读同一条视频、各出一份主旨契约。真实故障（2026-11-01 canary，`BV1PwsR6oEKW` 剧情）：

| 节点 | 读出的主旨 |
|---|---|
| N07 | 女生夜里给男生送吃的、叮嘱他好好休息，男生体贴回应 |
| R00 | 女生睡过头错过约定，男生体贴不追究 |

两套契约都传给了 R01，提示词也写明「不一致就标 needs_review」，但 R01 只拿**生成侧**契约核对标题（理由是「与视频 ASR 呈现的关心互动一致」），程序侧又没有任何跨契约比对——`validate_highlight_alignment` 只对齐「看点 vs 单份契约」。于是主体被读反的那条候选顶着「系统推荐（已交付）」交到人工面前；人工判定正是那处：「送吃的和叮嘱的主体都是男生，被叮嘱的是女生」。

根因不是谁读错，而是**材料里并存两条线索**：`e-description`（模型对画面的描述：长发女生递花束给窗内男生）与 `e-caption`（模型推断：闺蜜睡过头、男生不追究）互相矛盾，两条都是 `quality=model_derived_uncalibrated`，`derived_from` 又都是空——系统既没检测这份矛盾，也无法追溯推断链。两个节点各抓一条线索，都很忠实。

现在 `agent_system/thesis.py` 的 `contracts_disagree` 在交付前比对两侧 `core_promise` 与 `required_semantics`：

- **两个指标都低**才算分歧。只堵一个会误伤「核心承诺一字不差、只是必保语义换了说法」的详略差异。阈值 `0.35` 不是调出来的，见下方校准。
- 命中且路由本为 `handoff` 时降级为 `needs_review`、清空 `recommended_candidate_id` 与槽位交付，原因记为 `THESIS_CONTRACT_DISAGREEMENT`；本来就没在推荐的不改写既有 reason，只留痕。
- 落 `Q01 thesis_contract_disagreement` 事件，含两套读法与两侧证据可靠性分布（`source` / `unverified_source_metadata` / `raw_*` / `model_derived_uncalibrated` 四层）。
- **不按任何一侧自动重写。** 两侧站在同样硬的地基上时（本例即如此：两侧都建在未校准的模型推断上），按谁重写都只是把一次猜测换成另一次猜测。
- 评审台并排显示两套读法与「系统不推荐任何一条」，候选标签改为「系统通过（解读有分歧，未推荐）」，不再读成「系统推荐（已交付）」。

**阈值校准（`scripts/contract_disagreement_scan.py`，离线零成本，51 任务 / 11 视频）**

| 阈值 | 触发任务 | 触发视频 |
|---|---|---|
| 0.20 | 7（13.7%） | 4（36%） |
| **0.35** | **14（27.5%）** | **6（55%）** |
| 0.50 | 21（41.2%） | 7（64%） |

0.20 会**漏掉它要抓的那条真实故障**（canary 剧情 `task-8711453e0f15` 的 overlap=0.333），所以取能抓住它的最小值 0.35。

**已知代价（诚实记录）**：词法指标分不清「同义改写 / 一侧更详尽 / 互斥读法」。按人读定性，0.35 命中的 6 个视频里约一半是详略差异（如 `BV19AHd6PEAP` N07 写「从功能到饰」、R00 写「从功能走向配饰化」，同一意思）。更关键的是**目标与误报在数值上重叠**：

| 人读定性 | overlap | promise |
|---|---|---|
| 真分歧（剧情） | 0.333 | 0.154 |
| 详略（妆容） | 0.333 | 0.117 |
| 详略（小包装） | 0.312 | 0.146 |

同一个 overlap 值，一个是真分歧一个是同义详略——**没有阈值能分开**。

**所以判据是两颗：词法当筛子，语义当判官。** 词法只在触发时买一次语义比对（两侧措辞一致时直接跳过，不花钱）；判官说 `aligned`/`detail` 就**撤销**词法的告警，说 `contradiction`/`missing_point` 才降级。判官不可用时按保守口径降级。

**判官值不值这次调用——离线实测（`scripts/reconcile_probe.py`，46 对已存契约、15 秒）：**

| 判据 | 判「互斥」 | 命中真实故障 | 误报 |
|---|---|---|---|
| 词法 | 14 条 | 2 条 | **12 条（86%）** |
| 语义判官 | 2 条 | **2 条** | **0** |

按人工读法 19 条确定样本，判官对上 18 条（95%）；它还纠正了我自己读错的一条——`BV1WyHV6KEui` 我怀疑互斥，判官给出「两份都主张低俗感不由单件决定，要看整体搭配」，读得比我准。它还能说清**漏了哪条要点**，词法只能说「低重叠」。

判官的判断有两个来源：R01 的任务级字段 `thesis_consistency`（R01 本来就同时拿到两份契约，这是零额外调用的位置），以及专职比对调用 `_reconcile_contracts`——后者是判官，前者只作为额外的触发线索。离线脚本复用 `contracts_disagree` 同一判据，避免两处口径漂移。

回归见 `tests/test_contract_disagreement.py`（词法边界、证据分层、handoff 降级、既有 reason 保护、开关、端到端落库、评审台并排展示）。

## 运行结果边界

每次任务会写入 SQLite（`runtime/agent.db`）和事件日志。候选必须经过证据引用、长度、看点引用、限定表达（引用 `limited` 陈述时标题必须写出「据…/称/网传」等限定，否则报 `G1_MISSING_LIMIT` 强制重写）和元信息表述（禁止「作者称/字幕称/口播称/旁白称/标题称」这类框架，否则报 `G4_META_PHRASE` 强制重写）检查；评审结果会标记 `uncalibrated`。只有人工确认的 Case 才能进入 Memory。

限定表达与来源归属用**同一套判据**：`G1_MISSING_LIMIT` 早年自带一份词表（据/称/网传/疑似/报道…），`G1_SOURCE_ATTRIBUTION_MISSING` 用的是 `_ATTRIBUTION_MARKERS`（另有 受访/讲述/坦言/回忆/提到/补充/解释）。两份不一致时，「黄多多自述：…」这类标题会同时被判「有归属」和「没归属」，六个候选全灭、整条视频零交付。现在 `required_limits` 先按 `_limit_is_attribution` 分成归属型与其它型（范围、争议、时效）：归属型只认 `_has_explicit_source_relation`，其它型仍认 `limit_phrases`，两类分别通过、都不可互相顶替——否则「据网友称」就能顶掉「仅部分」这种范围限定。

理解阶段可输出事件时间线与语义单元；非叙事视频无需编造事件。反差不是默认优先项，事件两端各有引用不代表关系已证明；当前未建立可核验关系证据契约，程序不自动构造 turn 或事件拼接 main_axis 结构候选，原始事件线索仍供独立评审。语义关联和主旨仍需独立评审。

引用审计记录证据来源、悬空引用和时间定位；审计不证明文本语义或外部事实。缺失或非法 `grounding` 保留为 `unknown`。当前文本模型不接收画面像素，帧路径和衍生画面描述不能单独证明动作或关系。C22 对只引用描述/路径的陈述标记待核并排除出可写集合，语义单元若依赖这些陈述亦不可作为已确认来源；只引用原题的候选转人工待审。ASR/OCR 引用本身也不证明其语义支持标题——所以 `semantic_use`/`grounding` 不再只由模型自报：没有 ASR/OCR 支撑的单元声明 `observable_fact` 会被降为 `unsupported_inference`，连原题都没有支撑的单元声明 `grounded` 会被降为 `unknown`，改动记入 `semantic_use_bindings` 并写入事件，初次生成与重新评审都重算。这只是把「语义自证」压缩成「结构性可判」，语义真假仍由独立评审判断。

评审使用 G1–G4 与 Q1–Q3。业务 Gate（G2）只有在配置 `extra.business_policy` 且 `status: "approved"`、有版本和非空 `rules` 时才参与自动通过判断；缺政策时 G2 恒为 `needs_review`。

`rereview` 只重判既有候选，不重新生成标题：它把原任务状态复制进 `runtime/<output_dir>/rereviews/<唯一目录>/agent.db` 再裁决，原库保持只读（CLI 走 `mode=ro`）。隔离库冻结每个候选的 `candidate_id`、正文与完整标题哈希，重评前后各校验一次，任一漂移即失败；同时记录源库路径、视频快照哈希、生成/评审模型与提示词版本。「是否隔离」按目录结构判定（库必须位于 `<output_dir>/rereviews/` 下），不靠「表存在」自证——否则往原库手插一张 `rereview_snapshot` 就能让重评把事件写回原库。重评中途失败会删掉半成品隔离库，不留无主残留。载体（视频/旁白/字幕/画面）不是信源：剧情演绎类视频的叙述属性由【剧情演绎】前缀声明，正文再写「旁白：」按 G4_META_PHRASE 判否；边界不得对纯旁白内容要求 attribution_required，也不得整批置 verification_required（F09 属 U-S08 豁免档）。模型把结构化输出包一层信封（`{"type":"json_object","content":…}` 或 `{"BoundarySet":…}`）时由 `_unwrap_envelope` 拆开，避免理解/边界静默零输出。缺视频快照、缺候选或重写预算已用尽时明确停下，不补造候选、不重新打开重写循环；交付槽位与初次生成共用同一套 `_slot_delivery` / `_apply_slot_delivery`，降级时清空旧的「已交付」清单。

**多段并列视频只能有一个主线，取开头那段**：由几段互不相干的故事/案例拼成的视频，若把「合集」当主旨，`core_unit_ids` 会并列多段，`core_hook` 就会拿尾段最猎奇的那个去生成标题。实测：装死视频（0–23.4 秒）与铁锹绑RPG（无 ASR 证据的尾段）并列成核心，标题变成「铁锹绑RPG弹头破层深爆」。现在 N07 后按 ASR 证据反推各核心单元的时段，`core_unit_ids` 超过一个且起点相差 ≥ 8 秒时只保留开头叙事段，其余降为支撑（记录在 `mainline_demoted_units`）；完全无时间证据的单元不得与有证据的主线并列。同时 `core_hook` 槽必须绑定主看点，换成其它片段记 `G4_HOOK_OFF_MAINLINE`。

**零产出闸门按「能救就救」处理**，不再让一处结构瑕疵换掉整条视频：

- 边界节点若按 `usable_claims`/`limited_claims`/`excluded_claims` 分组给 ID（而不是逐条决策对象），归一化会还原成逐条决策；同一陈述出现在多个分组时取最严。曾经这种情况被读成 0 条决策，整任务判 `NO_USABLE_CLAIM`。
- 语义单元引用了不存在的 `claim_id` 时只剪掉悬空引用；剪空的单元判 `excluded`，其余继续生成（`reference_pruned_units` / `reference_excluded_units` 留痕）。只有非单元级的结构问题才阻断整任务。
- 模型输出被 `max_tokens` 截断时，先用完整原文补齐括号与未闭合字符串（含截断在 `{"subject":` 这类位置补 `null`），并保留尾部对象；截断不再等于整任务失败。

三槽是当前唯一受支持的交付契约：新任务在 `prepare()` 直接拒绝其它 `target_count`；已有任务（含 resume）在 `_execute` 停下并记录 `TARGET_COUNT_UNSUPPORTED` 与冲突值，不产出少槽候选。

`config.business-policy.json` 是已批准政策（`policy-v0.2`，9 条），每条规则带 `action`，决定违规后的处置，而不是一律涌向人审：

| action | 后果 | 典型场景 |
|---|---|---|
| `reject` | 直接驳回，不进重写 | 未成年人性化、可模仿的危险做法、隐私暴露、营销导流 |
| `rewrite` | 进重写循环，修好可发 | 低俗擦边措辞、伤亡细节渲染、群体贬损标签、人身攻击用语 |
| `review` | 必须人看，不自动放行 | 医疗/金融/法律结论、灾害伤亡具体信息 |

多条命中取最严（reject > review > rewrite）。规则没写 `action` 时保持旧行为（`reject` 需引用政策规则才真的驳回）。

**「违反哪条」与「检查过哪条」分开算。** G2 的 `rule_ids` 里既有模型判出的违规规则，也有它顺带列出的自查清单（常常是 P1–P8 全列一遍）。处置动作只取「被违反的」那几条（`violated_rule_ids` 加模型自己判出 G2 问题时列出的业务规则），否则一条 P9 会被清单里的 P1（`reject`）顶成判毙。

**P9 人身攻击用语是唯一由程序侧补证的规则。** 它不另开评审维度，就落在 G2 业务安全底线上、与 P1–P8 共用同一张 `action` 表：`_mechanical_check` 只产出「标题里确实出现了这批词」的事实证据（`policy_hits`），`_merge_program_policy_hits` 把它并进 G2 并把状态从 `pass` 拉到 `revise`（不覆盖更严的状态），处置动作仍由 P9 的 `action` 决定。之所以需要程序这一路：实测 9 条历史候选里，模型每次都因为「那只是转述网友的话」把 G2 判成 pass，其中 2 条已进入 `ready_for_human_review`。词表在 `agent_system/abuse.py`，只收没有正常义的词（垃圾、废物、白莲花、装备、装死都不收），否定结构（「她不欠扁」）不命中；命中政策的候选不进压缩通道（压缩只砍字、不会换词），走语义重写并带上 `forbidden_terms`。

材料不足时另有输入质量闸：`data.py: assess_material` 按一手证据分 `high/medium/low`（ASR 段数 + OCR 中文占比），`low` 时路由强制降级为 `needs_review`（`INPUT_LOW_CONFIDENCE`），配置 `extra.stop_on_low_quality_input` 可直接停在 `INSUFFICIENT_MATERIAL`。理解节点同时记录 `source_evidence_usage`——有没有陈述引用 `quality=source` 的原题，避免把唯一一手文本丢掉。

## 单任务调试台

运行 `python -m agent_system.cli debug`，然后在浏览器打开终端显示的本机地址（默认 `http://127.0.0.1:8765`）。调试台一次启动一条视频任务，页面先显示候选标题和人工判断，再展开流程、输入证据、规则和事件。人工评审只选择「可用 / 勉强通过 / 不可用」并说明标题瑕疵；点「驳回」时判定结果固定为不可用，只需选一个「驳回原因」（事实错误 / 关键背景缺失 / 没写到核心看点 / 句子不完整 / 用词读不懂 / 其他）并写一句话理由，原因与判据都可不填，由 AI 结合证据与流程记录归因。驳回原因会单独落到 `gold.csv` 的一列，便于统计主要失败类型。判定会同步写入 `templates/gold.csv`（按 video_id + candidate_id 覆盖）；已提交判定可撤回并重评，撤回会移除对应金标准行。`pending_human` 与 `stopped` 两种状态下的候选都可以判——重写耗尽而停下的任务里，候选同样是真实的。调试台默认扫描 `<output_dir>/*/agent.db` 下所有任务库（每次实验一个目录），需要指定时用 `--db-path`（可重复）。候选卡展示的是渲染后的完整标题（前缀 + 正文），字数按同一口径计算；编辑后通过时提交的也是完整串。Prompt 区展示 Prompt 组装记录、源文件常量和哈希，方便定位应该修改 `agent_system/prompts.py` 中的哪个节点；注入的规则正文可在规则区查看，对应源文件为 `rulepacks/*.md`。`mock` 模式下 Prompt 不会被大模型消费，候选只是确定性占位输出，不能用来判断 Prompt 效果。历史任务缺少 `prompt_snapshot` 时无法还原当时的完整输入。

每个模型调用会记录请求指纹、Prompt 快照、模型/节点耗时、重试和结构化结果。网络或临时服务错误可在任务页续跑，成功模型响应会按请求指纹复用；HTTP 402 等不可重试错误不再重复调用，CLI 批处理也会停止在首个不可重试故障。若 Prompt、规则或流水线代码已变化，续跑会拒绝混用新旧版本，需用新任务验证。

## 冻结批次人工评审台

已有批次含 `review_mapping.json` 和 `agent.db` 时，可用独立人工评审入口：

```bash
python3 -m agent_system.batch_review --batch-dir runtime/acceptance_deepseek_20261101_114430
# 打开终端显示的本机地址；--port 0 默认自动分配空闲端口，不替换已有调试台
python3 -m agent_system.batch_review --batch-dir runtime/acceptance_deepseek_20261101_114430 --feedback
```

页面只展示每个视频预先选定的一条原始完整标题及原视频、完整 ASR/OCR、抽帧。人工选择三档、填写意见，可修改或撤回；保存状态会明确显示。无候选的视频保留为未产出，不要求虚构判定。技术失败但已有检查点候选的样本仍可给诊断意见，不改变原技术失败状态。

该模式只读原任务，判定与历史操作写本批 `human_feedback.db`，按任务、候选和完整标题哈希绑定；保留平台前缀、编辑稿、理由、判据与原系统路由。它不写全局 `templates/gold.csv`、不触发案例学习，也不重新调用模型。返回聊天说「我评完了」即可让 Agent 用 `--feedback` 或 `/api/feedback` 读取已落盘意见，无需复制编号和文字；页面保存不会主动向聊天推送消息。当前普通单任务调试台仍沿用原保存逻辑，此处隔离入口不代表旧链路审计问题已全部修复。

## 目录结构

| 路径 | 负责什么 | 会不会被改 |
|---|---|---|
| `agent_system/` | 主体代码：流水线（`pipeline.py`）、数据接入（`data.py`）、模型适配（`model.py`）、Prompt（`prompts.py`）、存储与人工判定（`storage.py`）、调试台（`debugger.py`）、报告（`report.py`） | 经常 |
| `agent_system/rules/` | 规则模型、加载、领域/形态路由、Rubric | 偶尔（改规则优先级时） |
| `agent_system/verification/` | 外部搜索核查 Agent：Provider（WSA）、品类路由与白名单、核查结果判定与风险汇总 | 偶尔 |
| `rulepacks/` | 规则正文，按职责拆成 `common.md`（U 条通用）/ `domains.md`（V 条垂类）/ `forms.md`（F 条形态） | 业务知识变了才改 |
| `templates/` | 判据库 `criteria.csv`、决策登记 `decision_register.csv`、业务用例 `cases.csv`、bad case `bad_cases.csv`、人工金标准 `gold.csv`；`anchors.md` 是三档判定的锚点样例；`README.md` 是用法说明（文件标题“五张表”沿用历史叫法） | 判定口径或案例变化时改 |
| `data/source/` | 只读输入：100 条主表 `video_dataset.json`、采集/标注规范、改名映射。原始视频不进仓库，靠 `asset_root` 关联 | 只读 |
| `docs/` | 产品说明、现状、节点表、评审卡、**bad case 规范**（`bad-case-spec.md`）、数据契约、设计稿（`design/`）、参考文档（`_ref/`，不进 git） | 每轮同步 |
| `specs/` | 需求/设计/任务/复盘存档：规则包模块化、10-30 复测复盘 | 归档，一般不动 |
| `scripts/` | 辅助脚本：批量评测 `eval.py`、待审清单 `review_queue.py`、判据打标 `tag_criteria.py`、统一入口 `run.sh` | 偶尔 |
| `tests/` | 单测：流水线/归一化/路由（`test_pipeline.py`）、决策接线（`test_decision_wiring.py`）、搜索核查（`test_verification.py`）、前缀与政策三分口径（`test_prefix_and_policy.py`） | 跟代码一起改 |
| `runtime/` | 运行产物（不进 git）：每次实验一个目录，各含 `agent.db`；`_legacy/` 与 `_old_worktree_*/` 是历史快照 | 只增不改 |
