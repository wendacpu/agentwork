"""独立风险信号：只记录可定位线索，不把命中词等同于事实。"""
from __future__ import annotations

import re
from typing import Any

EXTRACTOR_VERSION = "risk-signals-v1"
_PATTERNS = {
    "ai_generated": r"AI\s*(?:生成|合成|创作|制作)|AIGC|人工智能生成",
    "fictional": r"虚构|剧情演绎|情景剧|短剧|纯属虚构",
    "simulation": r"模拟实验|模拟事故|游戏画面|影视片段|虚拟主播",
    "number": r"\d+(?:\.\d+)?\s*(?:万元|亿元|元|块钱|块|公里|千米|米|公斤|千克|斤|岁|年|月|日|人|个|碗|%|％)?|[零一二两三四五六七八九十百千万亿]+(?:元|人|碗|岁|公里)",
    "qualifier": r"不是所有|并非|不能|没有|未能|不代表|可能|疑似|部分|仅限|如果|只有|据称|未经证实|尚未|不适用|不建议|不得|未发现",
    "relative_time": r"刚刚|今日|今天|近日|最新|目前|现在",
}


def extract_risk_signals(video: Any) -> dict[str, Any]:
    """扫描原始证据并保留偏移；衍生描述始终只作待核验线索。"""
    signals: list[dict[str, Any]] = []
    for evidence in video.evidence:
        if evidence.field == "frame":
            continue  # 路径不是看过像素的证据
        text = str(evidence.text or "")
        for kind, pattern in _PATTERNS.items():
            for match in re.finditer(pattern, text, re.IGNORECASE):
                left = max(text.rfind(c, 0, match.start()) for c in "。！？!?；;\n") + 1
                ends = [text.find(c, match.end()) for c in "。！？!?；;\n"]
                right = min((n for n in ends if n >= 0), default=len(text))
                clause = text[left:right]
                before = text[max(left, match.start() - 8):match.start()]
                negated = bool(re.search(r"(?:不是|并非|非|未使用|没有使用|不是由|并非由)\s*$", before))
                discussion = bool(re.search(r"识别|辨别|检测|教程|如何|什么是|讨论|据说|有人说|他称", clause))
                uncertain = bool(re.search(r"疑似|可能|或许|似乎", clause))
                self_declared = bool(re.search(r"本视频|本作品|本片|此视频|本内容|该视频", clause))
                derived = evidence.quality in {"model_derived_uncalibrated", "unverified_source_metadata"}
                tag = text.strip().strip("#【】[] ").lower() == match.group(0).lower()
                confirmed = kind in {"ai_generated", "fictional"} and not (negated or discussion or uncertain or derived) and (self_declared or tag)
                signals.append({
                    "signal_id": f"signal-{len(signals) + 1:04d}", "signal_type": kind,
                    "value": match.group(0), "source_field": evidence.field,
                    "evidence_id": evidence.evidence_id, "span": [match.start(), match.end()],
                    "quality": evidence.quality, "polarity": "negative" if negated else "positive",
                    "target": "discussion" if discussion else "current_video" if self_declared or tag else "unknown",
                    "status": "confirmed_declaration" if confirmed else "observation" if kind in {"number", "qualifier", "relative_time"} else "needs_verification",
                    "context": clause, "extractor_version": EXTRACTOR_VERSION,
                })
    labels = []
    for kind, label in (("fictional", "剧情演绎"), ("ai_generated", "AI生成")):
        if any(s["signal_type"] == kind and s["status"] == "confirmed_declaration" for s in signals):
            labels.append(label)
    pending = [s["signal_id"] for s in signals if s["signal_type"] in {"ai_generated", "fictional", "simulation"} and s["status"] == "needs_verification" and s["polarity"] != "negative" and s["target"] != "discussion"]
    return {"extractor_version": EXTRACTOR_VERSION, "signals": signals, "required_prefixes": labels, "pending_signal_ids": pending}


def check_signal_constraints(headline: str, report: dict[str, Any], claims: list[Any], cited_claim_ids: list[str]) -> list[dict[str, Any]]:
    """不让无依据的时效措辞进入成品；限定语义保留交给显式待核验。"""
    errors: list[dict[str, Any]] = []
    cited = [c for c in claims if c.claim_id in cited_claim_ids]
    supported_text = " ".join(c.text for c in cited)
    for token in re.findall(r"刚刚|今日|今天|近日|最新|目前|现在", headline):
        if token not in supported_text:
            errors.append({"code": "G1_UNSUPPORTED_RECENCY", "repairable": True, "reason": f"无引用陈述支持时效表达「{token}」；发布时间不等于事件时间"})
    return errors


