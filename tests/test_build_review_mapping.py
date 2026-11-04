"""首轮候选清单重建：只读源批次，把单候选 mapping 换成首轮全部候选。

这层测试锁的是三条规则，它们直接决定「首轮通过率」算得准不准：

1. 优先取 generation_round=0；首轮候选若没被保留，退到最早可得轮次并标明，
   不能把重写版冒充首轮。
2. 完全没产出的视频保留为失败项，不补造候选。
3. 源批次的 mapping 与 agent.db 不被改写。
"""
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.build_review_mapping import build, earliest_round_candidates

VIDEO = {"video_id": "BV1test", "title": "测试视频"}


def _candidate(cid, title, round_no, **extra):
    return {"candidate_id": cid, "rendered_title": title, "generation_round": round_no, **extra}


def _state(candidates, delivered=None):
    return {"video": VIDEO, "candidates": candidates, "delivery_candidates": delivered or []}


def _make_batch(root: Path, tasks):
    """tasks: [(task_id, video_id, state)]"""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "agent.db")
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, video_id TEXT, status TEXT, state_json TEXT)")
    mapping = []
    for index, (task_id, video_id, state) in enumerate(tasks, start=1):
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", (task_id, video_id, "pending_human", json.dumps(state, ensure_ascii=False)))
        mapping.append({"task_id": task_id, "video_id": video_id, "blind_id": f"R{index:02d}",
                        "category": "测试", "candidate_id": "old", "title_sha256": "deadbeef"})
    conn.commit()
    conn.close()
    (root / "review_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return root


class ExtractTests(unittest.TestCase):
    def test_prefers_first_round(self):
        state = _state([
            _candidate("c1", "首轮一", 0),
            _candidate("c2", "首轮二", 0),
            _candidate("r1", "重写后", 1),
        ])
        picked, base = earliest_round_candidates(state)
        self.assertEqual(base, 0)
        self.assertEqual([cid for cid, _, _ in picked], ["c1", "c2"])

    def test_falls_back_to_earliest_when_first_round_missing(self):
        """首轮候选没留下时用最早可得轮次——但 base 会告诉上游这不是首轮。"""
        state = _state([_candidate("a2-1", "重写两次后", 2), _candidate("a2-2", "同轮另一条", 2)])
        picked, base = earliest_round_candidates(state)
        self.assertEqual(base, 2)
        self.assertEqual(len(picked), 2)

    def test_no_candidate_is_kept_as_failure_not_invented(self):
        picked, base = earliest_round_candidates({"video": VIDEO})
        self.assertEqual(picked, [])
        self.assertIsNone(base)

    def test_deduplicates_same_id_and_title(self):
        state = _state([_candidate("dup", "同一条", 0), _candidate("dup", "同一条", 0), _candidate("dup", "另一条", 0)])
        picked, _ = earliest_round_candidates(state)
        self.assertEqual([title for _, title, _ in picked], ["同一条", "另一条"])

    def test_missing_round_field_counts_as_first_round(self):
        state = _state([{"candidate_id": "x", "rendered_title": "没写轮次"}])
        picked, base = earliest_round_candidates(state)
        self.assertEqual(base, 0)
        self.assertEqual(len(picked), 1)


class BuildTests(unittest.TestCase):
    def test_rebuilds_mapping_and_keeps_source_read_only(self):
        with TemporaryDirectory() as tmp:
            src = _make_batch(Path(tmp) / "src", [
                ("t1", "BV1", _state([_candidate("c1", "甲", 0), _candidate("c2", "乙", 0)])),
                ("t2", "BV2", _state([_candidate("a2-1", "丙", 2)])),
                ("t3", "BV3", {"video": {"video_id": "BV3"}}),
            ])
            before = (src / "review_mapping.json").read_text(encoding="utf-8")

            report = build(src, Path(tmp) / "out")
            self.assertEqual(report["candidates"], 3)
            self.assertEqual(report["basis"], {"first_round": 1, "earliest_available": 1, "no_candidate": 1})

            rebuilt = json.loads((Path(tmp) / "out" / "review_mapping.json").read_text(encoding="utf-8"))
            self.assertEqual(len(rebuilt[0]["candidates"]), 2)
            self.assertEqual(rebuilt[0]["round_basis"], "first_round")
            self.assertEqual(rebuilt[1]["round_basis"], "earliest_available")
            self.assertEqual(rebuilt[1]["earliest_round"], 2)
            self.assertEqual(rebuilt[2]["candidates"], [])
            self.assertEqual(rebuilt[2]["round_basis"], "no_candidate")

            # 未产出项留在清单里，不因为没标题被删掉
            self.assertEqual(len(rebuilt), 3)
            # 源批次一个字节都不动
            self.assertEqual((src / "review_mapping.json").read_text(encoding="utf-8"), before)
            self.assertFalse((src / "SOURCE.json").exists())

    def test_refuses_to_overwrite_source_batch(self):
        with TemporaryDirectory() as tmp:
            src = _make_batch(Path(tmp) / "src", [("t1", "BV1", _state([_candidate("c1", "甲", 0)]))])
            with self.assertRaises(SystemExit):
                build(src, src)


if __name__ == "__main__":
    unittest.main()
