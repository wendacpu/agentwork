# 红点规则包接入说明

规则正文分为 [`common.md`](../../rulepacks/common.md)、[`domains.md`](../../rulepacks/domains.md) 和 [`forms.md`](../../rulepacks/forms.md)；Python 加载、路由与 Rubric 模块位于 [`agent_system/rules/`](../../agent_system/rules/)。代码不会把所有垂类和形态正文硬编码到每个提示词，而是在运行开始后按本条视频的领域、内容形态和属性生成可追溯的 `RuleBundle`。

## 规则如何进入链路

| 现有节点 | 接入内容 | 代码入口 |
| --- | --- | --- |
| N03 理解 | U 的证据、时效、信源和内容形态约束 | `understand`，输出仍兼容旧 `claims`，也接受规则包的 `facts` |
| N04 路由 | U + 匹配的 V 领域规则 + F 内容形态规则 | `load_rule_bundle()`，结果写入任务状态 `rules` |
| N05 边界 | 可写事实、必要限定；分别记录 claim 类型、证据状态、原始信源、传播路径、时间状态、断言许可和实体角色 | `boundary` 的 `matched_rules` 输入 |
| N07 看点 | P3：主看点、备选看点、观看价值、交付证据 | 新增 `highlight` 模型调用；缺少合法输出时使用受限 fallback |
| N09 生成/重写 | P4 + 匹配规则 + 看点限定 + Memory | `generate` / `rewrite` 的 `matched_rules` 输入 |
| R01 评审 | 原始材料与证据、boundary、适用规则、G1–G3/Q1–Q3 Rubric、程序机械检查结果 | `ReviewResultBatch`；不读取生成自评，输出结构化 issues、重写指导和 Memory 查询 |
| Q01 决策 | 程序执行 G4、评审 Schema 校验、Gate 聚合、质量阈值、排序与路由 | 不使用模型自报 `passed`；缺字段、无效引用或未解风险不能自动通过 |

## 路由原则

领域和内容形态分开匹配。例如“资讯”不会自动加载资讯播报规则；只有形态确实是“资讯播报”才加载 F11。当前数据集的领域名称与规则包的 10 个 V 类别并非一一对应，因此代码使用保守关键词映射，匹配不到时记录 `no_domain_rule_matched` 或 `no_form_rule_matched`，不会假装已经有垂类规则。

`RuleBundle` 保存 `rule_ids`、`match_trace`、`gaps` 和 `conflicts`。这些字段随任务状态落库，报告可以说明本次到底用了哪些规则。正式业务政策仍单独配置；规则包缺少正式平台审核政策时，评审不能把临时检查写成“平台正式通过”。

## 规则优先级与评审职责

标题规则分为 hard、conditional、preferred：证据边界、已批准业务政策和调用方机械配置属于 hard；来源、时间、实体角色或断言限定只在触发条件成立时成为 conditional；信息锚点、句式和吸引力属于 preferred。不存在跨内容类型的“主体＋动作＋结果”硬门槛，也不要求保留所有具名实体。发生冲突时先满足 hard，再满足已触发的 conditional，最后优化 preferred；hard/conditional 无法同时满足时应说明无法满足。

R01 是唯一语义评审节点；G1–G3 与 Q1–Q3 每项有独立职责，G4 和总路由由程序执行。每个问题只能归属一个与 issue code 前缀一致的 `primary_dimension`。G1–G4 是准入门，Q1–Q3 是质量与排序；质量总分不替代 Gate，通过后也只是进入人工复核，不代表已发布。

重写仍由 Workflow 控制最多 2 轮。首轮与定向重写前均由程序按场景检索已确认的 Memory 案例；评审 Agent 只输出错误类型和检索 query，不读取案例。案例只能影响表达方式，不能迁移历史事实。生成 Agent 不提供供评审采信的自评，最终采用仍需人工确认。

