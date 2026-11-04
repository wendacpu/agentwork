#!/usr/bin/env python3
"""把基线里判「可用 / 勉强通过」的标题沉淀进当前系统的案例库（cases 表）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_system.config import RunConfig, resolve_case_bank
from agent_system.memory import MemoryStore
from agent_system.storage import Storage
from agent_system.types import CaseRecord

TABLE = ROOT / "data" / "source" / "video_dataset.json"


def main() -> int:
    config = RunConfig.from_json(ROOT / "config.deepseek.json")
    config.apply_env()
    storage = Storage(Path(config.output_dir) / "agent.db", case_bank_path=(resolve_case_bank(config) or None))
    store = MemoryStore(storage.case_conn)
    baseline = json.loads((ROOT / "runtime" / "review" / "human-baseline.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "templates" / "eval_manifest.json").read_text(encoding="utf-8"))
    scope_by_video = {s["video_id"]: " ".join(x for x in [s.get("domain_1"), s.get("domain_2"), s.get("form")] if x) for s in manifest.get("samples", [])}

    made = {"positive": 0, "negative": 0}
    for item in baseline.get("labels", []):
        vid = item["video_id"]
        title = item.get("title") or ""
        verdict = item.get("verdict") or ""
        reason = item.get("reason") or ""
        scope = scope_by_video.get(vid, "")
        if verdict == "可用":
            case = CaseRecord(
                case_id=f"baseline-good-{vid}", case_type="expression", problem_type="表达",
                bad_example="", good_example=title, applicability="基线可用标题（裸模型直出，人工判可用）",
                source_task_id="baseline", rule_version=config.prompt_pack_version, confirmed=True,
                compatible_rule_versions=[config.prompt_pack_version], case_status="confirmed",
                scope=scope, source_evidence=[],
            )
            store.create_draft(case); made["positive"] += 1
        elif verdict == "勉强通过":
            case = CaseRecord(
                case_id=f"baseline-marginal-{vid}", case_type="expression", problem_type="表达",
                bad_example=title, good_example="", applicability=f"基线勉强通过：{reason[:160]}",
                source_task_id="baseline", rule_version=config.prompt_pack_version, confirmed=True,
                compatible_rule_versions=[config.prompt_pack_version], case_status="confirmed",
                scope=scope, source_evidence=[],
            )
            payload_extra = {"polarity": "negative"}
            store.create_draft(case); made["negative"] += 1
    print("沉淀完成：正例", made["positive"], "反例", made["negative"])
    # 统计当前库案例数
    n = storage.case_conn.execute("SELECT COUNT(*) FROM cases WHERE confirmed=1").fetchone()[0]
    print("当前库 confirmed 案例数:", n)
    storage.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
