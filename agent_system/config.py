from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def default_case_bank() -> str:
    """workspace 级共享案例库：让已确认案例跨实验目录对后续运行可见。"""
    return str(Path(__file__).resolve().parent.parent / "runtime" / "cases.db")


def default_cache_bank() -> str:
    """workspace 级模型调用缓存：同一份请求只付一次钱。

    为什么必须有它：实测一批 15 条视频 2.75M prompt tokens 里，42% 花在
    「理解 / 边界 / 看点 / 主旨复核」这四个确定性节点上。它们的输入是
    （视频证据 + prompt 版本 + 模型），与批次无关——62 个 config 快照反复跑
    同一批视频，每次都从零重算一遍同一件事。

    缓存键是请求内容哈希（system + user + schema + model），不含 task_id，
    所以跨任务、跨批次都能命中；prompt / rubric / 模型任一变化，哈希就变，
    旧结果不会冒充新结果。
    """
    return str(Path(__file__).resolve().parent.parent / "runtime" / "model_cache.db")


def resolve_cache_bank(config: "RunConfig") -> str:
    """显式配置优先；隔离实验返回空串（退回只在任务库内缓存）；默认共享。"""
    explicit = getattr(config, "cache_bank", "") or ""
    if explicit:
        return explicit
    if getattr(config, "cache_bank_isolated", False):
        return ""
    return default_cache_bank()


def resolve_case_bank(config: "RunConfig") -> str:
    """显式配置优先；隔离实验退回每目录一份；默认共享。

    返回空串表示「案例与任务库同库」（旧行为）。
    用 getattr 容错：调试台和测试会传入不含这些字段的轻量配置对象。
    """
    explicit = getattr(config, "case_bank", "") or ""
    if explicit:
        return explicit
    if getattr(config, "case_bank_isolated", False):
        return ""
    return default_case_bank()


