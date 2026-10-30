# 规则校正与拆分技术设计

## 审计结论

| 缺陷 | 当前代码是否存在 | 依据与处理 |
|---|---|---|
| 硬约束与优化目标混用 | 存在 | `title_policy.information_chain`、`specific_entity_required_when_available` 与 20 字限制同列，未区分条件要求和偏好；改为明确等级与适用条件。 |
| 事实/断言/信源不正交 | 存在 | `assertion_decision` 同时含来源陈述、确定性和支持状态；`source_decision` 混用来源身份与转述路径；拆成独立字段。 |
| 信息完整性固定为单一标题句式 | 存在 | 通用 rubric 固定 `subject_or_object + action_or_event + result_or_state`，没有按内容交付判断；改为“独立可理解 + 承诺可兑现”的通用标准。 |
| 评审项有统一层级但职责仍重叠 | 部分存在 | 已有 G1–G4/Q1–Q3、单一 1–3 分制和程序决策；但 G1/G3/Q1/Q2 判定边界交叉，规则码也有重叠，需收敛。 |
| 多节点重复语义评审/修改 | 当前不存在 | 当前流程是生成 → 程序机械检查 → R01 单次语义评审 → Q01 程序决策 → 必要时定向重写；不新增语义预检 Prompt。 |
| Rubric/通过状态/发布准入未统一 | 大体已解决 | 当前由程序聚合 G1–G4 和 Q1–Q3，并以 `ready_for_human_review` 转人工；继续保留该合同，补清 Gate 边界。 |

## 校正后的规则模型

### 规则等级

- `hard`：不可违反，包括证据不得捏造、已批准业务政策、调用方明确的机械展示约束。
- `conditional`：仅当触发条件成立时必须满足，例如标题表达来源陈述时须保留足以避免误解的归属。
- `preferred`：优化目标，例如信息锚点、细节密度、修辞吸引力。长度冲突时可舍弃。
- 裁决顺序：证据与已批准政策 → 触发的条件要求 → 独立理解与交付承诺 → 偏好优化。模型必须报告无法同时满足的硬/条件约束，不得通过编造或压缩出歧义来满足。

### 事实与实体结构

每条 Claim 分别携带：

- `claim_type`：observable / source_claim / opinion / prediction / inference / fictional_setting。
- `evidence_status`：supported / limited / conflicting / unknown / unsupported。
- `original_source`：信源身份对象（可未知），不编码传播方式。
- `transmission_path`：从原始信源到视频材料的有序传播者/媒介记录。
- `temporal_status`：current / historical / dynamic / unknown。
- `assertion_permission`：direct / attributed / qualified / forbidden / unknown。
- `entities`：实体与角色关系，角色至少区分 event_actor、affected_party、source、relayer、location；不把所有具名实体都当成必留主体。

旧字段可在读取层暂时兼容并映射到新字段，但新 Prompt 和规范化输出只以新字段为准。来源名字是否写进标题，由断言许可、信息来源与误解风险决定，不因“来源可识别”一律强制重复来源名。

## 评审顶层分类与裁决权

| ID | 类型 | 唯一职责 | owner |
|---|---|---|---|
| G1 | 事实与断言许可 | 支持状态、实体角色、信源/传播、时间有效性、归属强度；不评标题是否有趣或信息够不够丰富 | R01 |
| G2 | 已批准业务政策 | 只检查当前启用且已批准的政策；未配置时 needs_review | R01，Q01 校验政策引用 |
| G3 | 内容交付与承诺 | 标题是否描述主看点、是否让用户预期视频未交付的内容 | R01 |
| G4 | 机械约束 | 长度、格式、引用与必需结构字段 | 程序 |
| Q1 | 清晰度 | 是否能读懂指代、关系和句意；不要求统一主谓宾句式 | R01 |
| Q2 | 信息价值 | 内容能否被识别、是否有具体且相关的信息；不重复评事实支持或交付覆盖 | R01 |
| Q3 | 吸引力 | 在 G1–G4 合格前提下，表达是否有真实观看动机；不为夸张加分 | R01 |

每个 issue 只允许一个 `primary_dimension`。G1/G3/G4 属于 Gate；Q1–Q3 使用 1–3 分，1=不足、2=基本达标、3=优秀。Gate 不能由分数抵消；Q 分不设置总分准入线，全部 Gate 通过且 Q1–Q3 均至少 2 后才进入人工确认；Q 总分只用于合格候选排序。`ready_for_human_review` 不是发布状态。

## Prompt 节点职责

- understand：抽取独立事实维度与实体角色，不裁定标题得分。
- boundary：逐条给出证据状态与断言许可，不强制标题字段模板。
- highlight：选择看点并绑定事实、证据与必要限定。
- generate：生成候选；不得自评、不得宣称通过。
- mechanical precheck：只由程序执行 G4，不创建语义预检模型节点，也不改标题。
- review：唯一语义裁判，只产出 G1–G3、Q1–Q3、issues 与 rewrite guidance，不产出总决策。
- rewrite：只按 R01 的结构化 issues 定向修改，不自评；随后由同一个 R01 重新评审。
- Q01：程序校验 schema/引用并聚合 Gate、质量分、路由；最终去向为人工确认、重写、待核验或拒绝。

## 规则文档与代码结构

```text
rulepacks/
  common.md                 # U 通用规则及规则优先级
  domains.md                # V01–V10 领域规则
  forms.md                  # F01–F14 形态规则

agent_system/
  rules/
    __init__.py             # 兼容门面：原有公共 API
    models.py               # Rule、RuleBundle 数据结构
    loader.py               # 三个来源的解析与规则块校验
    router.py               # 显式领域/形态匹配与 match_trace
    rubric.py               # Gate/Quality 定义、TEMP_RULES
```

领域和形态 Markdown 只保留规则本体：适用条件、要求等级、规则动作、边界和必要示例。删除旧规则包中的流程图说明、重复节点提示词、评审复制模板、样例输出和无关迁移说明。有效通用内容并入 `common.md`；节点 Prompt 保留在 `prompts.py`。

`load_rule_bundle()` 继续作为公共入口；`RuleBundle.as_context()` 保留现有字段，并新增来源文件列表。模块化 loader 分别解析三个文件；router 选择匹配的 V/F block，缺失与冲突写入 trace/gaps/conflicts。现有 pipeline 导入不变。

## 实施顺序与验证

1. 先更新 Claim/Boundary 结构、review Gate/Rubric 和生成/边界/评审/重写 Prompt；移除固定 information chain 与具名实体全保留。
2. 使用当前真实样例核对新字段映射、来源关系和旧数据兼容。
3. 再将 U/V/F 规则分别迁入三个 Markdown，并拆分 loader/router/rubric 代码。
4. 保持 `资讯-时事社会 + 资讯播报 → U + V04 + F11`；比较规则 ID、正文、RuleBundle 上下文和最终程序决策合同。
5. 更新受结构变化影响的自动化测试与文档；不触碰 `runtime/` 运行产物与视频数据。

## 不在本次范围

- 不校准线上效果，不把临时 Rubric 声称为人工验证标准。
- 不新增第二个模型语义评审/预检节点。
- 不引入数据库规则管理、在线编辑或外部依赖。
