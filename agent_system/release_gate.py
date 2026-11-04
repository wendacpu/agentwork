"""本地人工发布门禁：只写证据目录，不应用提案或修改生产源码。

JSON 是可读证据，SQLite ledger 才是状态/审核的可信来源。安全边界是受控的
本机证据目录：本模块防止伪造/篡改 JSON，不声称抵抗能改写 SQLite 或 Python
运行环境的攻击者。调用方必须另外核对 proposal 的存在性和有效状态。

流程：validate_release -> record_proposal_verification（每个提案）->
approve_release -> check_active_release；撤销后必须重新验证才能再次批准。
checklist 的 original_case / positive_cases / negative_cases 各自必须为
{"verified": True, "evidence": "具体案例标识、期望/实际结果与审核依据"}。
这只是明确的人工审核声明，不是自动推断的修复有效性，也不是身份认证系统。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


LEDGER_NAME = "release_gate.sqlite3"
CHECKLIST_KEYS = ("original_case", "positive_cases", "negative_cases")


class ReleaseGateError(ValueError):
    """证据、人工确认或发布状态不满足门禁。"""


def _root(root):
    path = Path(root).resolve()
    if not path.is_dir():
        raise ReleaseGateError("root 必须是存在的目录")
    return path


def _fingerprint(root, patterns):
    digest = hashlib.sha256()
    paths = sorted({p for pattern in patterns for p in root.glob(pattern)})
    for path in paths:
        if any(part in {"__pycache__", ".pytest_cache", "cache"} for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ReleaseGateError("指纹范围不允许符号链接或越界文件")
        if path.is_file():
            name = path.relative_to(root).as_posix().encode("utf-8")
            data = path.read_bytes()
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def source_fingerprint(root):
    """稳定 SHA256：源码、真实规则/验证子目录和规则包，排除 cache。

    兼容项目根 rules/；本项目实际规则位于 agent_system/rules/，验证逻辑
    位于 agent_system/verification/，二者同属受门禁保护的生产源码。
    """
    return _fingerprint(_root(root), ("agent_system/*.py", "rules/*.py", "rulepacks/*.md",
                                     "agent_system/rules/*.py", "agent_system/verification/*.py"))


def _tests_fingerprint(root):
    return _fingerprint(root, ("tests/**/*.py", "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg"))


def _directory(output_dir):
    path = Path(output_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connect(output_dir):
    directory = _directory(output_dir)
    db = directory / LEDGER_NAME
    if db.is_symlink():
        raise ReleaseGateError("ledger 不允许符号链接")
    conn = sqlite3.connect(db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, root TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            validation_id TEXT NOT NULL REFERENCES evidence(id), proposal_id TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(validation_id, proposal_id)
        );
        CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY REFERENCES evidence(id), root TEXT NOT NULL,
            validation_id TEXT UNIQUE NOT NULL REFERENCES evidence(id), status TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_release
            ON releases(root) WHERE status='active';
    """)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _save(conn, directory, kind, root, payload):
    identity = uuid4().hex
    path = directory / f"{kind}-{identity}.json"
    payload = dict(payload, id=identity, kind=kind, root=str(root), created_at=_now())
    raw = _json(payload).encode("utf-8")
    # 排他创建，UUID 碰撞或已有文件都不能覆盖历史证据。
    with path.open("xb") as handle:
        handle.write(raw)
    conn.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?)",
                 (identity, kind, str(root), str(path), hashlib.sha256(raw).hexdigest(), _json(payload)))
    return payload, path


def _load(conn, path, kind, root):
    path = Path(path).resolve()
    row = conn.execute("SELECT * FROM evidence WHERE path=? AND kind=? AND root=?",
                       (str(path), kind, str(root))).fetchone()
    if row is None:
        raise ReleaseGateError("证据未由本模块 ledger 登记，或属于其他项目")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ReleaseGateError("证据缺失或 JSON 无效") from exc
    if hashlib.sha256(raw).hexdigest() != row["sha256"] or payload != json.loads(row["payload"]):
        raise ReleaseGateError("证据被篡改，与 ledger 不一致")
    return payload


def _ids(proposal_ids):
    if isinstance(proposal_ids, (str, bytes)):
        raise ReleaseGateError("proposal_ids 必须是非空 ID 列表")
    try:
        values = list(proposal_ids)
    except TypeError as exc:
        raise ReleaseGateError("proposal_ids 必须是非空 ID 列表") from exc
    if not values or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ReleaseGateError("proposal_ids 必须是非空字符串列表")
    if len(set(values)) != len(values):
        raise ReleaseGateError("proposal_ids 不能重复")
    return values


def _human(reviewer, confirmed):
    if confirmed is not True or not isinstance(reviewer, str) or not reviewer.strip():
        raise ReleaseGateError("必须明确 confirmed=True 并提供 reviewer")
    return reviewer.strip()


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")


