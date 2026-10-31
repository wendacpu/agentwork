from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Decision = Literal["usable", "limited", "excluded"]
TaskStatus = Literal[
    "prepared", "generating", "reviewing", "rewriting", "pending_human",
    "approved", "stopped", "technical_failed",
]


def to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    return value


@dataclass
class SourceRef:
    source_id: str
    name: str | None = None
    url: str | None = None
    verification: str = "unknown"
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    evidence_id: str
    field: str
    text: str
    locator: str | None = None
    derived_from: list[str] = field(default_factory=list)
    quality: str = "raw"


@dataclass
class VideoInput:
    video_id: str
    title: str
    author: str
    author_uid: int | None
    url: str
    domain: dict[str, str]
    format: str
    published_at: str | None
    duration: float | None
    dimensions: dict[str, int]
    description: str
    caption: str
    asr_segments: list[dict[str, Any]]
    ocr_segments: list[dict[str, Any]]
    evidence: list[Evidence]
    asset_paths: dict[str, Any]
    provenance: dict[str, Any]


@dataclass
class Claim:
    claim_id: str
    text: str
    claim_type: str
    evidence_refs: list[str]
    time_refs: list[str] = field(default_factory=list)
    evidence_status: str = "unknown"
    original_source: dict[str, Any] | None = None
    transmission_path: list[dict[str, Any]] = field(default_factory=list)
    temporal_status: str = "unknown"
    assertion_permission: str = "unknown"
    entities: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SemanticUnit:
    """视频级语义单元：一个跨片段/跨模态的完整意思，而非单句或单帧摘录。"""
    unit_id: str
    meaning: str
    claim_ids: list[str]
    evidence_ids: list[str]
    kind: str = "support"
    time_range: tuple[float, float] | None = None
    role_in_video: str = ""
    # 归纳的断言级别：grounded=关系性归纳能由单元内 claim 组合直接推出；
    # inferred=模型解读、claim 仅部分支撑；speculative=无 claim 支撑（禁止作确定看点）
    grounding: str = "grounded"
    # 印证源是一手证据（ASR/OCR/帧）：{status∈consistent/partial/conflict/unverified, evidence_ids, notes}
    # description/caption/searchDesc 是理解线索，不作印证源
    corroboration: dict[str, Any] = field(default_factory=dict)


@dataclass
class Understanding:
    main_format: str
    attributes: list[str]
    claims: list[Claim]
    event_times: list[dict[str, Any]]
    extracted_sources: list[SourceRef]
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    semantic_units: list[SemanticUnit] = field(default_factory=list)


@dataclass
class ClaimDecision:
    claim_id: str
    supported_statement: str | None
    decision: Decision
    evidence_refs: list[str]
    required_limits: list[str]
    forbidden_inferences: list[str]
    rule_ids: list[str]
    reason: str
    claim_type: str = "unknown"
    evidence_status: str = "unknown"
    original_source: dict[str, Any] | None = None
    transmission_path: list[dict[str, Any]] = field(default_factory=list)
    temporal_status: str = "unknown"
    assertion_permission: str = "unknown"
    entities: list[dict[str, Any]] = field(default_factory=list)
    attribution_required: bool = False
    required_source_phrase: str | None = None
    source_evidence_refs: list[str] = field(default_factory=list)
    # D02「暂缓核验」：不进入可写集合，但保留结构化的核验任务
    verification_required: bool = False


@dataclass
class UnitDecision:
    """语义单元能否作为看点来源的判断；原子 claim 的事实核验仍在其 decisions 里。"""
    unit_id: str
    decision: Decision
    required_limits: list[str] = field(default_factory=list)
    forbidden_inferences: list[str] = field(default_factory=list)
    attribution_required: bool = False
    required_source_phrase: str | None = None
    reason: str = ""


@dataclass
class BoundarySet:
    decisions: list[ClaimDecision]
    unresolved_issues: list[dict[str, Any]] = field(default_factory=list)
    global_requirements: dict[str, Any] = field(default_factory=dict)
    unit_decisions: list[UnitDecision] = field(default_factory=list)


@dataclass
class Highlight:
    highlight_id: str
    claim_ids: list[str]
    value: str
    representativeness: str
    delivery_evidence_refs: list[str]
    role: str = "backup"
    viewer_value: str = ""
    required_qualifiers: list[str] = field(default_factory=list)
    selection_reason: str = ""
    unit_id: str = ""
    # 钩子类型：information(信息价值)/suspense(悬念)/identity(身份认同)/conflict(冲突争议)/emotion(情绪共鸣)
    # 与观看价值(value)并列为看点的一等维度——价值回答"值不值得写"，钩子回答"勾不勾人"
    hook_type: str = ""


@dataclass
class Candidate:
    candidate_id: str
    headline: str
    highlight_id: str
    claim_ids: list[str]
    expression_strategy: str
    evidence_ids: list[str] = field(default_factory=list)
    used_case_ids: list[str] = field(default_factory=list)
    generation_round: int = 0
    parent_candidate_id: str | None = None
    # D05「必要限定在标题中的实际表达」，用于核对限定是否真的写进去了
    qualifiers_used: list[str] = field(default_factory=list)
    # 重写时模型自报「改了哪几条 issue」，用于验证是否照做、是否重蹈覆辙
    addressed_issue_ids: list[str] = field(default_factory=list)


@dataclass
class ReviewGate:
    status: str
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)


@dataclass
class QualityScore:
    score: int | None
    reason: str


@dataclass
class ReviewIssue:
    issue_id: str
    primary_dimension: str
    issue_code: str
    severity: str
    repairable: bool
    problem_span: str
    evidence_ids: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    reason: str = ""
    action: str = ""
    # D08「建议回退节点」：把问题直接归到上游节点，如 N05 / N07 / N09
    rollback_to: str = ""


@dataclass
class CandidateReview:
    candidate_id: str
    expected_takeaway: dict[str, str]
    gates: dict[str, ReviewGate]
    quality: dict[str, QualityScore]
    issues: list[ReviewIssue]
    rewrite_guidance: dict[str, list[str]]
    memory_query: dict[str, Any]
    mechanical_checks: dict[str, Any] = field(default_factory=dict)
    decision: str | None = None


@dataclass
class QualityRoute:
    action: str
    recommended_candidate_id: str | None
    eligible_candidate_ids: list[str]
    rewrite_candidate_id: str | None
    reason: str
    rewrite_round: int


@dataclass
class HumanDecision:
    candidate_id: str
    decision: str
    final_headline: str | None = None
    reason: str = ""
    reviewer: str | None = None


@dataclass
class CaseRecord:
    case_id: str
    case_type: str
    problem_type: str
    bad_example: str
    good_example: str
    applicability: str
    source_task_id: str
    rule_version: str
    confirmed: bool = False
    compatible_rule_versions: list[str] = field(default_factory=list)
    case_status: str = "draft"
    scope: str = ""
    source_evidence: list[str] = field(default_factory=list)
    version: str = "v1"

