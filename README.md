# 短视频推荐文案质量优化 Agent

这是一个可替换模型的本地 Agentic Workflow。当前默认使用确定性的 `mock` 模型适配器验证完整状态流转；临时 Rubric 标记为未校准，不能替代人工质量标准。

## 快速开始

```bash
python -m agent_system.cli audit
python -m agent_system.cli run BV1Ltsf6xEpV
python -m agent_system.cli batch --limit 10
python -m agent_system.cli report <task-id> --output runtime/report.html
python -m agent_system.cli rereview <task-id>
python -m agent_system.cli human <task-id> <candidate-id> approved --reviewer tiffany
python -m agent_system.cli case <task-id> case-001 expression low_density before after "表达型案例" --confirm
python -m agent_system.cli debug
python scripts/review_queue.py                       # 摊开待审清单（每个视频一条）
python scripts/eval.py                               # 读 gold.csv 出四个数
```

默认读取最终 100 条主表，并通过配置中的 `asset_root` 关联原始视频、抽帧、ASR 与 OCR。API 适配器支持 DeepSeek、Qwen 兼容接口和 Kimi；API key 只能通过环境变量提供，不会写入日志。

项目数据文件已经统一为英文命名并放在 `data/source/`；`docs/design/` 保存流程设计稿和思维导图。原始视频目录不复制进项目，使用 `config.example.json` 的 `asset_root` 关联。

规则正文按职责拆在 `rulepacks/common.md`、`rulepacks/domains.md` 和 `rulepacks/forms.md`。`agent_system/rules/` 负责规则模型、路由、加载和 Rubric；运行时由 `load_rule_bundle()` 按领域和内容形态选择 U/V/F 规则，并把 `rule_ids`、匹配原因和缺口写入任务状态。P3 看点节点会在候选生成前独立产出主/备选看点；生成、重写和独立评审共享本次规则切片，但评审不读取生成自评。

## 运行结果边界

每次任务会写入 SQLite（`runtime/agent.db`）和事件日志。候选必须经过证据引用、长度和看点引用检查；评审结果会标记 `uncalibrated`。只有人工确认的 Case 才能进入 Memory。

评审使用 G1–G4 与 Q1–Q3。默认没有正式业务安全政策，因此 G2 会进入 `needs_review`，任务不会自动达标；只有在配置 `extra.business_policy`，并提供 `status: "approved"`、版本和非空 `rules` 后，才允许业务 Gate 参与自动通过判断。

## 单任务调试台

运行 `python -m agent_system.cli debug`，然后在浏览器打开终端显示的本机地址（默认 `http://127.0.0.1:8765`）。调试台一次启动一条视频任务，页面先显示候选标题和人工判断，再展开流程、输入证据、规则和事件。人工评审只选择「可用 / 勉强通过 / 不可用」并说明标题瑕疵；问题判据可选，不确定根因时留空，由 AI 结合证据与流程记录归因。判定会同步写入 `templates/gold.csv`（按 video_id + candidate_id 覆盖）；已提交判定可撤回并重评，撤回会移除对应金标准行。`pending_human` 与 `stopped` 两种状态下的候选都可以判——重写耗尽而停下的任务里，候选同样是真实的。调试台默认扫描 `<output_dir>/*/agent.db` 下所有任务库（每次实验一个目录），需要指定时用 `--db-path`（可重复）。Prompt 区展示 Prompt 组装记录、源文件常量和哈希，方便定位应该修改 `agent_system/prompts.py` 中的哪个节点；注入的规则正文可在规则区查看，对应源文件为 `rulepacks/*.md`。`mock` 模式下 Prompt 不会被大模型消费，候选只是确定性占位输出，不能用来判断 Prompt 效果。历史任务缺少 `prompt_snapshot` 时无法还原当时的完整输入。

每个模型调用会记录请求指纹、Prompt 快照、模型/节点耗时、重试和结构化结果。网络或临时服务错误可在任务页续跑，成功模型响应会按请求指纹复用；HTTP 402 等不可重试错误不再重复调用，CLI 批处理也会停止在首个不可重试故障。若 Prompt、规则或流水线代码已变化，续跑会拒绝混用新旧版本，需用新任务验证。
