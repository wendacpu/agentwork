"""指标口径不混入历史候选，也不把缺评当成达标。"""
from scripts.metrics_vs_target import Video, measure, render, title_hash


def candidate(cid, title, round_no=0):
    return {'candidate_id': cid, 'headline': title, 'title_sha256': title_hash(title), 'round': round_no}


def decision(task, cid, title, value):
    return (task, cid, title_hash(title)), {'decision': value, 'original_headline': title}


def test_unreleased_approved_does_not_count_as_supply():
    v = Video(task_id='t', blind_id='R1', candidates=[candidate('c1', '标题')], route_delivered=set())
    m = measure([v], dict([decision('t', 'c1', '标题', 'approved')]))
    assert m['M5'][0] == 0
    assert m['M7'][:2] == (1, 1)


def test_unjudged_candidates_do_not_generate_target_pass():
    v = Video(task_id='t', blind_id='R1', candidates=[candidate('c1', '标题')])
    report = render(measure([v], {}))
    assert '人工评审未完成' in report
    assert '✅' not in report


def test_zero_denominator_is_not_pass():
    v = Video(task_id='t', blind_id='R1')
    report = render(measure([v], {}))
    assert '无有效分母' in report


def test_partial_first_round_not_counted_as_rescue_failure():
    v = Video(task_id='t', blind_id='R1', candidates=[candidate('c1', '标题一'), candidate('c2', '标题二')])
    m = measure([v], dict([decision('t', 'c1', '标题一', 'rejected')]))
    assert m['M4'][:2] == (0, 0)
