# 归档区

**这里的文档不再代表当前实现或当前口径。** 归档不是删除——需要查历史时还能翻，但不该再拿它做判断依据。

## 为什么归档（2026-11-02）

判断标准是「和当前迭代节奏是否一致」，不是「写得好不好」。归档的都属于三类：

| 类型 | 文件 | 理由 |
|---|---|---|
| **旧版本文档** | `product-design-draft.md`（v0.4）<br>`product-design-draft.v0.5.md` | 自己标了「保留历史草稿，旧口径不能作为结论」；[project-narrative.md](../project-narrative.md) 是它们的升级版 |
| **内容重叠** | `product-agent-doc.md`<br>`短视频文案Agent报告目录.md/.docx` | 与 narrative 或「目录与项目背景」高度重叠，留着会干扰判断 |
| **阶段性记录** | `a-stage-verification.md`<br>`smoke-20261101-mock.md`<br>`signal-replay-report.md` | 一次性过程记录，阶段已过 |

## 主要过期口径（引用前必看）

- **业务规模**：旧稿按 700～800 万 / 560～640 万条推算，**现行口径是每天 3～4 万条**。
- **单条审核工时**：旧稿 45 秒/条，**现行按约 30 秒/条估算**，实际均值仍待测量。

## 当前有效文档在 `docs/` 根目录

提案正文 [project-narrative.md](../project-narrative.md) · Bad Case 规范与案例 [bad-case-spec.md](../bad-case-spec.md) · 评测口径 [eval-spec.md](../eval-spec.md) · 回流闭环 [feedback-repair-loop.md](../feedback-repair-loop.md) · 审核指南 [reviewer-guide.md](../reviewer-guide.md) · 链路节点 [nodes.md](../nodes.md) · 成本 [cost-per-video.md](../cost-per-video.md) · 模型选型 [model-selection-plan.md](../model-selection-plan.md)
