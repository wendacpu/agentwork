"""守住「运行时入口必须接共享案例库」这条接线。

漏接的后果不是报错，而是静默退化：案例库为空，检索恒空，A/B 两臂跑出来一模一样，
还查不出原因。本轮就是这样漏掉了 `scripts/run_manifest.py` 等 4 个真跑脚本。
静态扫描比逐个记忆可靠。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# 刻意不接共享库的入口：它们是对照实验，接了反而污染。
INTENTIONALLY_ISOLATED = {"scripts/verify_constraint_ab.py"}

# 给真实 pipeline 造 Storage、且任务库路径来自 config.output_dir 的构造点。
_CONSTRUCT = re.compile(r"Storage\((?:[^()]|\([^()]*\))*\)", re.S)


def _runtime_constructs() -> list[tuple[Path, str]]:
    found = []
    paths = [*ROOT.glob("agent_system/*.py"), *ROOT.glob("scripts/*.py")]
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if rel in INTENTIONALLY_ISOLATED:
            continue
        source = path.read_text(encoding="utf-8")
        for match in _CONSTRUCT.finditer(source):
            call = match.group(0)
            if "config.output_dir" in call or "config, " in call and "output_dir" in call:
                found.append((path, call))
    return found


def test_runtime_entrypoints_pass_case_bank_path():
    offenders = []
    for path, call in _runtime_constructs():
        if "case_bank_path" not in call:
            offenders.append(f"{path.relative_to(ROOT)}: {' '.join(call.split())[:120]}")
    assert not offenders, "以下入口没接共享案例库，案例检索会静默为空：\n" + "\n".join(offenders)


def test_isolated_experiment_declares_intent():
    """故意隔离的入口要显式写 case_bank_isolated，避免被后人「顺手统一」。"""
    source = (ROOT / "scripts" / "verify_constraint_ab.py").read_text(encoding="utf-8")
    assert "case_bank_isolated=True" in source


def test_two_arms_resolve_to_different_case_supply():
    """A 臂必须真的看得到案例，B 臂必须真的一条都看不到。"""
    from agent_system.config import RunConfig, resolve_case_bank
    import sqlite3

    def case_count(config_path: str) -> int:
        resolved = resolve_case_bank(RunConfig.from_json(ROOT / config_path))
        if not resolved:
            return 0
        bank = Path(resolved)
        if not bank.is_absolute():
            bank = ROOT / bank
        if not bank.exists():
            pytest.skip(f"案例库不存在：{bank}")
        conn = sqlite3.connect(bank.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
        finally:
            conn.close()

    assert case_count("config.memory-ab-arm-a.json") > 0, "A 臂必须能读到案例"
    assert case_count("config.memory-ab-arm-b.json") == 0, "B 臂必须为空，否则不是对照"


def test_snapshot_contains_no_calibration_videos():
    """留一法快照不许含评测集视频自己的案例，否则 A 臂在抄答案。"""
    import json
    import sqlite3

    bank = ROOT / "runtime" / "cases_memory_ab.db"
    manifest = ROOT / "templates" / "eval_manifest.json"
    if not bank.exists() or not manifest.exists():
        pytest.skip("快照或清单不存在")
    excluded = {s["video_id"] for s in json.loads(manifest.read_text(encoding="utf-8"))["samples"]}
    conn = sqlite3.connect(bank.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        leaked = []
        for (payload,) in conn.execute("SELECT payload FROM cases WHERE confirmed=1"):
            video_id = str((json.loads(payload) or {}).get("video_id") or "")
            assert video_id, "无 video_id 的案例无法判归属，不应进快照"
            if video_id in excluded:
                leaked.append(video_id)
    finally:
        conn.close()
    assert not leaked, f"快照泄漏了评测集视频：{sorted(set(leaked))}"
