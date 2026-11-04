"""时间码这类素材误差只记录、不阻断。

素材的时间码由音频分片而来，越界几十毫秒（甚至几十秒）都出现过。定位不准不等于
这段文字不可信——它引用了什么才决定可信度，不是它标在第几秒。所以时间码问题
只进 reference_audit 与 reference_locator_notes，不能把整条视频判成零产出。
"""
from __future__ import annotations

from agent_system.pipeline import LOCATOR_ISSUE_CODES, _blocking_reference_issues
from agent_system.types import BoundarySet


def _issue(code: str, path: str = "video.evidence[3]"):
    return {"path": path, "field": "locator", "code": code, "value": "asr:9.3-53.5", "message": "时间码超出视频时长"}


def test_locator_codes_are_the_tolerated_set():
    assert LOCATOR_ISSUE_CODES == {"location_unknown", "location_invalid"}


def test_locator_overrun_does_not_block_the_video():
    issues = [_issue("location_invalid"), _issue("location_unknown")]
    assert _blocking_reference_issues(issues, BoundarySet(decisions=[])) == []


def test_structural_errors_still_block():
    issues = [_issue("location_invalid"), _issue("dangling_ref", "understanding.claims[0]")]
    out = _blocking_reference_issues(issues, BoundarySet(decisions=[]))
    assert [i["code"] for i in out] == ["dangling_ref"]