def validate_release(root, proposal_ids, output_dir):
    """执行真实 pytest，成功/失败/超时均写入不可覆盖 JSON 和 ledger。

    只接受至少一个 passed 且无 skipped 的完整运行；退出码 0 本身不充分。
    保存 subprocess 的真实 returncode；超时/启动失败时为 None，不伪造退出码。
    """
    root = _root(root)
    proposals = _ids(proposal_ids)
    before = source_fingerprint(root)
    tests_before = _tests_fingerprint(root)
    command = [sys.executable, "-m", "pytest", "-q", "tests"]
    env = dict(os.environ)
    # 不允许环境变量通过 -k、--collect-only、插件等偷偷改写验证命令。
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    returncode, stdout, stderr, timed_out, error = None, "", "", False, None
    try:
        result = subprocess.run(command, cwd=str(root), capture_output=True, text=True,
                                timeout=120, shell=False, env=env)
        returncode, stdout, stderr = result.returncode, _text(result.stdout), _text(result.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out, stdout, stderr, error = True, _text(exc.stdout), _text(exc.stderr), "timeout"
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    after = source_fingerprint(root)
    tests_after = _tests_fingerprint(root)
    # 使用 pytest 终结摘要而非任意测试打印的 '1 passed'。
    summary = next((line.strip(" =") for line in reversed(stdout.splitlines()) if re.search(r"\bin \d+(?:\.\d+)?s\b", line)), "")
    counts = {key: sum(int(n) for n in re.findall(rf"\b(\d+) {key}\b", summary))
              for key in ("passed", "skipped", "failed", "error", "errors", "deselected", "xfailed", "xpassed")}
    passed = (returncode == 0 and not timed_out and error is None and counts["passed"] > 0
              and not any(counts[k] for k in counts if k != "passed")
              and before == after and tests_before == tests_after)
    payload = {"proposal_ids": proposals, "fingerprint": before, "fingerprint_after": after,
               "tests_fingerprint": tests_before, "tests_fingerprint_after": tests_after,
               "command": command, "timeout_seconds": 120, "returncode": returncode,
               "stdout": stdout, "stderr": stderr, "timed_out": timed_out, "error": error,
               "test_counts": counts, "passed": passed}
    directory = _directory(output_dir)
    with _connect(directory) as conn:
        payload, path = _save(conn, directory, "validation", root, payload)
    return dict(payload, validation_id=payload["id"], validation_path=str(path))


def _valid_validation(conn, validation_path, root):
    validation = _load(conn, validation_path, "validation", root)
    if validation.get("passed") is not True or validation.get("returncode") != 0:
        raise ReleaseGateError("验证未通过或为空测/跳过/超时")
    if source_fingerprint(root) != validation["fingerprint"]:
        raise ReleaseGateError("源码 fingerprint 已漂移，必须重新验证")
    if _tests_fingerprint(root) != validation["tests_fingerprint"]:
        raise ReleaseGateError("测试或 pytest 配置已漂移，必须重新验证")
    return validation


def _checklist(checklist):
    if not isinstance(checklist, dict) or set(checklist) != set(CHECKLIST_KEYS):
        raise ReleaseGateError("checklist 必须覆盖 original_case、positive_cases、negative_cases")
    result = {}
    for key in CHECKLIST_KEYS:
        item = checklist[key]
        if (not isinstance(item, dict) or item.get("verified") is not True
                or not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
            raise ReleaseGateError(f"{key} 必须人工 verified=True 且提供具体 evidence")
        result[key] = {"verified": True, "evidence": item["evidence"].strip()}
    return result


def record_proposal_verification(root, validation_path, proposal_id, reviewer, checklist,
                                 output_dir, confirmed=False):
    """人工逐提案审核，审核绑定当前验证/源码；不读反馈或提案数据库。"""
    root = _root(root)
    reviewer = _human(reviewer, confirmed)
    checklist = _checklist(checklist)
    with _connect(output_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        validation = _valid_validation(conn, validation_path, root)
        if proposal_id not in validation["proposal_ids"]:
            raise ReleaseGateError("提案不属于本次验证")
        if conn.execute("SELECT 1 FROM releases WHERE validation_id=?", (validation["id"],)).fetchone():
            raise ReleaseGateError("验证已批准，不能修改其审核记录")
        payload = {"validation_id": validation["id"], "proposal_id": proposal_id,
                   "fingerprint": validation["fingerprint"], "reviewer": reviewer,
                   "checklist": checklist, "verified": True, "reviewed_at": _now()}
        conn.execute("INSERT OR REPLACE INTO reviews VALUES (?, ?, ?)",
                     (validation["id"], proposal_id, _json(payload)))
    return payload


def _reviews(conn, validation):
    reviews = []
    for proposal_id in validation["proposal_ids"]:
        row = conn.execute("SELECT payload FROM reviews WHERE validation_id=? AND proposal_id=?",
                           (validation["id"], proposal_id)).fetchone()
        if row is None:
            raise ReleaseGateError(f"提案 {proposal_id} 缺少人工 verified checklist")
        review = json.loads(row["payload"])
        if (review.get("verified") is not True or not review.get("reviewer")
                or review.get("validation_id") != validation["id"]
                or review.get("proposal_id") != proposal_id
                or review.get("fingerprint") != validation["fingerprint"]):
            raise ReleaseGateError("人工审核与验证不一致")
        _checklist(review.get("checklist"))
        reviews.append(review)
    return reviews


def approve_release(root, validation_path, reviewer, output_dir, confirmed=False):
    """批准证据和审核，不复制、打补丁、执行或修改任何生产源码。"""
    root = _root(root)
    reviewer = _human(reviewer, confirmed)
    directory = _directory(output_dir)
    with _connect(directory) as conn:
        conn.execute("BEGIN IMMEDIATE")
        validation = _valid_validation(conn, validation_path, root)
        if conn.execute("SELECT 1 FROM releases WHERE validation_id=?", (validation["id"],)).fetchone():
            raise ReleaseGateError("验证已使用（含撤销），必须重新验证")
        reviews = _reviews(conn, validation)
        payload, path = _save(conn, directory, "release", root,
                              {"validation_id": validation["id"], "validation_path": str(Path(validation_path).resolve()),
                               "fingerprint": validation["fingerprint"], "proposal_ids": validation["proposal_ids"],
                               "reviewer": reviewer, "reviews": reviews, "status": "approved"})
        conn.execute("UPDATE releases SET status='superseded' WHERE root=? AND status='active'", (str(root),))
        conn.execute("INSERT INTO releases VALUES (?, ?, ?, 'active')", (payload["id"], str(root), validation["id"]))
    return dict(payload, manifest_id=payload["id"], manifest_path=str(path))


def revoke_release(root, manifest_path, reviewer, reason, output_dir, confirmed=False):
    """不可逆撤销批准（不回滚源码）；理由作为新证据保留，不能复用旧验证。"""
    root = _root(root)
    reviewer = _human(reviewer, confirmed)
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseGateError("撤销必须提供 reason")
    directory = _directory(output_dir)
    with _connect(directory) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manifest = _load(conn, manifest_path, "release", root)
        release = conn.execute("SELECT status FROM releases WHERE id=? AND root=?", (manifest["id"], str(root))).fetchone()
        if release is None or release["status"] == "revoked":
            raise ReleaseGateError("发布不存在或已撤销")
        payload, path = _save(conn, directory, "revocation", root,
                              {"manifest_id": manifest["id"], "reviewer": reviewer, "reason": reason.strip()})
        conn.execute("UPDATE releases SET status='revoked' WHERE id=?", (manifest["id"],))
    return dict(payload, revocation_path=str(path))


def check_active_release(root, output_dir):
    """未配置发布不阻断开发；已有发布则 fail-closed，并提供结构化原因。

    撤销最后一个发布不是“未配置”，而是显式阻断；不会自动回退至旧发布。
    """
    root = _root(root)
    directory = Path(output_dir).resolve()
    if not (directory / LEDGER_NAME).exists():
        orphaned = directory.exists() and any(directory.glob("release-*.json"))
        return {"allowed": not orphaned, "status": "invalid" if orphaned else "unconfigured",
                "reason": "批准证据存在但 ledger 缺失" if orphaned else "尚未配置发布，不阻断开发"}
    try:
        with _connect(directory) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT r.*, e.path FROM releases r JOIN evidence e ON e.id=r.id WHERE r.root=? AND r.status='active'",
                               (str(root),)).fetchone()
            if row is None:
                configured = conn.execute("SELECT 1 FROM releases WHERE root=?", (str(root),)).fetchone()
                return {"allowed": not bool(configured), "status": "revoked" if configured else "unconfigured",
                        "reason": "发布已撤销，必须重新验证和批准" if configured else "尚未批准发布，不阻断开发"}
            manifest = _load(conn, row["path"], "release", root)
            validation = _valid_validation(conn, manifest["validation_path"], root)
            reviews = _reviews(conn, validation)
            if (manifest["validation_id"] != row["validation_id"] or manifest["validation_id"] != validation["id"]
                    or manifest["reviews"] != reviews or manifest["fingerprint"] != validation["fingerprint"]):
                raise ReleaseGateError("批准 manifest 与验证/人工审核不一致")
            return {"allowed": True, "status": "active", "manifest_id": manifest["id"],
                    "manifest_path": row["path"], "fingerprint": manifest["fingerprint"],
                    "proposal_ids": list(manifest["proposal_ids"])}
    except (ReleaseGateError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        return {"allowed": False, "status": "invalid", "reason": str(exc)}
