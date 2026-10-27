"""选择题选项标号清理：模板自动生成 A./B.，题干里不能再保留一份。

覆盖导入链路（PDF/Word/结构化解析）与手工粘贴共用的
`mathbank.tex_helper.strip_choice_item_labels`。
"""

from mathbank.tex_helper import strip_choice_item_labels


def test_strips_common_duplicate_labels():
    source = (
        "下列小数中，与0.7相等的是（　　　）。\n"
        "\\begin{choices}\n"
        "\\item A. 0.07\n"
        "\\item B．0.70\n"
        "\\item C、7.0\n"
        "\\item (D) 0.69\n"
        "\\end{choices}"
    )
    cleaned = strip_choice_item_labels(source)
    assert "\\item 0.07" in cleaned
    assert "\\item 0.70" in cleaned
    assert "\\item 7.0" in cleaned
    assert "\\item 0.69" in cleaned
    assert "A. 0.07" not in cleaned
    assert "B．0.70" not in cleaned


def test_strips_chinese_parenthesised_labels():
    source = "\\begin{choices}\n\\item （A）3元\n\\item （B）3角\n\\end{choices}"
    cleaned = strip_choice_item_labels(source)
    assert cleaned == "\\begin{choices}\n\\item 3元\n\\item 3角\n\\end{choices}"


def test_keeps_legitimate_text_starting_with_letters():
    # 「A、B 两点」是选项正文，不是重复标号；非 choices 环境的 \item 也不动。
    source = (
        "\\begin{choices}\n"
        "\\item A、B 两点之间的距离\n"
        "\\item 3 厘米\n"
        "\\end{choices}\n"
        "\\begin{enumerate}\n\\item A. 保留原样\n\\end{enumerate}"
    )
    cleaned = strip_choice_item_labels(source)
    assert "A、B 两点之间的距离" in cleaned
    assert "\\item A. 保留原样" in cleaned


def test_keeps_option_that_is_only_a_label():
    # 删除标号后没有正文时保留原样，避免产生空选项。
    source = "\\begin{choices}\n\\item A.\n\\item B. 2\n\\end{choices}"
    cleaned = strip_choice_item_labels(source)
    assert "\\item A." in cleaned
    assert "\\item 2" in cleaned


def test_no_choices_environment_untouched():
    assert strip_choice_item_labels("1. 计算 $1+1$。") == "1. 计算 $1+1$。"
    assert strip_choice_item_labels("") == ""
