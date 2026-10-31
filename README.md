# 短视频推荐文案质量优化 Agent

这是一个可替换模型的本地 Agentic Workflow。仓库里的默认配置仍是确定性的 `mock` 适配器（`config.example.json`），用于验证完整状态流转；真实模型链路已在 `deepseek-flash`（关闭思考）上跑通并批量验证，Rubric 仍标记为未校准，不能替代人工质量标准。

实现与证据边界（状态概述沿用 2026-10-31 盘点；测试结果为本次整理实测）：

- 生成、独立评审、程序裁决、人工三档判定（可用 / 勉强通过 / 不可用）已闭环，判定写入 `templates/gold.csv`。
- 标题前缀标注位（`agent_system/labels.py`）：剧情演绎 / AI生成由程序按形态与属性判定，前缀与正文合计 ≤ `max_chars`，前缀不豁免。
- 外部搜索核查（`agent_system/verification/`，节点 N03V）：已接线但**默认关闭**，需 `.env` 里显式开启；当前只覆盖「资讯」域。
- 测试命令：`python3 -m pytest -q`。本次整理实测 90 passed；历史运行与数据快照见 `docs/status.md`，不能替代后续重测。

## 快速开始

```bash
# 推荐入口：自动加载 .env / .secrets.env，不把密钥写进命令行
scripts/run.sh audit
scripts/run.sh --config config.deepseek.json batch BV1Ltsf6xEpV

python3 -m pytest -q                                  # 本次整理：2026-11-02，90 passed
python -m agent_system.cli audit
python -m agent_system.cli run BV1Ltsf6xEpV
python -m agent_system.cli batch --limit 10
python -m agent_system.cli report <task-id> --output runtime/report.html
python -m agent_system.cli rereview <task-id>
python -m agent_system.cli human <task-id> <candidate-id> approved --reviewer tiffany
python -m agent_system.cli case <task-id> case-001 expression low_density before after "表达型案例" --confirm
python -m agent_system.cli debug
python scripts/review_queue.py                       # 摊开待审清单（每个视频一条）
python scripts/eval.py                               # 读 gold.csv 出四个数 + kappa
```

默认读取最终 100 条主表，并通过配置中的 `asset_root` 关联原始视频、抽帧、ASR 与 OCR。API 适配器支持 DeepSeek、Qwen 兼容接口和 Kimi；API key 只从环境变量读取（本地放在 `.secrets.env`，已被 `.gitignore` 忽略；用 `scripts/run.sh` 启动会自动加载），不会写入日志。外部事实核查已提供腾讯元宝搜索 WSA Provider（`AGENT_VERIFICATION_PROVIDER=wsa`，密钥放 `WSA_API_KEY` 环境变量；不要写入配置文件或提交进 Git），调用前还需显式启用 `AGENT_VERIFICATION_ENABLED=true`。

项目数据文件已经统一为英文命名并放在 `data/source/`；`docs/design/` 保存流程设计稿和思维导图。原始视频目录不复制进项目，使用 `config.example.json` 的 `asset_root` 关联。

规则正文按职责拆在 `rulepacks/common.md`、`rulepacks/domains.md` 和 `rulepacks/forms.md`。`agent_system/rules/` 负责规则模型、路由、加载和 Rubric；运行时由 `load_rule_bundle()` 按领域和内容形态选择 U/V/F 规则，并把 `rule_ids`、匹配原因和缺口写入任务状态。理解节点先把整条视频整合成跨片段、跨模态的语义单元（而非逐句 ASR/OCR 拆看点），每个单元聚合其原子陈述与证据并记录描述/意图/上传者说明的交叉印证；P3 看点节点再从语义单元中挑主/备选看点。生成、重写和独立评审共享本次规则切片，但评审不读取生成自评。

