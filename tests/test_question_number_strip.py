"""Word/PDF 整卷导入：题干不保留题号与出处标注。

题号（`1.`、`2．`、`3、`、`第4题`）和出处标注（`（2024·全国·高考真题）`）属于
卷面排版信息，导入后不应进入题库题干；`source` 字段的既有逻辑保持不变。
"""


def _question(content):
    return {
        "content": content,
        "answer_markdown": "",
        "question_type": "detailed_answer",
        "category_compulsory": "必修一",
        "category_chapter": "1. 集合与常用逻辑用语",
        "difficulty": "easy_error",
        "source": None,
        "referenced_images": [],
    }


def test_strip_question_number_prefix_variants():
    from main import strip_question_number_prefix as strip

    assert strip("1. 计算 $1+1$。") == "计算 $1+1$。"
    assert strip("2．已知 $x=1$。") == "已知 $x=1$。"
    assert strip("3、求 $2+2$。") == "求 $2+2$。"
    assert strip("第4题 已知 $a>0$。") == "已知 $a>0$。"
    assert strip("5,求 $x$。") == "求 $x$。"


def test_strip_question_number_keeps_math_and_subquestions():
    from main import strip_question_number_prefix as strip

    # 小问编号是题干内容，不能当题号删掉
    assert strip("（1）有 12 枝花；（2）平均插在 2 个花瓶里。") == "（1）有 12 枝花；（2）平均插在 2 个花瓶里。"
    assert strip("(1) 求 $x$ 的值。") == "(1) 求 $x$ 的值。"
    # 小数、带单位的数值、千分位不是题号
    assert strip("3.14 是圆周率的近似值。") == "3.14 是圆周率的近似值。"
    assert strip("1.5 元一支，买 3 支多少钱？") == "1.5 元一支，买 3 支多少钱？"
    assert strip("1,000 元存一年利息多少？") == "1,000 元存一年利息多少？"
    assert strip("12 个苹果平均分给 3 人，每人几个？") == "12 个苹果平均分给 3 人，每人几个？"


def test_strip_question_number_keeps_empty_shell():
    from main import strip_question_number_prefix as strip

    # 纯题号空壳原样保留，避免生成空题卡片
    assert strip("7.") == "7."
    assert strip("   ") == "   "
    assert strip("") == ""


def test_strip_source_annotation_only_for_source_like_brackets():
    from main import strip_source_annotation as strip

    assert strip("（2024·全国·高考真题）已知 $a>0$。") == "已知 $a>0$。"
    assert strip("【2023·北京中考】计算 $1+2$。") == "计算 $1+2$。"
    assert strip("(2022年厦门质检)求 $x$。") == "求 $x$。"
    # 正文括号不能误删
    assert strip("（如图）在 $\\triangle ABC$ 中，$AB=3$。") == "（如图）在 $\\triangle ABC$ 中，$AB=3$。"
    assert strip("（单位：cm）求面积。") == "（单位：cm）求面积。"
    assert strip("（1）求 $x$ 的值。") == "（1）求 $x$ 的值。"


def test_post_process_drops_number_and_source_annotation():
    import main

    questions = [
        _question("1. 计算 $1+1$。"),
        _question("2．已知 $x=1$，求 $x^2$。"),
        _question("1.（2024·全国·高考真题）已知 $a>0$，求最小值。"),
        _question("（1）有 12 枝花，平均插在 2 个花瓶里。"),
    ]
    processed = main.post_process_pdf_parsed_questions(questions, "测试卷", None, None)

    assert [q["content"] for q in processed] == [
        "计算 $1+1$。",
        "已知 $x=1$，求 $x^2$。",
        "已知 $a>0$，求最小值。",
        "（1）有 12 枝花，平均插在 2 个花瓶里。",
    ]
    # source 字段维持原有逻辑（此处为试卷标题），本次改动不涉及
    assert all(q["source"] == "测试卷" for q in processed)


def test_post_process_drops_number_in_preserved_source_body():
    import main

    questions = [_question("3.（2023·北京中考）计算 $\\sqrt{4}$。")]
    processed = main.post_process_pdf_parsed_questions(
        questions, "保真卷", None, None, preserved_indices=[0]
    )

    assert processed[0]["content"] == "计算 $\\sqrt{4}$。"
    assert processed[0]["source"] == "保真卷"


def test_enforce_stem_without_number_reapplies_after_source_verification():
    """回归：原文核验把题干还原成原卷原文后，题号会回填，交付前必须再剥一次。"""
    import main

    questions = [
        _question("3. 王大爷要给菜地围篱笆，哪种围法更牢固？"),
        _question("4.38元中的“3”表示的意思是（　　）。"),
        _question(""),
    ]

    assert main.enforce_stem_without_number(questions) == 1
    assert questions[0]["content"] == "王大爷要给菜地围篱笆，哪种围法更牢固？"
    # 以小数开头的正文不能被当成题号
    assert questions[1]["content"] == "4.38元中的“3”表示的意思是（　　）。"
    assert questions[2]["content"] == ""
