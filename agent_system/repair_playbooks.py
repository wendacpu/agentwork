"""可人工执行的修复调查任务；只构造数据，不运行代码、不改源码、不授予发布资格。

family 是调查分流而非根因结论。真实运行是否首发/失守必须由历史快照、
节点输入输出及版本证据确认；当前代码重放不能替代历史执行证据。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


FAMILIES = ("content_nature", "numeric", "selection", "constraint", "expression", "unresolved")


def _location(file: str, symbol: str, role: str) -> dict[str, str]:
    return {"file": file, "symbol": symbol, "role": role}


def _step(phase: str, action: str, required_evidence: str) -> dict[str, str]:
    return {"phase": phase, "action": action, "required_evidence": required_evidence,
            "conclusion_status": "unconfirmed"}


def _option(trigger: str, change: str, do_not: str) -> dict[str, str]:
    return {"trigger": trigger, "change": change, "do_not": do_not,
            "trigger_status": "unconfirmed"}


def _case(kind: str, scenario: str, expected: str) -> dict[str, str]:
    return {"kind": kind, "scenario": scenario, "expected": expected,
            "truth_status": "synthetic_test_spec_not_observed_result"}


_PLAYBOOKS = {
    "content_nature": {
        "objective": "区分本片明确声明、讨论/否定、衍生线索与未知属性，定位标注或待审链路断点。",
        "target_locations": [
            _location("agent_system/signals.py", "extract_risk_signals", "N02：声明指向、极性、质量和证据偏移"),
            _location("agent_system/prompts.py", "UNDERSTAND_PROMPT", "N03：风险信号与属性解释，不将线索升级事实"),
            _location("agent_system/labels.py", "required_prefixes", "形态/属性到平台标注的映射"),
            _location("agent_system/pipeline.py", "_normalize_candidates", "N09：必需前缀规范化与渲染"),
            _location("agent_system/pipeline.py", "_mechanical_check", "前缀缺失及总长度兜底"),
            _location("agent_system/pipeline.py", "Pipeline._generate_and_review", "未解属性信号阻止普通交接"),
            _location("agent_system/pipeline.py", "Pipeline.rereview", "历史候选重审重新提取信号与前缀"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "沿原始声明→N02信号→N03属性→required_prefixes→候选前缀逐跳比对，找首次丢失或误认；否定、讨论及衍生文本单独列出。", "声明原句/evidence_id/span/quality、当时模型输出与版本、prefix_labels"),
            _step("escaped_defense", "确认首次错误之后，前缀检查、最终渲染、pending_signal_ids 路由及重审哪一层应捕获但未捕获；不存在历史输出时只列待查。", "mechanical_checks、rendered_title、risk_signals、route、历史节点日志"),
        ],
        "change_options": [
            _option("原始证据明确指向本片的肯定声明被漏提取，且偏移/语境经人工核对", "定向补充声明解析与极性/指向测试，再检查必需前缀传递。", "不能看到AI/虚构字样就无条件加标；讨论、否定、引用和低校准描述不能自动确认为本片属性。"),
            _option("信号已正确提取但必需前缀或待审状态在规范化、渲染、重审中丢失", "在已定位的传递断点保留前缀/未解状态，并让生成与重审使用一致约束。", "不能让模型漏标取消平台义务，也不能为通过而放宽总字数或把未知属性当已确认。"),
        ],
        "test_cases": [
            _case("positive", "ASR明确写本视频由AI生成，understanding.attributes为空", "AI生成前缀仍传入并渲染；前缀与正文共同计数"),
            _case("positive", "OCR明确写本片纯属虚构，正文未自带标签", "按确认声明要求剧情演绎标注"),
            _case("negative", "不是AI生成；如何识别AI生成；他称另一部片是AI生成", "分别核对否定/讨论/指向，不凭关键词给本片确认属性"),
            _case("variant", "只有 model_derived_uncalibrated 描述肯定AI属性", "保留needs_verification并待审，不能当确认声明或默默交接"),
            _case("variant", "伪造历史mechanical_checks=pass并删除前缀后重审", "重新计算守卫，不信任旧pass；记录当前重放而非声称历史必然漏检"),
        ],
        "family_acceptance": "确认声明不漏标，否定/讨论不误标，未知属性留待核验，初审/重审和实际渲染一致。",
    },
    "numeric": {
        "objective": "逐项定位数值、单位、指标、问句/断言与引用关系，不把差异直接判为数字冲突真相。",
        "target_locations": [
            _location("agent_system/signals.py", "numeric_facts", "数字归一化、指标、角色与原文偏移"),
            _location("agent_system/signals.py", "unsupported_numeric_facts", "标题/陈述到被引用原始证据的支持检查"),
            _location("agent_system/prompts.py", "UNDERSTAND_PROMPT", "N03：原题与ASR冲突、说话者及疑问语气"),
            _location("agent_system/pipeline.py", "_apply_numeric_conflict_gate", "N05：无数字支持的claim进入核验"),
            _location("agent_system/pipeline.py", "Pipeline._generate_and_review", "N09：按引用claim回查证据而非原题白名单"),
            _location("agent_system/pipeline.py", "Pipeline.rereview", "重审重新执行数字支持检查"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "制作原文→数字事实→claim→boundary→标题对照表，检查首次改变数值、单位、总价/单价、实体或问句角色的节点；原题与ASR不设固定优先级。", "原句与span、evidence_refs、说话者、numeric_facts、N03/N05输入输出"),
            _step("escaped_defense", "分别检查N05数字守卫、N09按claim取证、独立评审与重审为何未阻止不支持断言；核对是否错引其他实体或旧缓存。", "引用claim映射、过滤后的证据文本、mechanical_checks、reviews、历史代码版本"),
        ],
        "change_options": [
            _option("人工对齐证明抽取器在具体单位、中文数词或语气上解析错误", "局部修正numeric_facts的归一化/角色/指标规则并加最小对照用例。", "不能无条件禁止数字、把不同数字等同冲突、默认原题正确或把ASR差异直接当噪声。"),
            _option("抽取正确但claim或标题使用了问句数字、错误指标或未引用证据", "修复引用传播/支持检查；核心数字未解时保留核验任务或改选已有支持的看点。", "不能自动算出材料未给的结果；同数值同单位也不能证明同实体同指标。"),
        ],
        "test_cases": [
            _case("positive", "证据断言总价八百元，标题总价800元且引用同一事实", "归一化等值可支持，不因字面写法不同误拦"),
            _case("positive", "原题872元、ASR说总价800元，标题断言总价872元且仅引用ASR", "标记数字支持未解，不能用原题白名单放行或宣称872必假"),
            _case("negative", "一口一个脆的口感修辞；无数字事实的正常标题", "不凭普通词片段制造数量事实"),
            _case("variant", "证据总价800元，标题每人800元；证据800元吗，标题断言800元", "单价/总价不混用，疑问数字不变直接断言"),
            _case("variant", "同数值但人物不同，或生成后/重审时替换evidence_refs", "需复核实体和引用链，不能仅数值相同视为已核实；初审重审都覆盖"),
        ],
        "family_acceptance": "原案例及等值写法均验证，单位/指标/实体/语气不串用，无支持只记未解而不编造真值。",
    },
    "selection": {
        "objective": "定位主旨提取、核心单元选择或观看承诺失配，而非只修标题用词。",
        "target_locations": [
            _location("agent_system/prompts.py", "UNDERSTAND_PROMPT", "N03：thesis、role_in_video和跨片段语义整合"),
            _location("agent_system/pipeline.py", "_normalize_understanding", "语义单元/事件/转折的规范化"),
            _location("agent_system/pipeline.py", "_structural_candidates", "关系证据契约缺失时的结构候选闸（当前不合成关系候选）"),
            _location("agent_system/prompts.py", "HIGHLIGHT_PROMPT", "N07：core主线测试、备选与淘汰理由"),
            _location("agent_system/pipeline.py", "_normalize_highlights", "看点引用与fallback结果"),
            _location("agent_system/prompts.py", "REVIEW_PROMPT", "R01：G3观看承诺与主看点覆盖"),
            _location("agent_system/pipeline.py", "Pipeline._route_candidates", "看点错误回退及重写路由"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "完整阅读语义链，手工标主旨/core/support/peripheral；判断是N03漏主旨、N07选边角、结构候选失真还是N09偏离已选看点。", "完整ASR/OCR、semantic_units、events/turns、main_thesis、highlights、eliminated与候选"),
            _step("escaped_defense", "对照用户只看标题的观看预期与视频实际交付，查G3评审是否把边角钩子误当核心、fallback是否绕过筛选、路由是否只改表达。", "R01输入/expected_takeaway/issues、fallback记录、rollback_to与重写前后看点ID"),
        ],
        "change_options": [
            _option("完整材料支持核心主旨但理解输出缺thesis或把同一意思拆碎", "修正理解阶段的合并与主旨输出，再验证下游选点，不先追加标题禁词。", "不得只看原题/描述就确认主旨，更不能将缺少印证的关系升级成事实。"),
            _option("核心单元正确但看点选了边角，或重写仍沿用失配看点", "定向调整core主线测试/fallback条件，并让已确认的G3问题回退看点选择。", "不能一律禁用情绪、反差或悬念；仅在内容不支持或未兑现时拒绝该钩子。"),
        ],
        "test_cases": [
            _case("positive", "完整视频讲训练效果主张，只有一句作者自嘲更吸睛", "主看点不选自嘲边角，选有证据且保留必要限定的核心意思"),
            _case("positive", "主旨已被正确识别，生成标题却承诺视频未给的方法", "定位生成偏移并由G3阻断，不误归因全部为N03失败"),
            _case("negative", "视频主旨本来就是完整动作演示或真实有答案的悬念", "允许动作/悬念作为核心，不一律禁用某类内容或钩子"),
            _case("variant", "调换片段顺序、同义改写边角金句、无thesis触发fallback", "主线判断不随局部钩子漂移；fallback不能把未知主旨伪装成确定事实"),
        ],
        "family_acceptance": "核心覆盖与观看承诺经完整材料复核；边角不抢主位，合法动作/反差/悬念不被整体压制。",
    },
    "constraint": {
        "objective": "追踪实体方向、必要限定、归属与时间锚点在边界和成品中的传递。",
        "target_locations": [
            _location("agent_system/prompts.py", "BOUNDARY_PROMPT", "N05：required_limits、归属和断言许可"),
            _location("agent_system/pipeline.py", "_normalize_boundary", "边界字段规范化"),
            _location("agent_system/pipeline.py", "_mechanical_check", "必要限定与争议标记兜底"),
            _location("agent_system/signals.py", "check_signal_constraints", "无引用支持的时效措辞"),
            _location("agent_system/signals.py", "signal_review_reasons", "限定语义及时效待审，不以缺词直接判事实错误"),
            _location("agent_system/pipeline.py", "Pipeline._generate_and_review", "source_requirements及引用陈述传递"),
            _location("agent_system/pipeline.py", "Pipeline._validate_and_decide_reviews", "限定未核实转needs_review"),
            _location("agent_system/prompts.py", "REWRITE_PROMPT", "定向修订不能牺牲限定或归属"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "从证据实体角色与条件出发，逐条比对claim、boundary.required_limits/forbidden_inferences、生成输入和实际正文，找首次反向归属、漏限或时间漂移。", "原句/说话者/事件日期、claim、boundary、source_requirements、qualifiers_used与正文"),
            _step("escaped_defense", "检查机械检查是否被一个无关不/称字骗过、独立评审是否逐条核对限定、signal_review_reasons是否进入路由；自报限定不算正文保留证据。", "mechanical_checks、signal_review_reasons、expected_takeaway、route及重写父子候选"),
        ],
        "change_options": [
            _option("限定在边界正确存在却于生成/重写丢失，且语义核对确认读者预期改变", "修补实际传递断点与逐claim限定核对；装不下时换有支持的更短看点或待审。", "不能无条件强制地点/主体全称/完整主谓宾，也不能删限定、改字数上限换通过。"),
            _option("词面守卫把同义保留误判，或被无关限定词错误放行", "将词命中作为待核验线索，补语义对照与按引用限定的局部校验。", "不能把未出现原词直接当事实错误，或用任意据/不字替代特定范围和争议。"),
        ],
        "test_cases": [
            _case("positive", "证据仅部分成人可能适用，标题人人适用", "发现范围及不确定性丢失并要求修订/核验"),
            _case("positive", "2020年材料说今日开业，新任务仍写今日开业", "相同词面不证明当前事件时间，缺独立日期锚定需核验"),
            _case("negative", "部分成人可能适用改为仅限部分成年人或可适用", "核对语义等价，不因原限定词消失就判事实错误"),
            _case("negative", "有证据的无歧义名词性标题，地点与全名不是理解所必需", "不为凑字段强加背景，保留自然简洁表达"),
            _case("variant", "他给她带饭改为她给他带饭；争议陈述只加某称；无关不字", "实体方向和争议各自核对，不能被泛限定词或自报qualifiers_used绕过"),
        ],
        "family_acceptance": "必要限定和实体关系语义保留、非必要背景不强加；历史时效、归属与争议不互相替代。",
    },
    "expression": {
        "objective": "在事实、看点和必要限定正确前提下，修复读者无法独立理解或不自然的表达。",
        "target_locations": [
            _location("agent_system/prompts.py", "GENERATION_PROMPT", "N09：语义自足、称谓与自然省略"),
            _location("agent_system/prompts.py", "REWRITE_PROMPT", "只修已定位表达缺陷且保留正确事实"),
            _location("agent_system/prompts.py", "REVIEW_PROMPT", "R01：Q1清晰度及G3元话语的区分"),
            _location("agent_system/pipeline.py", "_mechanical_check", "格式/长度及载体元话语守卫"),
            _location("agent_system/pipeline.py", "_default_rollback", "表达缺陷回退N09"),
            _location("agent_system/pipeline.py", "Pipeline._generate_and_review", "定向修订与父子候选对照"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "先确认事实和看点无误，再让读者仅看渲染标题复述主体/关系/承诺；定位N09首发残句、错误动作主语、黑话或误连因果。", "证据/边界/看点、生成提示版本、headline与rendered_title、独立读者复述"),
            _step("escaped_defense", "核对R01是否将短标题等同不完整，或只看修订理由未读成品；查重写是否确实解决issue_id且未新增事实、丢限定或改变看点。", "Q1及G3理由、rewrite_guidance、addressed_issue_ids、父子正文和mechanical_checks"),
        ],
        "change_options": [
            _option("独立读者对必要主体或关系理解分歧，且证据能提供消歧信息", "只补必要指代/关系或换自然词语，重测语义与总长度。", "不得无条件要求完整主谓宾、加全名地点或禁所有名词性标题。"),
            _option("已确认是载体元话语、错误动作主语、黑话或虚构连接", "按具体语义缺陷改写并重新独立评审，保留真实信源与正确事实。", "不能全局封禁作者称/据/反被等词；真实来源归属和有证据的反差应保留。"),
        ],
        "test_cases": [
            _case("positive", "小店逛吃发现好物，让地点成为执行动作的主语", "改成证据支持的行动主体或自然结构，不新增人物身份"),
            _case("positive", "据某机构，后接一串缺少关系的名词；标题用未展开行业缩写", "修复悬空归属/关系和黑话；无证据时不得编造补全"),
            _case("negative", "雨后的老街；车主表示已收到赔偿，均有证据且可独立理解", "允许名词性标题及真实信源归属，不为语法完整强改"),
            _case("variant", "有证据的迟到反被关心与无因果的定十个闹钟挡不住被关心", "允许真实反差，只修错接关系；改写后仍符合边界与前缀总长度"),
        ],
        "family_acceptance": "独立读者能复述必要关系；不靠新事实或删限定补句；名词句、自然省略与真实归属不误伤。",
    },
    "unresolved": {
        "objective": "信息不足先补证分流；未知族/人工理由不清时不生成已确认根因或自动补丁。",
        "target_locations": [
            _location("agent_system/prompts.py", "UNDERSTAND_PROMPT", "原始证据与推断边界"),
            _location("agent_system/signals.py", "extract_risk_signals", "补查可定位的风险线索，不能替代真值"),
            _location("agent_system/pipeline.py", "Pipeline._review_input", "核对评审实际可见材料"),
            _location("agent_system/pipeline.py", "Pipeline._validate_and_decide_reviews", "未知事实/政策的待审路径"),
            _location("agent_system/repairs.py", "diagnose", "人工理由、issue_code与调查族的假设分流"),
        ],
        "diagnostic_steps": [
            _step("first_failure", "补齐人工理由、原候选、证据和节点快照后再分流；找不到首发节点保持unconfirmed，不把family分类当因果。", "task_id、candidate_id、原始证据、具体误读/期望与节点版本；缺项清单"),
            _step("escaped_defense", "核对当时哪些约束与评审材料真实存在；无历史检查结果不能宣称已定位失守，当前重放仅生成调查线索。", "历史提示hash/代码版本/检查和路由输出，以及与当前重放的差异"),
        ],
        "change_options": [
            _option("补证后人工能够将缺陷定位到具体族和节点，并确认适用约束", "重新生成对应族调查任务，明确根因确认与失守确认分别需要的证据。", "信息不足不自动改提示词、加禁词、修改守卫或发布；人工期望改文也不是事实真值。"),
        ],
        "test_cases": [
            _case("positive", "reason为空、快照缺失或family是未知值", "返回unresolved补证任务，首发/失守未确认且无发布资格"),
            _case("negative", "理由不清但标题含数字或AI关键词", "不能仅凭词命中确认根因或风险属性"),
            _case("variant", "之后补充证据、撤回人工判断或改写最终标题", "按新证据重新分流；旧假设不升格，final_headline只作人工修订参考"),
        ],
        "family_acceptance": "缺证明确列出，分流需人工核对，根因未知时仅交付补证任务，不授予自动发布资格。",
    },
}


def playbook(family: str) -> dict[str, Any]:
    """返回独立可修改的调查模板；未知名称安全回落 unresolved。"""
    selected = family if isinstance(family, str) and family in _PLAYBOOKS else "unresolved"
    result = deepcopy(_PLAYBOOKS[selected])
    result.update({
        "family": selected,
        "requested_family": family if isinstance(family, str) else None,
        "status": "hypothesis",
        "first_failure": "unconfirmed",
        "escaped_defense": "unconfirmed",
        "execution_mode": "manual_plan_only",
        "release_eligible": False,
        "acceptance": {
            "functional": result.pop("family_acceptance"),
            "evidence": "原案例复现、首发与失守各有历史节点证据；当前守卫重放不得充当历史执行结论。",
            "regression": "正例、反例、同风险变体及历史保留集逐项核对；未运行只记录测试规格，不记为通过。",
            "false_positive": "在相同人工标注保留集比较误拦数/样本数及误拦率；不得比基线增加，例外须显式人工批准。",
            "human_burden": "同一保留集比较needs_review数量/总量、每百任务人工复核分钟和新增核验任务；不得无批准地增加。",
            "measurement": "记录基线/变更版本、同一数据集、分母、逐例判定、复核用时与预算；缺任一测量不得声称验收完成。",
            "release": "根因和失守经人工确认、所有测试及负担预算验证并独立审批后才可另走发布门禁；本接口永不授权发布。",
        },
    })
    return result


def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str) and item]
    return []


def build_work_items(family: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """嵌入反馈上下文，生成可交给人的具体任务，不读取文件/数据库或执行测试。

    证据引用合并 payload、候选和候选所引 claim 的 refs；不会替候选引入
    无关 claim 的证据。不存在于快照的引用保留并单列，不伪造证据或真值。
    """
    template = playbook(family)
    source = payload if isinstance(payload, dict) else {}
    candidate = source.get("candidate") if isinstance(source.get("candidate"), dict) else {}
    snapshot = source.get("snapshot") if isinstance(source.get("snapshot"), dict) else {}
    decision = source.get("decision") if isinstance(source.get("decision"), dict) else {}
    understanding = snapshot.get("understanding") or {}
    claims = (understanding.get("claims") or understanding.get("candidate_claims") or []) if isinstance(understanding, dict) else []
    refs = [*_refs(source.get("evidence_refs")), *_refs(candidate.get("evidence_ids")), *_refs(candidate.get("evidence_refs"))]
    cited = set(_refs(candidate.get("claim_ids")))
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict) and claim.get("claim_id") in cited:
                refs.extend(_refs(claim.get("evidence_refs")))
    refs = list(dict.fromkeys(refs))
    video = snapshot.get("video") if isinstance(snapshot.get("video"), dict) else {}
    raw_evidence = video.get("evidence") or []
    evidence = [deepcopy(item) for item in raw_evidence if isinstance(item, dict) and item.get("evidence_id") in refs] if isinstance(raw_evidence, list) else []
    found = {item["evidence_id"] for item in evidence}
    missing = [name for name, value in (("task_id", source.get("task_id")), ("candidate", candidate), ("snapshot.video.evidence", raw_evidence)) if not value]
    common = {
        "task_id": source.get("task_id"), "candidate_id": candidate.get("candidate_id"),
        "candidate": deepcopy(candidate), "evidence_refs": refs, "evidence": evidence,
        "unresolved_evidence_refs": [ref for ref in refs if ref not in found],
        "evidence_status": "references_only_requires_review", "missing_context": missing,
        "human_feedback": deepcopy(decision), "expected_revision_is_ground_truth": False,
        "family": template["family"], "requested_family": template["requested_family"],
        "status": "hypothesis", "first_failure": "unconfirmed", "escaped_defense": "unconfirmed",
        "release_eligible": False, "execution_mode": "manual_plan_only",
        "target_locations": template["target_locations"],
    }
    specs = [
        {"kind": "diagnostic", **step} for step in template["diagnostic_steps"]
    ] + [
        {"kind": "conditional_change", **option} for option in template["change_options"]
    ] + [
        {"kind": "regression", "action": "由审核者补齐原案例复现，再逐项实现并运行测试规格；此处不执行。", "test_cases": template["test_cases"]},
        {"kind": "acceptance", "action": "记录修复前后同集误拦及人工负担，独立审核后另走发布门禁。", "acceptance": template["acceptance"]},
    ]
    return [
        {**deepcopy(common), "work_item_id": f"{template['family']}:{index:02d}", **deepcopy(spec)}
        for index, spec in enumerate(specs, 1)
    ]
