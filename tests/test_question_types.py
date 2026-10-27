import pytest

from mathbank.question_types import (
    detect_structured_question_form,
    looks_like_true_false,
    normalize_ai_question_form,
)


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        (r"答案为\fillin", "fill_in_blank"),
        (r"答案为\FILLIN", "fill_in_blank"),
        (r"\begin{choices}\item A\end{choices}", "choice"),
        (r"\begin { choices }\item A\end{choices}", "choice"),
        ("请证明该结论", None),
    ),
)
def test_structured_question_form_detection(content, expected):
    assert detect_structured_question_form(content) == expected


def test_choices_environment_takes_precedence_over_blank_inside_an_option():
    content = r"\begin{choices}\item \fillin\item 2\end{choices}"

    assert detect_structured_question_form(content) == "choice"


@pytest.mark.parametrize(
    ("model_value", "expected"),
    (
        ("single_choice", "choice"),
        ("multi_choice", "choice"),
        ("单选题", "choice"),
        ("多选题", "choice"),
        ("fill_in_blank", "fill_in_blank"),
        ("true_false", "true_false"),
        ("判断题", "true_false"),
        ("对错题", "true_false"),
        ("detailed_answer", "detailed_answer"),
        ("unexpected", "unknown"),
        (None, "unknown"),
    ),
)
def test_ai_question_form_never_preserves_single_or_multi_choice(model_value, expected):
    assert normalize_ai_question_form(model_value) == expected


def test_local_pdf_structure_accepts_model_judged_true_false():
    from mathbank.pdf_local_structure import _TYPES

    assert "true_false" in _TYPES


def test_true_false_type_rule_is_shared_by_prompts():
    from mathbank.question_types import TRUE_FALSE_TYPE_RULE

    assert "true_false" in TRUE_FALSE_TYPE_RULE
    assert "正确/错误" in TRUE_FALSE_TYPE_RULE
    assert "detailed_answer" in TRUE_FALSE_TYPE_RULE


@pytest.mark.parametrize(
    ("content", "answer", "expected"),
    (
        # 库里真实误标样例：判断句 + 末尾空括号 + 答案是 √/×
        ("在小数的末尾添上0或去掉0，小数的大小不变。（　　　）", "√", True),
        ("小数都比整数小。（　　）", "×", True),
        ("判断下面各题，对的画√。", "[EXTRACTED_ORIGINAL] √", True),
        # 选择题：答案是字母，或有 choices/选项行
        ("下列小数中，与0.7相等的是（　　　）。", "B", False),
        (
            "下列结论正确的是（　　）\n\\begin{choices}\\item A\\item B\\end{choices}",
            "√",
            False,
        ),
        # 多选“画√”小题：答案不是单一结论
        ("下面哪组分法是平均分？在对应的（　　　）里画“√”。", "第 1 组画“√”。第 2 组不是", False),
        # 填空/计算题
        ("在◯里填上“+”“-”“×”或“÷”。", "— ×    ÷ ×", False),
        ("计算 $18\\div3$。", "6", False),
        ("没有答案的题目。（　　）", "", False),
    ),
)
def test_looks_like_true_false_only_fires_on_a_single_verdict(content, answer, expected):
    assert looks_like_true_false(content, answer) is expected
