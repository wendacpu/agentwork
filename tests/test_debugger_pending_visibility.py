import copy
import json
import subprocess
import unittest

from agent_system.debugger import PAGE, _display_candidate_ids


class DebuggerPendingVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "candidates": [
                {"candidate_id": "a", "headline": "推荐标题"},
                {"candidate_id": "b", "headline": "待核标题"},
                {"candidate_id": "c", "headline": "重写标题"},
                {"candidate_id": "d", "headline": "拒绝标题"},
            ],
            "candidate_decisions": [
                {"candidate_id": "a", "status": "ready_for_human_review", "reason": "PASS"},
                {"candidate_id": "b", "status": "needs_review", "reason": "事实待核 <核实>"},
                {"candidate_id": "c", "status": "rewrite", "reason": "需要重写"},
                {"candidate_id": "d", "status": "reject", "reason": "拒绝"},
            ],
            "route": {"action": "handoff", "eligible_candidate_ids": ["a"],
                      "pending_review_candidate_ids": ["b", "c"],
                      "recommended_candidate_id": "a"},
        }

    def test_union_order_and_no_route_mutation(self):
        original = copy.deepcopy(self.state)
        self.assertEqual(_display_candidate_ids(self.state), ["a", "b", "c"])
        self.assertEqual(self.state, original)
        self.state["route"] = {"action": "needs_review", "candidate_ids": ["b"]}
        self.assertEqual(_display_candidate_ids(self.state), ["b"])
        self.state["route"] = {}
        self.assertEqual(_display_candidate_ids(self.state), ["a", "b", "c", "d"])

    def test_render_status_reason_and_recommendation(self):
        script = PAGE.split('<script>', 1)[1].split('</script>', 1)[0]
        script = script.replace('init();poll=setInterval(refresh,3500);', '')
        task = {"state": self.state, "display_candidate_ids": _display_candidate_ids(self.state),
                "events": [], "video_id": "video", "status": "pending_human"}
        js = """
const elements = {};
global.document = {getElementById: id => elements[id] ||= {innerHTML:'', classList:{toggle(){}}}, querySelectorAll:()=>[]};
""" + script + "\nrender(" + json.dumps(task, ensure_ascii=False) + "); console.log(elements.content.innerHTML);"
        result = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
        html = result.stdout
        self.assertIn("系统状态：needs_review · 原因：事实待核 &lt;核实&gt; · 非自动推荐／未标记为可交付", html)
        self.assertIn("系统状态：rewrite · 原因：需要重写 · 非自动推荐／未标记为可交付", html)
        self.assertIn("系统状态：ready_for_human_review · 原因：PASS · 自动推荐", html)
        self.assertNotIn("拒绝标题", html)


if __name__ == '__main__':
    unittest.main()
