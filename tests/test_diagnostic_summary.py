import copy
import json
import unittest

from agent_system.diagnostic_summary import summarize


def section(heading, *records):
    return {'heading': heading, 'items': [json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in records]}


class DiagnosticSummaryTests(unittest.TestCase):
    def base(self, scenario):
        return {'scenario': scenario, 'task_id': 't1', 'candidate_id': 'c1',
                'candidates': [{'candidate_id': 'c1'}, {'candidate_id': 'c2'}], 'sections': [], 'notices': []}

    def test_understanding_preserves_separate_historical_interpretations(self):
        result = self.base('understanding')
        result['sections'] = [
            section('主旨契约', {'topic': '甲主题', 'core_promise': '生成读法', 'evidence_ids': ['e1', 'absent'], 'pending_questions': ['待核']}),
            section('评审主旨契约', {'core_promise': '复核读法', 'evidence_ids': ['e2']}),
            section('理解断言', {'claim_id': 'cl1', 'text': '模型说法', 'evidence_refs': ['e1'], 'verification': None}),
            section('证据原文、定位与质量', {'evidence_id': 'e1', 'text': '素材', 'locator': '00:02', 'quality': 'raw'}, {'evidence_id': 'e2', 'text': '另一素材'})]
        before = copy.deepcopy(result)
        summary = summarize(result)
        self.assertEqual(before, result)
        text = '\n'.join(summary['paragraphs'])
        for value in ('生成侧历史模型主旨', '生成读法', '评审侧历史模型主旨', '复核读法', '待核', '未记录', '不是视频事实结论', '不等于事实确证'):
            self.assertIn(value, text)
        self.assertEqual(summary['evidence'][0]['locator'], '00:02')
        self.assertFalse(summary['evidence'][1]['matched'])
        self.assertEqual(summary['evidence'][1]['evidence_id'], 'absent')

    def test_review_candidate_and_task_route_are_not_conflated(self):
        result = self.base('review')
        result['sections'] = [section('候选级评审', {'candidate_id': 'c1', 'gates': {'G3': {'status': 'needs_review', 'reason': '覆盖待核', 'evidence_ids': ['e1']}}, 'issues': [{'issue_code': 'X', 'reason': '限定缺失', 'evidence_ids': ['e1']}]}),
                              section('候选级决策', {'candidate_id': 'c1', 'status': 'needs_review', 'reason': '人工确认'}),
                              section('评审关联证据', {'evidence_id': 'e1', 'locator': '00:03', 'text': '原文'}),
                              section('任务级路由（不等于候选级评审）', {'action': 'handoff', 'recommended_candidate_id': 'c2', 'reason': '排序'})]
        summary = summarize(result)
        text = '\n'.join(summary['paragraphs'])
        for value in ('候选级评审', '覆盖待核', '限定缺失', '候选级决策', '人工确认', '任务级路由', 'c2', '排序', '不等于当前候选'):
            self.assertIn(value, text)
        self.assertEqual([e['reason'] for e in summary['evidence']], ['G3：覆盖待核', '限定缺失'])

    def test_evidence_only_cites_actual_candidate_or_claim_refs(self):
        result = self.base('evidence')
        result['sections'] = [section('候选引用', {'candidate_id': 'c1', 'evidence_ids': ['e1', 'lost']}),
                              section('候选关联断言（模型抽取，非事实裁决）', {'claim_id': 'cl1', 'text': '断言', 'evidence_refs': ['e1'], 'verification': {'verdict': 'supported'}}),
                              section('候选关联证据原文、定位与质量', {'evidence_id': 'e1', 'text': '材料', 'locator': '12秒'}),
                              section('全部素材证据（不一定被候选引用）', {'evidence_id': 'e2', 'text': '无关材料'}),
                              section('问题字面检索', '未在检索范围内找到完全匹配。')]
        summary = summarize(result)
        self.assertEqual([x['evidence_id'] for x in summary['evidence']], ['e1', 'lost', 'e1'])
        self.assertFalse(summary['evidence'][1]['matched'])
        text = '\n'.join(summary['paragraphs'])
        self.assertIn('历史核查结果：supported', text)
        self.assertIn('完整字面检索', text)
        self.assertIn('不进行自然语言理解', text)
        self.assertNotIn('已证实', text)

    def test_missing_and_selection(self):
        for scenario in ('review', 'evidence'):
            result = self.base(scenario)
            result['candidate_id'] = None
            result['notices'] = ['question 尚未检索']
            result['sections'] = [section('候选清单', {'candidate_id': 'c1'}, {'candidate_id': 'c2'})]
            summary = summarize(result)
            self.assertIn('请指定 candidate_id', '\n'.join(summary['paragraphs']))
            self.assertIn('question 尚未检索', summary['notices'])
            self.assertEqual(summary['evidence'], [])
        result = self.base('understanding')
        result['sections'] = [section('主旨契约', '未记录主旨契约。')]
        text = '\n'.join(summarize(result)['paragraphs'])
        self.assertIn('生成侧历史模型主旨未记录', text)
        self.assertIn('评审侧历史模型主旨未记录', text)
        self.assertIn('核查状态未知', text)

    def test_question_match_and_invalid_scenario(self):
        result = self.base('review')
        result['sections'] = [section('问题字面检索', '证据: {"text": "示例"}')]
        self.assertIn('返回 1 条', '\n'.join(summarize(result)['paragraphs']))
        with self.assertRaises(ValueError):
            summarize({'scenario': 'other'})


if __name__ == '__main__':
    unittest.main()