def signal_review_reasons(headline: str, report: dict[str, Any], evidence_ids: set[str]) -> list[dict[str, Any]]:
    """语义无法确定时明确待审，不把关键词缺失直接判为事实错误。"""
    reasons = []
    groups = {
        "negation": r"并非|不能|没有|未能|不代表|尚未|不是所有|不适用|未|不",
        "uncertainty": r"可能|疑似|或许|尚待|不确定|据称|未经证实",
        "scope": r"部分|仅限|只有|如果|一些|少数|特定|并非所有|不是所有",
    }
    for signal in report.get("signals", []):
        if signal["evidence_id"] not in evidence_ids or signal["signal_type"] != "qualifier":
            continue
        value = signal["value"]
        category = "uncertainty" if re.search(r"可能|疑似|据称|未经证实", value) else "scope" if re.search(r"部分|仅限|如果|只有|不是所有", value) else "negation"
        if not re.search(groups[category], headline):
            reasons.append({"code": "QUALIFIER_RETENTION_UNVERIFIED", "signal_id": signal["signal_id"], "evidence_id": signal["evidence_id"], "reason": f"引用材料含必要限定「{value}」，成品未见同类限定，需核对是否改变语义"})
    if re.search(_PATTERNS["relative_time"], headline):
        reasons.append({"code": "RELATIVE_TIME_UNVERIFIED", "reason": "相对时间缺独立事件日期锚定；即使引用材料有同词也不能证明当前有效"})
    return reasons


# 数字事实独立于风险词命中；不推断原题真值，也不生成“数值冲突”结论。
def _numeric_decimal(raw: str) -> str | None:
    """将常见中文数词及阿拉伯数词转为无多余零的十进制字符串。"""
    from decimal import Decimal, InvalidOperation

    raw = (raw.replace(",", "").replace("，", "").replace("两", "二").replace("〇", "零")
           .replace("萬", "万").replace("億", "亿"))
    digits = dict(zip("零一二三四五六七八九", "0123456789"))
    if not raw or (not any(c in digits or c.isdigit() for c in raw) and not any(c in "十百千" for c in raw)):
        return None
    if any(c in raw for c in "十百千万亿") and re.search(r"[一二三四五六七八九]{2}", raw):
        return None
    try:
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[万亿])?", raw):
            scale = {"万": Decimal(10000), "亿": Decimal(100000000)}
            value = Decimal(raw[:-1]) * scale[raw[-1]] if raw[-1] in scale else Decimal(raw)
        elif "点" in raw:
            integer, fraction = raw.split("点", 1)
            base = _numeric_decimal(integer)
            if base is None or not fraction or any(c not in digits for c in fraction):
                return None
            value = Decimal(base + "." + "".join(digits[c] for c in fraction))
        elif all(c in digits for c in raw):
            value = Decimal("".join(digits[c] for c in raw))
        else:
            total, section, number = 0, 0, 0
            for char in raw:
                if char in digits:
                    number = int(digits[char])
                elif char in "十百千":
                    section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                    number = 0
                elif char in "万亿":
                    section += number
                    number = 0
                    if char == "万":
                        total += section * 10000
                    else:
                        total = (total + section) * 100000000
                    section = 0
                else:
                    return None
            value = Decimal(total + section + number)
        return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")
    except (InvalidOperation, ValueError):
        return None


