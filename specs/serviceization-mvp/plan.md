# 服务化 MVP + 推荐系统接入方案 · 行动计划

> 状态：已批准；P1 已实现，P2 的本地模拟消费/信号回流已实现。目标 = 服务化 MVP + 接入方案能讲死；不是「真接入」。
> 硬约束：3 周封顶；技术栈只用 stdlib + SQLite（不引入 docker/redis/celery/FastAPI）；每周人工投入 ≤2h，其余由 agent 承担。

## 0. 当前事实（2026-11-06 盘点）

- V 集已冻结（`templates/validation_manifest.json` v2-20261104，20 条，成员哈希 583d62ec…），**从未跑过任何模型**——跑批被 DeepSeek 额度 402 阻塞（见 `docs/evaluation-readiness-diagnosis.md`）。
- 开发集 D 已确认整批污染（20/20 被历史 117 个库接触过），历史 30%/55% 数字只能描述旧版本。
- 成本改造 A/B（11-05）后过闸 ready 54%，但该批人工终验未完成，不算验收结论。
- 工程底子：`Pipeline.prepare/run/resume/execute(video_id)` 已是干净任务入口；任务态在 SQLite（tasks/events/candidates_by_task/reviews/human_decisions/feedback）；`ThreadingHTTPServer` 已有三处先例（debugger / batch_review / diagnostic_server）；模型响应按请求指纹复用、Prompt/规则变化拒绝混用续跑——幂等续跑机制现成。

## 1. 三条线四个阶段

### 线 A · 质量验收（P0，D1–D3）

| # | 任务 | 谁 | 出口判据 |
|---|---|---|---|
| A1 | DeepSeek 账户充值（建议 ¥50 档；V 集全流程实测成本约 ¥4–8，留重写与复跑余量） | 本人拍板 | 冒烟 1 条过闸非 402 |
| A2 | V 集 20 条跑批（`scripts/run_acceptance.py`，audit 前置免费预检） | agent | 20 任务全部终态（handoff/needs_review/stopped），无 prepared 残留 |
| A3 | 冻结批次评审台人工三档判定 + 驳回原因（`agent_system.batch_review`） | 本人 ~2–3h | 出三个干净数字：首推严格可用率、首轮交付率、重写挽救率（对应既有三条 OKR 口径） |
| A4 | 抽 10 条候选双人盲标算 Cohen's kappa（可后置，不阻塞 P1） | 本人 + 一名标注者 | kappa 或「不可算」如实记录 |

**纪律**：数字无论到没到 50% 都照实报；V 集按关键词热度采集、非平台随机，报告必须带外推限制声明。

### 线 B · 服务化 MVP

#### P1 任务化 + 物料写出（W1，agent 主建）

| # | 任务 | 说明 |
|---|---|---|
| B1 | `agent_system/serve.py` | stdlib ThreadingHTTPServer。`POST /tasks {video_id, source}` 入队；`GET /tasks/{id}` 状态+候选+评审结论；`GET /queue` backlog。只绑 127.0.0.1 |
| B2 | 队列表 + worker | `task_queue` 表 + 1–2 个 worker 领任务调 `Pipeline.execute`；崩溃重启幂等直接复用现有指纹续跑 |
| B3 | 物料出口 `material_store` | **对外契约表**，不裸暴露 agent.db 内部表：渲染完整标题 + 槽位（core_hook/highlight/curiosity）+ 前缀标注 + 风险标（policy_hits/verification_risk/THESIS_CONTRACT_DISAGREEMENT）+ 审核态 + 版本指纹（rule_version/model/prompt hash）+ supersede 链 |

P1 出口判据：**主池 100 条无人值守跑通**，中断丢任务 = 0，产出 100% 经 API 可查且写 material_store；实测 P50/P95 任务时延、吞吐（条/日）、每条成本。

#### P2 模拟消费端 + 护栏（W2，agent 主建）

| # | 任务 | 说明 |
|---|---|---|
| B4 | 选物料模拟器 | 从 material_store 按视频取展示标题（现阶段=系统推荐槽；留「未来素材选择模型」替换接口），记录展示事件 |
| B5 | 行为信号回流 | `POST /signals {material_id, event, ts}` → 走既有 feedback/repair_outbox 链路；演示完整一条：模拟信号 → 聚组归因 → 修复草案 → 人工批准 → 重跑对照。**信号是模拟载荷，文档显式声明非真实 CTR** |
| B6 | 护栏三件 | 模型不可用熔断（worker 停领 + backlog 保留 + 告警事件）；日预算闸（超限拒收新任务）；节点级时延/重试统计（并进诊断台） |

P2 出口判据：演示「拔网线→队列积压→恢复→自动续跑」全程无丢单；信号→修复草案链路走通一次并留事件痕迹。

### 线 C · 接入方案与叙事（文档 W1–W2 并行，W3 定稿）

| # | 任务 | 说明 |
|---|---|---|
| C1 | `docs/recommendation-integration.md` | ①上游契约：内容理解服务产出 → `VideoInput/Evidence` 字段映射表，`data.py` 加 provider 适配层替代手动 asset 目录；②下游契约：material_store → 物料库写入/版本/撤回/TTL；③信号回流位：物料级实验的随机化单元怎么写；④容量成本模型：实测 ¥0.18/条、~8 次调用折算平台日新增量级 → 推出「离线预生成 + 按需重生成 + 择优消费」而非在线生成；⑤降级预案：生成服务故障时推荐侧只消费存量，永不同步依赖；⑥生产映射：task_queue→MQ、worker→k8s Job、material_store→内容中台 |
| C2 | 演示脚本 + 面试叙事 | 5 分钟 live demo：curl 入队 → 轮询 → 物料库查询 → 模拟信号回流 → 修复草案出现；简历三句话 + 数据表 |

## 2. 预期效果

**量化层**
- 拿到从未污染验证集的首次实测：三个 OKR 口径数字（首推严格可用率 / 首轮交付率 / 重写挽救率）+ 长度合格率 + 误放样本。这是「值不值上线」的唯一可信证据，现在还没有。
- 100 条无人值守跑通（现状 CLI 批量要人盯、中断留 prepared 残尸）；吞吐 / P95 时延 / 成本护栏全部有实测数。
- 反馈闭环从「只有人评一种信号」升级为「行为信号接口已存在且被走通一次」。

**叙事层**
- 项目定位从「我搭了个 Agent workflow 生成标题」变成「我建了个物料生成服务：近线定位、对外契约、熔断降级、成本模型、A/B 回流都想清楚过」。工程师追问幂等、降级、为什么不做在线、成本怎么算、信号真不真——每个点有代码落点或文档落点。对 AI PM 面试，这是 demo→工程的完整闭环证据。

**诚实边界层**
- 三件事写进文档：V 集非随机不能外推 CTR；行为信号是模拟载荷；真 A/B 与真接入只在平台发生。「没做的」自己先说清楚，面试时是加分项不是雷。

## 3. 风险与停止规则

| 风险 | 处置 |
|---|---|
| 质量不达标（V 集 <50%） | 不挤占 P1/P2 工期去救质量；实测结果本身进叙事，质量冲刺另立下一轮 |
| 过度工程化 | 任何 B 项单元超 2 天 → 降级为「写进文档、不做进代码」；新依赖一律拒绝 |
| 预算 | 全流程模型花费 ≈ ¥10–20（跑批+冒烟+P1 百条无人值守用现有 deepseek 价），充值是唯一前置；等预算=等决策，不是等钱多 |
| 秋招面试冲突 | 3 周硬帽；人评/演示/拍板之外的工作全部由 agent 承担 |
