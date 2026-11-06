# 服务化 MVP · 接口设计（P1）

> 对应 `plan.md` 线 B / P1。原则：**不改流水线内部**，服务层只是调度器 + 物料出口；全部 stdlib + SQLite；只绑 127.0.0.1。生产映射（MQ/k8s Job/内容中台）写在线 C 文档，MVP 不实现。

## 1. 模块与库

`agent_system/service.py` 单模块承载：队列、worker、物料出口、HTTP。

库：**与任务库同库**（默认 `runtime/service/agent.db`，可 `--db` 指定）。理由：prepare/execute/events 全在这；同库让「入队→执行→物料写出」在一个连接组里完成，无跨库事务问题。service 用 `CREATE TABLE IF NOT EXISTS` 自建表，**不改 `storage.py`**。

## 2. 表契约

### task_queue（调度账本）
| 列 | 说明 |
|---|---|
| id INTEGER PK | 入队序 |
| task_id TEXT UNIQUE | `Pipeline.prepare()` 产出 |
| video_id TEXT | |
| source TEXT | 入队来源标记（api/cli/demo） |
| idempotency_key TEXT UNIQUE NULL | 同键重复 POST 返回已有任务，不再建 |
| state TEXT | `queued → running → done / failed` |
| enqueued_at / claimed_at / finished_at | ISO UTC |
| result_status / error TEXT | 任务终态快照 |

- **claim**：`BEGIN IMMEDIATE` + SELECT 队首 queued + UPDATE … WHERE id=? AND state='queued'，rowcount==1 才算领到（多 worker 互斥）。
- **崩溃恢复**：服务启动时把所有 `running` 复位 `queued`（单进程 MVP 无租约续期；生产映射 = lease + heartbeat，文档写明）。复位不清任务 state——`Pipeline.execute` 自带 checkpoint 续跑 + 模型响应指纹复用，重跑不重复付费。

### material_store（对外物料契约）
每任务到达终态后，按候选一行写入。**这是给「别人消费」的表**，字段按内容中台口径设计，不裸暴露 agent.db 内部结构：

| 列 | 说明 |
|---|---|
| material_id TEXT PK | `mat-<hex12>` |
| video_id / task_id / candidate_id | 身份三元组（评测口径要求候选存在于该任务） |
| slot TEXT | 三槽实名 `source_rewrite / core_summary / core_hook`（`thesis.SLOTS`）/ NULL |
| full_title / prefix / body / char_count | 渲染后完整标题 = 前缀 + 正文，字数同评审台口径 |
| is_recommended INTEGER | 1 = 该任务的系统推荐槽（`route.recommended_candidate_id`） |
| delivery_state | `recommended`（handoff 且被推荐）/ `passed_needs_review` / `not_recommended` / `failed` |
| route_action / route_reason | 路由快照，含 THESIS_CONTRACT_DISAGREEMENT 等降级原因 |
| risk_flags JSON | policy_hits、verification_risk、pending 待核项 |
| review_summary JSON | G1–G4 / Q1–Q3 / G2 违反规则（violated_rule_ids 口径，不吃自查清单） |
| version_fingerprint JSON | rule_version、生成/评审模型、prompt 哈希、config sha——可追溯「这条物料哪个版本产的」 |
| superseded_by TEXT NULL | 同 video 新任务写入时，旧行指向新 task_id（版本链，只追加不覆盖） |
| created_at | |

## 3. HTTP（`python -m agent_system.service --config <cfg> [--workers 1] [--port 8770]`）

| 端点 | 语义 |
|---|---|
| `POST /tasks` `{video_id, source?, idempotency_key?}` | prepare + 入队，`202 {task_id, state}`；幂等命中 `200` 带原 task_id；视频不在数据表 → `404`；预算闸/熔断拒收 → `429 {error, reason}` |
| `GET /tasks/<task_id>` | `{status, route, candidates:[{candidate_id, slot, full_title, delivery_state}], queue_state}` |
| `GET /queue` | `{queued, running, done, failed, circuit:{open,reason}, budget:{date, max_tasks, used}}` |
| `GET /materials?video_id=` | material_store 行（默认只回未 superseded 的最新版，`?all=1` 回全链） |

worker 默认与 API **同进程**内置线程 ×N（demo 形态：起一个命令就能 curl 入队看结果）；`--no-worker` 只开 API。

## 4. 护栏（P1 最小集）

- **熔断**：401/402/403/missing key 触发（口径与 `scripts/run_acceptance.py` 完全一致，不另造）。熔断后：在途任务允许跑完当前调用后停止领新任务；backlog 保留；`circuit_open` 落 `service_state` 表并计入 `/queue`；恢复靠重启服务或 `POST /admin/circuit {reset:true}`。
- **日预算闸**：`--max-tasks-per-day N`（默认 50）。`POST /tasks` 在熔断或超额时 429 拒收（不排队囤积——囤积会让成本失控看起来「还在跑」）。token 用量事后统计：任务 done 时从 `events` 的 `model_reply.usage` 汇总写 `service_usage`（只观测不拦截，MVP 成本模型走任务数闸）。
- **时延**：worker 记录每任务 claim→finish 毫秒，`/queue` 回 p50/p95（样本 <2 时回 null，不造假数）。

## 5. P1 出口判据（对应 plan.md）

1. mock 配置下批量入队 ≥10 条无人值守跑完，丢任务 = 0（进程杀掉重启后自动续完）。
2. 产出 100% 可经 `GET /materials?video_id=` 查询且带版本指纹。
3. 熔断注入（假 adapter 抛 402）：后续任务不被领取、backlog 保留、事件可查。
4. 幂等键重复 POST 不产生第二个任务。
5. 全量 pytest 绿（现基线 480 passed）。

## 6. 边界声明（写进演示与文档，不藏）

- 单进程多线程 ≠ 可扩展服务；claim 互斥靠 SQLite 事务，生产映射为 MQ + 租约。
- `POST /tasks` 无鉴权，仅 127.0.0.1；接入平台必须有服务间认证，文档列接口位。
- 物料「版本链」不含下架/撤回执行（只记 supersede），真撤回依赖平台物料库，接口位预留。
