# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/pipeline.py"
doc = io.open(p, encoding="utf-8").read()

funcs = '''def _apply_conflict_gate(understanding: Any, boundary: Any) -> Any:
    """冲突闸门：medium/high 冲突涉及的 claim 从 usable 降到 limited。"""
    conflicts = getattr(understanding, "conflicts", None) or []
    if not conflicts or not getattr(boundary, "decisions", None):
        return boundary
    evidence_to_claims: dict[str, list[str]] = {}
    for claim in understanding.claims:
        for ev in claim.evidence_refs or []:
            evidence_to_claims.setdefault(str(ev), []).append(claim.claim_id)
    decisions = {d.claim_id: d for d in boundary.decisions}
    for conflict in conflicts:
        severity = str(conflict.get("severity") or "").lower()
        if severity not in {"medium", "high"}:
            continue
        touched: set[str] = set()
        for ev in conflict.get("evidence_ids") or []:
            touched.update(evidence_to_claims.get(str(ev), []))
        for claim_id in touched:
            decision = decisions.get(claim_id)
            if decision is None or decision.decision != "usable":
                continue
            decision.decision = "limited"
            decision.required_limits = list(decision.required_limits) + [
                "存在冲突（%s）：%s" % (conflict.get("conflict_id", ""), str(conflict.get("description") or "")[:60])
            ]
            decision.reason = (decision.reason or "") + " | 冲突闸门：降级为 limited"
    return boundary


def _apply_numeric_conflict_gate(video: Any, understanding: Any, boundary: Any) -> Any:
    """数字冲突：ASR-only 的 claim 里出现标题没有的数字 → 降级为 limited（以标题为准）。"""
    title_numbers = set(re.findall(r"\\d+", str(getattr(video, "title", "") or "")))
    if not title_numbers:
        return boundary
    decisions = {d.claim_id: d for d in boundary.decisions}
    for claim in understanding.claims:
        numbers = set(re.findall(r"\\d+", claim.text or ""))
        if not numbers:
            continue
        evs = [str(x) for x in (claim.evidence_refs or [])]
        if not evs or not all(x.startswith("e-asr") for x in evs):
            continue
        extra = numbers - title_numbers
        if not extra:
            continue
        decision = decisions.get(claim.claim_id)
        if decision is None or decision.decision != "usable":
            continue
        decision.decision = "limited"
        decision.required_limits = list(decision.required_limits) + [
            "数字以标题为准（标题：%s）；ASR 里的 %s 不是已证实价格" % ("/".join(sorted(title_numbers)), "/".join(sorted(extra)))
        ]
        decision.reason = (decision.reason or "") + " | 数字冲突闸门：ASR 数字与标题不一致，以标题为准"
    return boundary


''' + 'def _normalize_boundary(raw: dict[str, Any], claim_texts: dict[str, str] | None = None) -> BoundarySet:'

anchor = 'def _normalize_boundary(raw: dict[str, Any], claim_texts: dict[str, str] | None = None) -> BoundarySet:'
if anchor in doc and "_apply_conflict_gate" not in doc:
    doc = doc.replace(anchor, funcs, 1); print("函数已加")
else:
    print("函数 MISS")

old = '''            boundary, guarded = _apply_primary_evidence_guard(boundary, primary_audit, understanding)
            if guarded:
                self.storage.event(task_id, "N05", "primary_evidence_downgraded", {"downgraded_claims": guarded, "flagged_total": len(primary_audit.get("claims") or {})})'''
new = old + '''
            # 冲突闸门：冲突记录不能只躺在 state 里，必须下沉到 boundary 降级 claim。
            before_conflict = {d.claim_id: d.decision for d in boundary.decisions}
            boundary = _apply_conflict_gate(understanding, boundary)
            boundary = _apply_numeric_conflict_gate(video, understanding, boundary)
            conflict_downgraded = sorted(cid for cid, dec in before_conflict.items() if dec == "usable" and next((d.decision for d in boundary.decisions if d.claim_id == cid), dec) != "usable")
            if conflict_downgraded:
                self.storage.event(task_id, "N05", "conflict_downgraded", {"downgraded_claims": conflict_downgraded})'''
if old in doc:
    doc = doc.replace(old, new, 1); print("调用已加")
else:
    print("调用 MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
