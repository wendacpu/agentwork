# 推荐系统接入方案（服务化 MVP P2）

> 状态：本地、零成本的**模拟消费与信号回流**已实现；未接入任何线上推荐系统，未产生真实 CTR、曝光或 A/B 结论。

## 结论

推荐侧不应同步依赖标题生成：它只消费已经进入 `material_store` 的、版本可追溯的推荐物料。生成端故障时，推荐侧继续读存量；新任务留在 `task_queue`，由熔断、预算与恢复机制管理。

这套 MVP 证明的是「离线预生成 → 物料选择 → 可追溯模拟信号 → 修复假设」的工程闭环，**不证明标题质量或线上收益**。质量结论仍须来自冻结验证集、隔离人工评审台和真实平台实验。

## 1. 本地契约

### 1.1 上游：内容理解输入

现有 `Dataset` 从 `data/source/video_dataset.json` 与 `asset_root` 读取视频 ID、标题、ASR、OCR、帧和分类。生产替换位是数据接入适配层：将内容中台的 `VideoInput/Evidence` 映射为当前数据集字段，而不是让推荐系统传提示词或标题正文。

| 目标字段 | 当前来源 | 生产接入要求 |
|---|---|---|
| `video_id` | 数据主表 | 内容唯一 ID；幂等键与物料身份锚点 |
| 原始标题、ASR、OCR、帧证据 | `asset_root` | 证据类型、位置、采集时间和可用性必须保留 |
| 分类/内容形态 | 主表 | 供规则包和前缀策略使用 |
| 版本信息 | 运行配置、规则、Prompt | 写入物料指纹，支持审计与回滚 |

`POST /tasks {video_id, source?, idempotency_key?}` 只建任务并入队。服务限本机回环，生产接入必须补服务鉴权、调用方身份和审计日志。

### 1.2 下游：物料出口

`material_store` 是唯一对外物料契约，不直接开放任务状态表。每行含 `video_id/task_id/candidate_id` 三元身份、完整渲染标题、槽位、前缀、交付态、风险标、评审摘要、版本指纹和 `superseded_by` 版本链。

消费规则（当前固定策略）：

1. `GET /consume?video_id=<id>` 只选择同视频最新、未被 supersede 的物料；
2. 只允许 `is_recommended=1` 且 `delivery_state=recommended`；
3. `needs_review`、`not_recommended`、失败、已 supersede 物料一律不展示；
4. 未来替换点是「物料选择模型」，但不得绕过上述可交付过滤；
5. 线上物料库应以 `material_id` 去重、以 `version_fingerprint` 审计，并定义平台侧 TTL/下架能力。MVP 仅记录 supersede，不执行平台撤回。

## 2. 模拟信号回流

`POST /signals`：

```json
{
  "material_id": "mat-...",
  "event": "negative_feedback",
  "ts": "2026-11-06T12:00:00Z",
  "idempotency_key": "optional-caller-key",
  "payload": {"reason": "simulated_demo"}
}
```

- 支持事件：`impression`、`click`、`dismiss`、`negative_feedback`；`ts` 必须为 ISO-8601。
- 接口只接受当前可消费物料；过期/替换物料返回 `409`，未知物料返回 `404`。
- 每条信号写入 `service_signals`，并用调用方幂等键（没有时用规范化载荷哈希）去重。
- 同时写任务事件 `simulated_signal_received`；随后经既有 `repair_outbox → repair_defects` 链路生成可审查的修复假设。
- 信号修复键额外带 `simulated_signal` 来源，避免覆盖同候选的人工三档判定。
- 仅 `negative_feedback` 会进入 `triage`；其余信号保留观察记录而不凭空制造缺陷。

**严格边界：所有记录均带 `simulated=true`。它不是人工 verdict、不是金标、不是实际曝光、不是 CTR，更不能用于质量排序、显著性或线上效果结论。**

## 3. 影子运行建议

推荐系统真正接入前，应先影子写入、不改用户可见标题：

1. 内容事件触发 `POST /tasks`，记录原始输入/规则/Prompt 指纹；
2. worker 完成后，将物料写入影子库，推荐侧仅做契约校验和选择日志；
3. 对照当前线上物料，抽样人工审核风险标、交付态、版本切换和缺失率；
4. 真实行为回流需要平台提供曝光定义、随机化单元、实验分桶、反作弊、归因窗口与隐私审批；
5. 只有真实随机实验完成后，才能计算 CTR 或增量效果。冻结验证集的人评数字也不能替代这一环。

## 4. 容量、延迟与降级

- 生成采用「离线预生成 + 按需重生成 + 择优消费」，不处于推荐请求同步关键路径。
- `GET /queue` 给出队列、熔断、日任务预算及已完成任务的 p50/p95 时延；样本少于 2 时延指标返回 `null`，不伪造统计量。
- 模型返回 401/402/403 或缺 key 时，熔断器停止 worker 领新任务并保留 backlog；恢复后 `running → queued`，依赖 `Pipeline.execute` checkpoint 续跑。
- 日任务预算耗尽时拒绝新任务，避免隐性成本积压。MVP 按任务数限额；生产需要按模型调用和 token 预算补充二级闸门。

| MVP 组件 | 生产映射 |
|---|---|
| SQLite `task_queue` | MQ + lease / heartbeat / dead-letter queue |
| 进程内 worker | K8s Job / worker deployment |
| SQLite `material_store` | 内容中台物料库（版本、TTL、撤回） |
| 本地 `service_signals` | 统一埋点/实验平台（带实验桶与隐私治理） |
| 本地 HTTP 无鉴权 | 服务鉴权、限流、调用审计 |

## 5. 复现与验收

零成本回归：

```bash
python3 -m pytest -q tests/test_service.py tests/test_repairs.py
```

覆盖：消费端不读取 superseded 物料、模拟信号幂等、信号事件留痕、信号进入 `repair_outbox`、模拟信号不创建 `human_decisions`。这些验证的是路由和存储机制，不生成新的标题，也不需要人工评审台。

仍缺的 P2 exit evidence：真实断网→积压→恢复的端到端演练，以及「模拟负反馈 → 修复草案 → 人工批准 → 重跑对照」中人工批准和重跑对照的实际执行。它们需要单独明确范围；不能用本地接口测试替代。
