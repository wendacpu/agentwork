"""仅供接线测试使用的固定材料：对照本用例的原始标题，不推断额外事实。"""
from agent_system.model import MockAdapter


class EvidenceReviewAdapter(MockAdapter):
    """模拟该视频的生成与评审；不适用于其他视频或不受支持的标题。"""
    mismatch_first = False

    def complete(self, **kwargs):
        reply = super().complete(**kwargs)
        task, user = kwargs['task'], kwargs['user']
        if task in ('generate', 'rewrite'):
            if task == 'generate':
                generation_count = getattr(self, '_generation_count', 0)
                self._generation_count = generation_count + 1
                titles = (['通报称涉79名学生', '官方通报涉及79名学生', '涉79名学生的官方通报']
                          if self.mismatch_first and generation_count == 0 else
                          ['通报称涉及78名学生', '官方通报涉及78名学生', '涉78名学生的官方通报'])
            else:
                parent = str(user.get('parent_candidate_id') or '')[-1:]
                titles = {'1': ['官方通报共涉及78名学生'],
                          '2': ['通报称共涉及78名学生'],
                          '3': ['共涉及78名学生的通报']}.get(parent, ['官方通报共涉及78名学生'])
            for item, title in zip(reply.data['candidates'], titles):
                item['headline'] = title
        elif task == 'review':
            evidence = next(e for e in user['evidence'] if e['evidence_id'] == 'e-title')
            excerpt = '共涉及78名学生'
            assert excerpt in evidence['text']
            candidates = {c['candidate_id']: c for c in user['candidate_titles']}
            for review in reply.data['reviews']:
                title = candidates[review['candidate_id']]['title']
                if '79名学生' in title:
                    span, alignment = '79名学生', 'mismatched'
                    explanation = '标题称79人，原题通报称涉及78人，人数不一致。'
                elif '78名学生' in title:
                    span, alignment = '78名学生', 'matched'
                    explanation = '标题与原题都称涉及78名学生；不推断检测或确诊结果。'
                else:
                    raise AssertionError(f'未为此标题定义证据对照: {title}')
                gate = review['gates']['G3']
                gate['evidence_ids'] = [evidence['evidence_id']]
                gate['promise_comparison'] = {
                    'title_promise': title, 'trigger_span': span,
                    'video_delivery': '原题称共涉及78名学生', 'evidence_excerpt': excerpt,
                    'evidence_answered_question': '通报称涉及多少名学生',
                    'alignment': alignment, 'explanation': explanation,
                }
                if alignment == 'mismatched':
                    gate.update(status='revise', delivery_supported=False, reason=explanation)
        return reply