标题前缀与正文分开存：`agent_system/labels.py` 按「形态 × 属性」判定必须渲染的平台标注位（当前枚举：剧情演绎 / AI生成），模型自报的枚举外标签一律丢弃；长度按渲染后的完整串计算，前缀计入 `max_chars`（业务方 2026-10-31 定，前缀不豁免），因此 `body_budget()` 会实时扣掉前缀占位。

外部搜索核查（`agent_system/verification/`）在理解之后、边界判定之前以 N03V 节点运行：按领域路由到权威站点白名单，只查可核查的事实性断言，模型把检索结果判为 supported / refuted / unverifiable；`refuted` 且来源数达阈值的断言在边界阶段被强制 `excluded`、所属语义单元降级，并记入 `state.verification_risk`。默认 `verification_enabled=false`，不开启时该节点不产生任何调用。

## 运行结果边界

每次任务会写入 SQLite（`runtime/agent.db`）和事件日志。候选必须经过证据引用、长度、看点引用、限定表达（引用 `limited` 陈述时标题必须写出「据…/称/网传」等限定，否则报 `G1_MISSING_LIMIT` 强制重写）和元信息表述（禁止「作者称/字幕称/口播称/旁白称/标题称」这类框架，否则报 `G4_META_PHRASE` 强制重写）检查；评审结果会标记 `uncalibrated`。只有人工确认的 Case 才能进入 Memory。

理解阶段后有一道程序化的「一手证据守卫」（判据 C22）：材料里只要有 ASR/OCR/原题，claim 的 `evidence_refs` 就必须至少命中其中一条；只引用模型描述（description/caption）或画面帧路径的 claim 一律降级为 `excluded`，整个语义单元的 claim 都缺一手文本时该单元也不能作看点来源。这条堵的是最贵的一种失败——描述编造出一个不存在的情节，标题照写，重写多少轮都救不回来。纯画面视频（ASR/OCR/原题都为空）不适用本项判据。

评审使用 G1–G4 与 Q1–Q3。业务 Gate（G2）只有在配置 `extra.business_policy` 且 `status: "approved"`、有版本和非空 `rules` 时才参与自动通过判断；缺政策时 G2 恒为 `needs_review`。

`config.business-policy.json` 是已批准政策（`policy-v0.1`，8 条），每条规则带 `action`，决定违规后的处置，而不是一律涌向人审：

| action | 后果 | 典型场景 |
|---|---|---|
| `reject` | 直接驳回，不进重写 | 未成年人性化、可模仿的危险做法、隐私暴露、营销导流 |
| `rewrite` | 进重写循环，修好可发 | 低俗擦边措辞、伤亡细节渲染、群体贬损标签 |
| `review` | 必须人看，不自动放行 | 医疗/金融/法律结论、灾害伤亡具体信息 |

多条命中取最严（reject > review > rewrite）。G2=pass 时评审引用的规则是"检查过哪些"，程序不会误当成违规。规则没写 `action` 时保持旧行为（`reject` 需引用政策规则才真的驳回）。

材料不足时另有输入质量闸：`data.py: assess_material` 按一手证据分 `high/medium/low`（ASR 段数 + OCR 中文占比），`low` 时路由强制降级为 `needs_review`（`INPUT_LOW_CONFIDENCE`），配置 `extra.stop_on_low_quality_input` 可直接停在 `INSUFFICIENT_MATERIAL`。理解节点同时记录 `source_evidence_usage`——有没有陈述引用 `quality=source` 的原题，避免把唯一一手文本丢掉。

## 单任务调试台

