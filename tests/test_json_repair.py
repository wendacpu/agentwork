"""模型输出的 JSON 修复：内嵌未转义引号是中文材料里最常见的一种坏法。

真实故障（2026-11-01 真实冒烟）：评审节点连续 3 次 JSON 非法，报错位置都在
「Expecting ',' delimiter」，但真正的坏点在几十字符之前——模型把标题/理由里的
引号直接写进了字符串。
"""
from __future__ import annotations

from agent_system.model import _escape_stray_quotes, loads_json_loose


def test_stray_quote_inside_value_is_escaped():
    broken = '{"a":"他说"你好"然后走了","b":1}'
    assert loads_json_loose(broken) == {"a": '他说"你好"然后走了', "b": 1}


def test_stray_quote_at_value_end_is_not_double_escaped():
    broken = '{"t":"他说"好"}'
    assert loads_json_loose(broken) == {"t": '他说"好'}


def test_already_escaped_quotes_are_left_alone():
    broken = '{"a":"他说\\"你好\\"","b":2}'
    assert loads_json_loose(broken) == {"a": '他说"你好"', "b": 2}


def test_valid_json_is_untouched():
    assert loads_json_loose('{"a": "b"}') == {"a": "b"}
    assert _escape_stray_quotes('{"a":"b"}') == '{"a":"b"}'


def test_trailing_comma_and_stray_quote_together():
    broken = '{"a":"含"引号"的值","b":[1,2,],}'
    assert loads_json_loose(broken) == {"a": '含"引号"的值', "b": [1, 2]}
