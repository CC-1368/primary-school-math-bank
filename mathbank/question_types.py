"""Pure helpers for question form detection and paper type grouping."""

import re


PAPER_TYPE_ORDER = ("single_choice", "multi_choice", "fill_in_blank", "true_false", "detailed_answer")


def normalize_section_order(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def paper_type_order(present_types, paper_type: str, question_types=None, section_order=None) -> list[str]:
    """Use saved section order, appending any types that were added afterwards."""
    order = list(PAPER_TYPE_ORDER)
    if paper_type == "exam_19":
        return order
    present = list(dict.fromkeys(present_types))
    for item in question_types or []:
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, str) and value in present and value not in order:
            order.append(value)
    order.extend(value for value in present if value not in order)
    preferred = normalize_section_order(section_order)
    return preferred + [value for value in order if value not in preferred]


def custom_type_labels(question_types=None) -> dict[str, str]:
    """Custom labels are plain text; each output format must escape them itself."""
    labels = {}
    for item in question_types or []:
        if not isinstance(item, dict):
            continue
        value, label = item.get("value"), item.get("label")
        if isinstance(value, str) and isinstance(label, str) and label.strip():
            labels.setdefault(value, label)
    return labels


def is_written_question_type(q_type: str) -> bool:
    # 判断题的作答只有对错符号，不需要解答留白
    return q_type not in ("single_choice", "multi_choice", "fill_in_blank", "true_false")


QUESTION_FORM_CHOICE = "choice"
QUESTION_FORM_FILL_IN_BLANK = "fill_in_blank"
QUESTION_FORM_TRUE_FALSE = "true_false"
QUESTION_FORM_DETAILED_ANSWER = "detailed_answer"
QUESTION_FORM_UNKNOWN = "unknown"

# Shared wording for every prompt that lets a model pick a question type, so
# PDF / Word / single-file imports all judge 判断题 the same way.
TRUE_FALSE_TYPE_RULE = (
    "判断题判定：原卷标有“判断题/判断对错/对的画√错的画×”等分节，或题干本身是完整陈述句、"
    "作答只要求回答“正确/错误（√/×）”，且没有 A/B/C/D 选项、没有 \\fillin 或成串待填空格时，"
    "题型必须判为 true_false；判断题保留原作答括号（如末尾“（　）”），不要改成 choices 环境或 \\fillin，"
    "答案第一行写“正确”或“错误”（原卷为 √/× 时同样转写）。"
    "需要写出过程、证明或多步计算结果的是 detailed_answer；只有确实留下待填空位才是 fill_in_blank。"
)


_CHOICES_ENV_PATTERN = re.compile(
    r"\\begin\s*\{\s*choices\s*\}",
    re.IGNORECASE,
)
_FILLIN_PATTERN = re.compile(r"\\fillin\b", re.IGNORECASE)


def detect_structured_question_form(content: str) -> str | None:
    """Return a definitive form only when normalized LaTeX structure proves it.

    A choices environment takes precedence because a choice option can itself
    contain a blank-like expression without changing the enclosing question
    into a fill-in-the-blank item.
    """

    normalized = str(content or "")
    if _CHOICES_ENV_PATTERN.search(normalized):
        return QUESTION_FORM_CHOICE
    if _FILLIN_PATTERN.search(normalized):
        return QUESTION_FORM_FILL_IN_BLANK
    return None


_TRAILING_BLANK = re.compile(r"[（(][\s\u3000]*[）)]\s*[。．.]?\s*$")
_TRUE_FALSE_ANSWER = re.compile(
    r"^\s*(?:√|×|✓|✗|✔|✘|对|错|正确|错误|对的|错的)\s*[。．.]?\s*$"
)
_TRUE_FALSE_HINT = re.compile(
    r"(判断|正误|对的画|错的画|正确的画|错误的画|画\s*[“\"]?√|打\s*[“\"]?√)"
)
_OPTION_LINE = re.compile(r"(?:^|\n)\s*(?:[A-H][．.、)]|\\item\b|[（(][A-H][）)])")


def looks_like_true_false(content: str, answer: str = "") -> bool:
    """Structural fallback for 判断题 when a model still labels it as something else.

    Only fires when the answer itself is a single 对/错 verdict and the stem has
    no option list: this keeps multi-select "画√" items and choice questions out.
    """

    text = str(content or "")
    if _CHOICES_ENV_PATTERN.search(text) or _OPTION_LINE.search(text):
        return False
    lines = [line.strip() for line in str(answer or "").splitlines() if line.strip()]
    if not lines:
        return False
    first_line = re.sub(r"^\[EXTRACTED_ORIGINAL\]\s*", "", lines[0])
    if not _TRUE_FALSE_ANSWER.match(first_line):
        return False
    return bool(_TRAILING_BLANK.search(text) or _TRUE_FALSE_HINT.search(text))


def normalize_ai_question_form(value: object) -> str:
    """Collapse model output to a coarse form that cannot encode single/multi."""

    normalized = str(value or "").strip().lower()
    mapping = {
        "choice": QUESTION_FORM_CHOICE,
        "choice_question": QUESTION_FORM_CHOICE,
        "single_choice": QUESTION_FORM_CHOICE,
        "multi_choice": QUESTION_FORM_CHOICE,
        "选择题": QUESTION_FORM_CHOICE,
        "单选题": QUESTION_FORM_CHOICE,
        "多选题": QUESTION_FORM_CHOICE,
        "fill_in_blank": QUESTION_FORM_FILL_IN_BLANK,
        "fill-in-blank": QUESTION_FORM_FILL_IN_BLANK,
        "填空题": QUESTION_FORM_FILL_IN_BLANK,
        "true_false": QUESTION_FORM_TRUE_FALSE,
        "true-false": QUESTION_FORM_TRUE_FALSE,
        "truefalse": QUESTION_FORM_TRUE_FALSE,
        "tf": QUESTION_FORM_TRUE_FALSE,
        "judge": QUESTION_FORM_TRUE_FALSE,
        "judgement": QUESTION_FORM_TRUE_FALSE,
        "judgment": QUESTION_FORM_TRUE_FALSE,
        "判断题": QUESTION_FORM_TRUE_FALSE,
        "判断": QUESTION_FORM_TRUE_FALSE,
        "对错题": QUESTION_FORM_TRUE_FALSE,
        "正误题": QUESTION_FORM_TRUE_FALSE,
        "detailed_answer": QUESTION_FORM_DETAILED_ANSWER,
        "解答题": QUESTION_FORM_DETAILED_ANSWER,
        "unknown": QUESTION_FORM_UNKNOWN,
        "未知": QUESTION_FORM_UNKNOWN,
    }
    return mapping.get(normalized, QUESTION_FORM_UNKNOWN)