运行 `python -m agent_system.cli debug`，然后在浏览器打开终端显示的本机地址（默认 `http://127.0.0.1:8765`）。调试台一次启动一条视频任务，页面先显示候选标题和人工判断，再展开流程、输入证据、规则和事件。人工评审只选择「可用 / 勉强通过 / 不可用」并说明标题瑕疵；点「驳回」时判定结果固定为不可用，只需选一个「驳回原因」（事实错误 / 关键背景缺失 / 没写到核心看点 / 句子不完整 / 用词读不懂 / 其他）并写一句话理由，原因与判据都可不填，由 AI 结合证据与流程记录归因。驳回原因会单独落到 `gold.csv` 的一列，便于统计主要失败类型。判定会同步写入 `templates/gold.csv`（按 video_id + candidate_id 覆盖）；已提交判定可撤回并重评，撤回会移除对应金标准行。`pending_human` 与 `stopped` 两种状态下的候选都可以判——重写耗尽而停下的任务里，候选同样是真实的。调试台默认扫描 `<output_dir>/*/agent.db` 下所有任务库（每次实验一个目录），需要指定时用 `--db-path`（可重复）。候选卡展示的是渲染后的完整标题（前缀 + 正文），字数按同一口径计算；编辑后通过时提交的也是完整串。Prompt 区展示 Prompt 组装记录、源文件常量和哈希，方便定位应该修改 `agent_system/prompts.py` 中的哪个节点；注入的规则正文可在规则区查看，对应源文件为 `rulepacks/*.md`。`mock` 模式下 Prompt 不会被大模型消费，候选只是确定性占位输出，不能用来判断 Prompt 效果。历史任务缺少 `prompt_snapshot` 时无法还原当时的完整输入。

每个模型调用会记录请求指纹、Prompt 快照、模型/节点耗时、重试和结构化结果。网络或临时服务错误可在任务页续跑，成功模型响应会按请求指纹复用；HTTP 402 等不可重试错误不再重复调用，CLI 批处理也会停止在首个不可重试故障。若 Prompt、规则或流水线代码已变化，续跑会拒绝混用新旧版本，需用新任务验证。

## 目录结构

| 路径 | 负责什么 | 会不会被改 |
|---|---|---|
| `agent_system/` | 主体代码：流水线（`pipeline.py`）、数据接入（`data.py`）、模型适配（`model.py`）、Prompt（`prompts.py`）、存储与人工判定（`storage.py`）、调试台（`debugger.py`）、报告（`report.py`） | 经常 |
| `agent_system/rules/` | 规则模型、加载、领域/形态路由、Rubric | 偶尔（改规则优先级时） |
| `agent_system/verification/` | 外部搜索核查 Agent：Provider（WSA）、品类路由与白名单、核查结果判定与风险汇总 | 偶尔 |
| `rulepacks/` | 规则正文，按职责拆成 `common.md`（U 条通用）/ `domains.md`（V 条垂类）/ `forms.md`（F 条形态） | 业务知识变了才改 |
| `templates/` | 判据库 `criteria.csv`、决策登记 `decision_register.csv`、业务用例 `cases.csv`、bad case `bad_cases.csv`、人工金标准 `gold.csv`；`anchors.md` 是三档判定的锚点样例；`README.md` 是用法说明（文件标题“五张表”沿用历史叫法） | 判定口径或案例变化时改 |
| `data/source/` | 只读输入：100 条主表 `video_dataset.json`、采集/标注规范、改名映射。原始视频不进仓库，靠 `asset_root` 关联 | 只读 |
| `docs/` | 产品说明、现状、节点表、评审卡、数据契约、设计稿（`design/`）、参考文档（`_ref/`，不进 git） | 每轮同步 |
| `specs/` | 需求/设计/任务/复盘存档：规则包模块化、10-30 复测复盘 | 归档，一般不动 |
| `scripts/` | 辅助脚本：批量评测 `eval.py`、待审清单 `review_queue.py`、判据打标 `tag_criteria.py`、统一入口 `run.sh` | 偶尔 |
| `tests/` | 单测：流水线/归一化/路由（`test_pipeline.py`）、决策接线（`test_decision_wiring.py`）、搜索核查（`test_verification.py`）、前缀与政策三分口径（`test_prefix_and_policy.py`） | 跟代码一起改 |
| `runtime/` | 运行产物（不进 git）：每次实验一个目录，各含 `agent.db`；`_legacy/` 与 `_old_worktree_*/` 是历史快照 | 只增不改 |
