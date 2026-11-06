from agent_system.chat_tools import ToolContext, get_task, get_review, get_evidence, check_evidence_coverage


def test_multiple_batches_require_explicit_identity():
    ctx = ToolContext(db_aliases={'one': 'a', 'two': 'b'}, default_alias='one')
    assert not get_task({'task_id': 'same'}, ctx)['ok']
    assert not get_review({'task_id': 'same', 'candidate_id': 'c'}, ctx)['ok']
    assert not get_evidence({'task_id': 'same'}, ctx)['ok']
    assert not check_evidence_coverage({'task_id': 'same', 'candidate_id': 'c'}, ctx)['ok']