def numeric_facts(text: str) -> list[dict]:
    """抽取可定位数字；span 是原文半开偏移，metric 未知时不猜测实体。"""
    number = (
        r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?[万萬亿億]?"
        r"|[零〇一二两三四五六七八九十百千万億亿]+(?:点[零〇一二两三四五六七八九]+)?"
    )
    pattern = (
        rf"(?<![\d.])(?:(?:大约|约|将近|接近|近|超过|至少|不足|不到)(?:{number})"
        rf"|[数几][十百千万亿]*|(?:{number})多[十百千万亿]*|{number})"
    )
    # 货币单位必须单列：漏了它们，「400亿美元」会被抽成 value=4e10、unit=''，
    # 于是标题写成「400亿」（金额口径不明）也与证据「单位一致」，数字核验整条失效。
    # 量词只收「确实在数东西」的那些：ASR 常见繁体（塊錢/個/歲），教程类常见「招/式/套」。
    # 漏收的后果不是保守，是**把有证据的数字判成没证据**——V 集实测「九招」漏成 unit=''、
    # 与标题「九個動作」的 unit='个' 不匹配，标题明明抄自视频原题却被判数字冲突。
    unit_pattern = r"\s*(人民币|美元|美金|港元|港币|欧元|日元|英镑|韩元|卢布|新台币|块钱|塊錢|元|圆|圓|块|塊|公里|千米|公斤|千克|分鐘|分钟|小時|小时|個月|个月|歲|岁|年|月|日|天|人|位|名|個|个|碗|招|式|套|款|條|条|張|张|道|份|件|臺|台|瓶|盒|組|组|次|遍|步|顆|颗|米|斤|%|％)"
    facts = []
    for match in re.finditer(pattern, text):
        start, end = match.span()
        # 序数也可能是需要证据的排名或先后顺序（如「全球第1例」），不能整类跳过。
        # 仅排除明确的口感修辞，不泛化豁免“一口一个饺子”等真实数量表达。
        if match.group() == "一" and re.search(r"一口$", text[:start]) and re.match(r"个脆(?:[，。！!]|$)", text[end:]):
            continue
        raw_number = match.group()
        ambiguous = bool(re.search(r"[数几多]|^(?:大约|约|将近|接近|近|超过|至少|不足|不到)", raw_number))
        value = None if ambiguous else _numeric_decimal(raw_number)
        unit_match = re.match(unit_pattern, text[end:])
        original_unit = unit_match.group(1) if unit_match else ""
        if unit_match and re.match(r"\s*(?:左右|上下|以上|以下|多)", text[end + unit_match.end():]):
            value = None
        if value is None:
            if not unit_match:
                continue
            end += unit_match.end()
            facts.append({"value": None, "unit": original_unit, "metric": "unknown", "role": "unknown", "span": [start, end]})
            continue
        # 无量词的中文片段容易是“一共”“万一”等普通词，宁可漏报。
        if not unit_match and not re.search(r"\d", match.group()) and not re.search(r"(?:编号|数字|数值)\s*$", text[max(0, start - 8):start]):
            continue
        if unit_match:
            end += unit_match.end()
        unit = {"分鐘": "分钟", "小時": "小时", "個月": "个月", "歲": "岁", "個": "个", "人民币": "元", "块钱": "元", "块": "元",
                "塊錢": "元", "塊": "元", "圓": "元", "美金": "美元", "港币": "港元", "位": "人", "名": "人",
                "千米": "公里", "千克": "公斤", "％": "%", "條": "条", "張": "张", "臺": "台", "組": "组", "顆": "颗"}.get(original_unit, original_unit)
        # 以分句取指标上下文，避免把邻近的年龄、总价或人数串成同一指标。
        left = max((text.rfind(c, 0, start) for c in "，,。！？!?；;\n"), default=-1) + 1
        stops = [text.find(c, end) for c in "，,。！？!?；;\n"]
        right = min((p for p in stops if p >= 0), default=len(text))
        before, after = text[left:start], text[end:right]
        if unit == "元":
            # 就近的价格标签优先；不能让同句总价标签污染后面的单价。
            labels = list(re.finditer(r"单价|每(?:人|位|名|个|碗|份|斤|公斤|件)|人均|总价|总计|合计|一共|共计|总共", before))
            label = labels[-1].group() if labels else ""
            per_suffix = re.match(r"\s*(?:[/／]\s*(?:人|位|个|碗|份|斤|公斤|件)|一(?:人|位|个|碗|份|斤|件))", after)
            metric = "unit_price" if per_suffix or label.startswith(("单价", "每", "人均")) else "total_price" if label else "amount"
        elif unit in {"美元", "港元", "欧元", "日元", "英镑", "韩元", "卢布", "新台币"}:
            metric = "amount"
        elif unit == "人":
            metric = "people"
        elif unit == "岁":
            metric = "age"
        elif unit == "年":
            metric = "year" if (len(value) == 4 and value.isdigit()) or re.search(r"年份|公元", before) else "duration"
        elif unit in {"月", "日", "天", "小时", "分钟", "个月"}:
            metric = "duration"
        elif unit in {"个", "碗", "招", "式", "套", "款", "条", "张", "道", "份", "件", "台", "瓶", "盒", "组", "次", "遍", "步", "颗"}:
            metric = "count"
        elif unit in {"公里", "米"}:
            metric = "distance"
        elif unit in {"公斤", "斤"}:
            metric = "weight"
        elif unit == "%":
            metric = "percentage"
        else:
            metric = "unknown"
        # 问号作用于当前句，而不是整篇；逗号后仍可能是同一疑问句。
        sentence_left = max((text.rfind(c, 0, start) for c in "。！？!?；;\n"), default=-1) + 1
        sentence_stops = [text.find(c, end) for c in "。！？!?；;\n"]
        sentence_right = min((p for p in sentence_stops if p >= 0), default=len(text))
        sentence = text[sentence_left:sentence_right]
        question = (sentence_right < len(text) and text[sentence_right] in "？?") or bool(re.search(r"是否|难道|究竟|到底|为何|为什么|怎么|多少|几[个人岁年]|吗", sentence))
        facts.append({"value": value, "unit": unit, "metric": metric, "role": "question" if question else "assertion", "span": [start, end]})
    return facts


