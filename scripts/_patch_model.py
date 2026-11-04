# -*- coding: utf-8 -*-
import io
p = "/Users/wanting/program/复杂 agent/agent_system/model.py"
doc = io.open(p, encoding="utf-8").read()

# 1) 加截断修复函数（放在 loads_json_loose 之前）
anchor = 'def loads_json_loose(text: str) -> Any:'
helper = '''def _close_truncated(body: str) -> str:
    """尽力修复被 max_tokens 截断的 JSON：补齐未闭合的字符串/对象/数组。
    只做结构补齐，不改内容；修不好仍由上层重试。"""
    out = body.rstrip()
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in out:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
    if in_str:
        out += '"'
    out = re.sub(r"[,:\\\\s]+$", "", out)
    out += "".join("}" if c == "{" else "]" for c in reversed(stack))
    return out


''' + anchor
if anchor in doc and "_close_truncated" not in doc:
    doc = doc.replace(anchor, helper, 1); print("helper 已加")
else:
    print("helper MISS")

# 2) 在 raise 前加截断修复尝试
old = '''    repaired = re.sub(r",(\\s*[}\\]])", r"\\1", body)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as exc:'''
new = '''    repaired = re.sub(r",(\\s*[}\\]])", r"\\1", body)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_close_truncated(repaired))
    except json.JSONDecodeError as exc:'''
if old in doc:
    doc = doc.replace(old, new, 1); print("截断修复已接入")
else:
    print("截断修复 MISS")

# 3) payload 加 max_tokens
old2 = 'payload = {"model": self.model, "temperature": 0.2,'
new2 = 'payload = {"model": self.model, "temperature": 0.2, "max_tokens": 8192,'
if old2 in doc and '"max_tokens"' not in doc:
    doc = doc.replace(old2, new2, 1); print("max_tokens 已加")
else:
    print("max_tokens MISS")

io.open(p, "w", encoding="utf-8").write(doc)
print("written")
