"""选择题的作答位一律是全角括号（　）。

题库规则（"只要是单选题，都默认是括号"）要从导入到印刷全程成立：

* 原卷印成下划线、Word 下划线、``\\fillin``、``\\paren``、半角空括号的
  作答位，入库后都收敛成一个 ``（　）``；
* 题干中间的真填空（如"在（　）里填上运算符号"）不会被误改成第二个作答位；
* 句末标点保留在括号之后；
* 重复保存结果不变（幂等），手工保存不会凭空补括号。
"""

from __future__ import annotations

import re

PAREN = "（\u3000）"

_CHOICES_RE = re.compile(r"\\begin\s*\{choices\}")

# 只匹配"空的"作答括号：括号内只能出现空格/全角空格，有内容的括号不动。
_BLANK_PAREN_RE = re.compile(r"[(（][ \t\u3000]*[)）]")
_FILLIN_RE = re.compile(r"\\fillin\b\*?")
_PAREN_MACRO_RE = re.compile(r"\\paren\b")
_UNDERSCORE_BLANK_RE = re.compile(r"_{3,}")

# 需要原样保留的片段：行内公式、图片/链接、行内代码。
_PROTECTED_RE = re.compile(
    r"\$\$?[^$\n]*\$\$?|\\\([\s\S]*?\\\)|"
    r"!?\[[^\]\n]*\]\([^\n)]*\)|`[^`\n]*`"
)

_ADJACENT_PARENS_RE = re.compile(re.escape(PAREN) + r"[ \t\u3000]*" + re.escape(PAREN))
_SPACE_BEFORE_PAREN_RE = re.compile(r"[ \t]+" + re.escape(PAREN))

_TRAILING_PUNCT_RE = re.compile(r"([。，,；;.!?！？：:])\s*$")
_IMAGE_ONLY_LINE_RE = re.compile(r"^\s*(?:!\[[^\]]*\]\([^\n)]*\)\s*)+$")


def _choices_boundary(text: str) -> int:
    match = _CHOICES_RE.search(text)
    return match.start() if match else len(text)


def _replace_slots(segment: str) -> str:
    segment = _FILLIN_RE.sub(PAREN, segment)
    segment = _PAREN_MACRO_RE.sub(PAREN, segment)
    segment = _UNDERSCORE_BLANK_RE.sub(PAREN, segment)
    segment = _BLANK_PAREN_RE.sub(PAREN, segment)
    return segment


def _convert_stem(stem: str) -> str:
    """把题干里的各种作答位写法收敛成（　），公式/图片/代码原样保留。"""
    if not stem:
        return stem
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_RE.finditer(stem):
        parts.append(_replace_slots(stem[cursor:match.start()]))
        parts.append(match.group())
        cursor = match.end()
    parts.append(_replace_slots(stem[cursor:]))
    converted = "".join(parts)
    while True:
        collapsed = _ADJACENT_PARENS_RE.sub(PAREN, converted)
        collapsed = _SPACE_BEFORE_PAREN_RE.sub(PAREN, collapsed)
        if collapsed == converted:
            return converted
        converted = collapsed


def _insert_missing_slot(stem: str) -> str:
    """题干没有任何作答位时，在最后一个散文行的句末标点前补一个（　）。

    结尾的独立图片行不算散文，括号必须补在图片之前，否则会被挤到下一行。
    """
    lines = stem.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        body, ending = (line, "") if not line.endswith("\n") else (
            line[:-1].rstrip("\r"), line[-1])
        if not body.strip() or _IMAGE_ONLY_LINE_RE.fullmatch(body):
            continue
        punct = _TRAILING_PUNCT_RE.search(body)
        insert_at = punct.start() if punct else len(body)
        lines[index] = body[:insert_at] + PAREN + body[insert_at:] + ending
        return "".join(lines)
    # 全是图片/空行：补在整个题干末尾。
    return stem + PAREN


def convert_choice_answer_slots(text: str, add_missing: bool = True) -> str:
    """把 text 题干部分的作答位收敛成（　）；选项区（choices 环境）不动。

    add_missing=False 时只收敛已有作答位，不凭空补括号（手工编辑场景）。
    """
    if not text or not isinstance(text, str):
        return text or ""
    boundary = _choices_boundary(text)
    stem, rest = text[:boundary], text[boundary:]
    stem = _convert_stem(stem)
    if add_missing and PAREN not in stem:
        stem = _insert_missing_slot(stem)
    return stem + rest


def has_choice_answer_slot(text: str) -> bool:
    """题干里是否已经存在（　）形式的作答位。"""
    if not text:
        return False
    return PAREN in text[:_choices_boundary(text)]
