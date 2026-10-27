"""选择题的作答位一律是括号（　）。

老师的原话是"只要是单选题，都默认是括号"。这条规则要从导入到印刷全程成立：

* 原卷印成下划线、Word 下划线、``\\fillin``、半角括号、``\\paren``，
  入库后都收敛成一个 ``（　）``；
* 题干中间的真填空（如"在（　）里填上运算符号"）不能被误改；
* 句末标点保留在括号之后；
* 重复保存结果不变（幂等），手工保存不会凭空补括号。
"""

import pytest


def _normalize(text, keep=True, add_missing=True):
    from main import normalize_fillin_macro
    return normalize_fillin_macro(
        text, keep_choice_parens=keep, add_choice_slot_if_missing=add_missing,
    )


@pytest.mark.parametrize("raw, expected", [
    # 原卷印成下划线
    ("下面哪幅图不能用“4×3”表示？___", "下面哪幅图不能用“4×3”表示？（\u3000）"),
    # 原卷印成 Word 下划线，被解析成 \fillin
    (
        "分针指向8，是\\fillin。\n\\begin{choices}\n\\item 2时40分\n\\end{choices}",
        "分针指向8，是（\u3000）。\n\\begin{choices}\n\\item 2时40分\n\\end{choices}",
    ),
    # 题干以图片结尾：括号必须在图片之前，否则被挤到下一行像"没括号"
    (
        "下面是小明早上起床的时间，正确的是\\fillin。\n![](/static/uploads/a.jpg)",
        "下面是小明早上起床的时间，正确的是（\u3000）。\n![](/static/uploads/a.jpg)",
    ),
    # 半角括号统一成全角
    (
        "正确的是(   )。\n\\begin{choices}\n\\item a\n\\end{choices}",
        "正确的是（\u3000）。\n\\begin{choices}\n\\item a\n\\end{choices}",
    ),
    # exam-zh 的 \paren 宏也并成一个括号
    (
        "正确的是 \\paren\n\\begin{choices}\n\\item a\n\\end{choices}",
        "正确的是（\u3000）\n\\begin{choices}\n\\item a\n\\end{choices}",
    ),
    # 连续两组作答位只留一个
    (
        "正确的是（\u3000）（\u3000）\n\\begin{choices}\n\\item a\n\\end{choices}",
        "正确的是（\u3000）\n\\begin{choices}\n\\item a\n\\end{choices}",
    ),
])
def test_choice_answer_slot_becomes_parens(raw, expected):
    assert _normalize(raw) == expected


def test_choice_blank_is_never_an_underscore():
    """回归：库里出现过的"单选题最后还是下划线"。"""
    raw = "2025年世界人形机器人运动会竞技赛8月17日的赛程从早上8时40分开始，到下午6时结束，用时___。"
    result = _normalize(raw)
    assert "\\fillin" not in result
    assert "___" not in result
    assert "（\u3000）" in result


def test_choice_mid_stem_blank_is_preserved():
    """题干中间的真填空不能被吞掉，也不能被换成第二个作答位。"""
    raw = "应沿（\u3000）号线剪开。\n\\begin{choices}\n\\item 1\n\\end{choices}"
    result = _normalize(raw)
    assert result.count("（\u3000）") == 1
    assert result.startswith("应沿（\u3000）号线剪开")


def test_choice_saving_is_idempotent():
    """连按多次保存，括号不会越积越多，也不会多出空行。"""
    raw = "下面哪组算式不能用同一句乘法口诀计算?（\u3000）\r\n\\begin{choices}\r\n\\item a\r\n\\end{choices}"
    once = _normalize(raw)
    twice = _normalize(once)
    assert once == twice == raw


def test_choice_edit_does_not_add_slot_when_missing():
    """手工编辑保存时不能凭空补括号，题干还是原样。"""
    raw = "测试API题目干 $a^2+b^2=c^2$"
    assert _normalize(raw, add_missing=False) == raw


def test_fill_in_blank_question_is_untouched():
    """填空题不受影响：下划线仍然转成 \\fillin。"""
    assert _normalize("一年有___个季节", keep=False) == "一年有\\fillin个季节"


@pytest.mark.parametrize("q_type", ["single_choice", "multi_choice"])
def test_render_always_prints_one_paren(q_type):
    """排版层最后一道保障：任何残留都印成一个括号，且只有一个。"""
    import re

    from mathbank.paper_helper import clean_content_for_latex

    cases = [
        "是\\fillin。\n\\begin{choices}\n\\item a\n\\end{choices}",
        "是___。\n\\begin{choices}\n\\item a\n\\end{choices}",
        "是（\u3000）。\n\\begin{choices}\n\\item a\n\\end{choices}",
        "是？\n\\begin{choices}\n\\item a\n\\end{choices}",
        "正确的是\\fillin。\n![](/static/uploads/a.jpg)",
    ]
    for raw in cases:
        out = clean_content_for_latex(raw, q_type=q_type)
        head = out.split("\\begin{choices}")[0] if "\\begin{choices}" in out else out
        assert "\\fillin" not in head, (raw, out)
        assert not re.search(r"(?<!\w)_{3,}", head), (raw, out)
        assert len(re.findall(r"[（(][\s\u3000]*[)）]", head)) == 1, (raw, out)