_COUNT_UNITS = frozenset({"个", "个", "碗", "招", "式", "套", "款", "条", "张", "道", "份", "件",
                          "台", "瓶", "盒", "组", "次", "遍", "步", "颗"})


def _unit_equivalent(left: str, right: str) -> bool:
    """同一指标下，量词差别不算口径差别。

    「九招」和「九个动作」数的是同一件事，只换了量词；旧实现要求量词字面相等，
    于是把抄自原视频标题的数字判成「没有证据支持」。计数量词之间互通，
    金额/时长/重量/距离/百分比仍按单位原样比——「400亿美元」写成「400亿」
    是真丢了口径，必须继续拦。
    """
    if left == right:
        return True
    return left in _COUNT_UNITS and right in _COUNT_UNITS


def unsupported_numeric_facts(headline: str, evidence_texts: list[str]) -> list[dict]:
    """返回证据未支持的标题数字，不将不同数值直接判为事实冲突。

    证据中的问句不是断言。未知指标仅与未知指标匹配；金额 amount
    不自动冒充已知总价或单价，不计算、补全或假定原题就是事实。
    量词等价只覆盖计数量词（个/招/式/套…），不跨指标、不跨金额口径。

    模糊量词（value=None：约/数/几/将近…）只在证据里出现**同样模糊的同单位词**
    时算支持。真实误伤（V 集 2026-11-04）：「每天持续数小时」在 claim 和证据里
    是同一个词，但 value=None 永不匹配（且证据侧 role=unknown 被 assertion 过滤
    掉），claim 被「数字证据守卫」整条 excluded，下游候选引用即
    G4_INVALID_REFERENCE，三个候选白跑重写。
    存在量词（一 + 人/物量词：一名幼童、一只猫）同理——它是「有个」不是
    「数了几个」，不构成数量主张。
    """
    if isinstance(evidence_texts, str):
        evidence_texts = [evidence_texts]
    all_facts = [fact for text in evidence_texts for fact in numeric_facts(text)]
    evidence = [fact for fact in all_facts if fact["role"] == "assertion"]
    return [fact for fact in numeric_facts(headline)
            if not _is_existential_quantifier(headline, fact)
            and _numeric_fact_unsupported(fact, all_facts, evidence)]


_EXISTENTIAL_UNITS = frozenset({
    "人", "个", "只", "条", "家", "头", "辆", "间", "场", "次", "顿", "句", "段",
    "碗", "招", "式", "套", "款", "张", "道", "份", "件", "台", "瓶", "盒", "组", "步", "颗",
})


def _is_existential_quantifier(text: str, fact: dict) -> bool:
    """「一名幼童」「一只猫」是存在描述不是数量主张。

    判据：值恰为 1、量词是人/物量词（经规范化：名/位→人）、且前面没有
    「每/共/各/约/仅」等计数语境词（「共四名」「每一名」仍是数量主张，
    但那些值通常 ≥2，语境词是双保险）。金额/时长/距离等计量单位
    （元/小时/公里）不在此列——「一万元」永远是金额事实。
    """
    if fact.get("value") != "1" or fact.get("unit") not in _EXISTENTIAL_UNITS:
        return False
    start = fact.get("span", [0, 0])[0]
    before = text[max(0, start - 2):start]
    return not re.search(r"[每共各约仅足]$", before)


def _numeric_fact_unsupported(fact: dict, all_facts: list[dict], assertions: list[dict]) -> bool:
    if fact.get("value") is None:
        # 模糊量词（数/约/几…）：只被同样模糊且同单位的证据支持（用全量集——
        # 模糊量词在证据里同样记 role=unknown，assertion 过滤会把它们全部丢掉，
        # 「数小时」对「数小时」就成了无支持）。明确数值仍只认断言集：
        # 「总价872元」不能被问句「总价872元吗？」支持。
        # 「数亿年」对「3亿年」→ 不支持（模糊量级不能被明确数值隐式顶替，
        # 见 test_approximate_numbers）。
        return not any(
            candidate.get("value") is None
            and _unit_equivalent(fact["unit"], candidate["unit"])
            for candidate in all_facts
        )
    return not any(
        fact["value"] == candidate["value"]
        and _unit_equivalent(fact["unit"], candidate["unit"])
        and fact["metric"] == candidate["metric"]
        for candidate in assertions
    )