@dataclass
class RunConfig:
    rule_version: str = "temporary-v1"
    prompt_pack_version: str = "red-dot-v0.2-modular"
    rubric_version: str = "red-dot-review-v2-uncalibrated"
    memory_version: str = "dev-empty-v1"
    target_count: int = 3
    max_chars: int = 20
    # 模型输出坏 JSON 是当前最大的零产出来源（一次坏就是一次整任务失败），
    # 2 次尝试实测不足：同一任务两次都坏的比例仍高，提到 3 次。
    schema_retry_limit: int = 2
    max_rewrite_rounds: int = 2
    max_rewrite_candidates: int = 3
    rewrite_candidate_count: int = 2
    # 机械压缩最多尝试两次；同一标题的压缩与语义重写共享最多两轮的谱系预算。
    max_mechanical_repairs: int = 2
    memory_call_limit: int = 6
    memory_calls_per_round: int = 2
    reference_time: str = ""
    ocr_mode: str = "filtered"
    model_provider: str = "mock"
    base_url: str = ""
    api_key_env: str = ""
    reasoning_effort: str = ""
    # 单次模型请求超时（秒）。默认 300 太宽：一次评审请求超时 300 秒，批失败再逐条
    # 重试，单轮就二十分钟以上，而超时通常意味着模型输出已退化、再等也不会好。
    # 实测 37K 输入的评审在 60–90 秒内返回，120 秒足够；失败尽快转下一条路径。
    model_timeout: int = 120
    # 结构化输出：off=只用 json_object（向后兼容的默认值）；auto=有登记 schema 时
    # 用 json_schema，网关拒绝则自动回退 json_object；on=强制，拒绝即报错。
    # json_object 只保证「是合法 JSON」，不保证字段齐全与类型正确，于是结构校验
    # 实际落在解码之后的手写检查上——模型可以合法地少写一个必填键，整批评审作废。
    structured_output: str = "off"
    # 评审是否默认每个候选单独调用。大批次实测：一次评 3 个候选时，模型要么写不全
    # 必填字段（1100–2300 tokens），要么输出失控到 max_tokens 被砍断（16384）。
    # 拆成单候选后输出降到 900–2400 tokens 且一次通过，prompt 前缀仍可命中缓存。
    # 默认 false = 保留旧行为（先整批、失败才降级）。
    review_per_candidate: bool = False
    # 评审载荷范围：full=原样全量（向后兼容）；compact=按引用闭包裁剪证据与信号。
    # R01 拿到的是上游所有节点的输出转储，72K 字符里 evidence 有 36/68 条没被任何
    # 结论引用、risk_signals 65 条每条重复同一组常量字段。裁剪不删被引用的证据，
    # 一手材料始终保留，只去掉没进入判断链路的部分。
    review_input_scope: str = "full"
    generation_model: str = "mock"
    review_model: str = "mock"
    asset_root: str = "/Users/wanting/program/新项目/复杂agent/recent-portrait-100"
    output_dir: str = "runtime"
    table: str = ""
    # 独立案例库路径；空 = 未显式配置，由 resolve_case_bank 决定。
    case_bank: str = ""
    # 做隔离对照实验时置 true：案例只写本实验目录，不读历史案例。
    # 默认 false = 用 workspace 级共享库，否则每次新实验都从零开始积累，
    # 历史人工判定锁死在各自 output_dir 里，案例回流实际为 0（实测命中率 3.7%）。
    case_bank_isolated: bool = False
    # 模型调用缓存库路径；空 = 未显式配置，由 resolve_cache_bank 决定（默认共享）。
    # 缓存键是请求内容哈希，不含 task_id：同一份请求在同一批次内重复调用、
    # 或在另一个批次重复跑同一批视频，都只付一次钱。
    cache_bank: str = ""
    # 做隔离对照实验时置 true：只在任务库内缓存，不读写共享缓存。
    # 需要「同一输入每次都真的问模型」时（例如测温度/非确定性）必须打开它，
    # 否则共享缓存会把第一次的随机结果固化成唯一答案。
    cache_bank_isolated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # 外部实时核查配置（P2）：哪些品类核查、refuted 阈值、单视频核查条数上限
    verification_enabled: bool = False
    # 核查选择方式（与 verification_enabled 是两层，不是替代关系）：
    #   off     完全不核查（即使 enabled=True 也停）
    #   static  按品类路由决定查不查——历史行为，默认值，保证已有配置语义不变
    #   dynamic 模型在理解阶段给出核查建议，程序按「强制条件 ∪ 模型建议」决定
    verification_mode: str = "static"
    # 强制核查清单：命中即必须核查（dynamic 下模型无权关闭）。默认空，
    # 由业务侧按政策对齐后显式配置——见 docs/verification-dynamic-routing.md 的推荐清单。
    verification_mandatory_domains: list[str] = field(default_factory=list)
    verification_mandatory_keywords: list[str] = field(default_factory=list)
    # dynamic 下命中白名单为空时是否停止核查：默认 True，宁可不查也不全网乱搜。
    verification_require_whitelist: bool = True
    verifiable_domains: list[str] = field(default_factory=lambda: ["时事社会"])
    excluded_domains: list[str] = field(default_factory=list)
    verifiable_claim_types: list[str] = field(default_factory=lambda: ["observable", "source_claim"])
    max_verification_claims: int = 3
    refute_min_sources: int = 2
    refute_confidence_threshold: float = 0.8
    verification_provider: str = "none"  # none / wsa
    verification_secret_id_env: str = "WSA_SECRET_ID"
    verification_secret_key_env: str = "WSA_SECRET_KEY"

    @classmethod
    def from_json(cls, path: str | Path) -> "RunConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def apply_env(self) -> "RunConfig":
        for key, value in {
            "verification_enabled": os.getenv("AGENT_VERIFICATION_ENABLED"),
            "verification_mode": os.getenv("AGENT_VERIFICATION_MODE"),
            "verification_provider": os.getenv("AGENT_VERIFICATION_PROVIDER"),
            "verification_secret_id_env": os.getenv("AGENT_VERIFICATION_SECRET_ID_ENV"),
            "verification_secret_key_env": os.getenv("AGENT_VERIFICATION_SECRET_KEY_ENV"),
        }.items():
            if value is not None:
                setattr(self, key, value.strip().lower() in {"1", "true", "yes", "on"} if key == "verification_enabled" else value)
        for key, value in {
            "asset_root": os.getenv("AGENT_ASSET_ROOT"),
            "model_provider": os.getenv("AGENT_MODEL_PROVIDER"),
            "generation_model": os.getenv("AGENT_GENERATION_MODEL"),
            "base_url": os.getenv("AGENT_BASE_URL"),
            "api_key_env": os.getenv("AGENT_API_KEY_ENV"),
            "review_model": os.getenv("AGENT_REVIEW_MODEL"),
            "output_dir": os.getenv("AGENT_OUTPUT_DIR"),
        }.items():
            if value:
                setattr(self, key, value)
        return self

