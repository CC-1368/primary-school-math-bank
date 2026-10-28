"""AI classification candidate pools respect installed textbooks and grades.

Orphan knowledge points (no textbook path) must never re-enter AI prompts,
and a locked grade narrows the curriculum pool without changing validators.
"""
import pytest

from mathbank.ai_knowledge_points import build_ai_knowledge_prompt, load_ai_knowledge_catalog
from mathbank.database import KnowledgePoint, KnowledgePointCurriculum
from mathbank.prompts import (
    build_classification_system_prompt,
    build_grade_lock_rule,
    build_pdf_parse_system_prompt,
    detect_grade_books,
    filter_curriculum_to_books,
    grade_pool_and_rule,
)

TWO_BOOKS = {
    "二年级上册": {"一 分类与整理": ["1.1 按不同标准分类"], "二 1~6的表内乘法": []},
    "六年级上册": {"三 分数除法": ["3.1 倒数的认识"]},
}


@pytest.fixture()
def seeded_points(db_session):
    mapped = KnowledgePoint(code="9001", name="表内除法", original_name="表内除法", source="local", _aliases="[]")
    orphan = KnowledgePoint(code="9002", name="集合的含义", original_name="集合的含义", source="legacy", _aliases="[]")
    db_session.add_all([mapped, orphan])
    db_session.flush()
    db_session.add(KnowledgePointCurriculum(
        point_code="9001", version_code="A", compulsory="二年级上册",
        chapter="二 1~6的表内乘法", knowledge="", reference_section="", level="chapter"))
    db_session.commit()
    return mapped, orphan


def test_orphan_points_never_enter_ai_catalog(seeded_points, db_session):
    catalog = load_ai_knowledge_catalog(db_session)
    codes = {point["code"] for point in catalog}
    assert "9001" in codes
    assert "9002" not in codes


def test_empty_knowledge_pool_tells_model_to_return_no_points():
    prompt = build_ai_knowledge_prompt([])
    assert "恒返回 []" in prompt
    assert "允许跨章节选点" not in prompt


def test_mapped_points_keep_cross_chapter_prompt():
    prompt = build_ai_knowledge_prompt([{"code": "9001", "name": "表内除法",
                                         "original_name": "表内除法", "aliases": []}])
    assert "9001" in prompt
    assert "允许跨章节选点" in prompt


@pytest.mark.parametrize("texts,expected", [
    (("二年级上册数学期中测试",), {"二年级上册"}),
    (("2年级上册期中",), {"二年级上册"}),
    (("二年级数学期末测试",), {"二年级上册"}),
    (("二年级上册卷", "六年级上册答案"), {"二年级上册", "六年级上册"}),
    (("数学期末测试",), set()),
    (("四年级上册",), set()),
])
def test_grade_hints_collect_installed_books(texts, expected):
    assert detect_grade_books(TWO_BOOKS, *texts) == frozenset(expected)


def test_single_named_book_locks_the_pool():
    pool, rule = grade_pool_and_rule(TWO_BOOKS, "六年级上册期末测试")
    assert list(pool) == ["六年级上册"]
    assert "册次锁定" in rule


def test_grade_without_volume_narrows_to_its_two_books():
    pool, rule = grade_pool_and_rule(TWO_BOOKS, "二年级数学期末测试")
    assert list(pool) == ["二年级上册"]
    # TWO_BOOKS only installs one grade-two book, so one hint already locks it.


def test_conflicting_grades_keep_the_full_pool():
    pool, rule = grade_pool_and_rule(TWO_BOOKS, "二年级上册卷", "六年级上册答案")
    assert list(pool) == ["二年级上册", "六年级上册"]
    assert rule == ""


def test_lock_ignores_books_outside_curriculum():
    pool, rule = grade_pool_and_rule(TWO_BOOKS, "必修一第一章")
    assert list(pool) == ["二年级上册", "六年级上册"]
    assert rule == ""


def test_grade_lock_rule_mentions_both_field_names():
    rule = build_grade_lock_rule("二年级上册")
    assert "compulsory" in rule and "category_compulsory" in rule


def test_classification_prompt_locks_expected_book():
    prompt = build_classification_system_prompt(TWO_BOOKS, expected_compulsory="二年级上册")
    assert "六年级上册" not in prompt
    assert "册次锁定" in prompt


def test_classification_prompt_without_lock_keeps_full_pool():
    prompt = build_classification_system_prompt(TWO_BOOKS)
    assert "二年级上册" in prompt and "六年级上册" in prompt
    assert "册次锁定" not in prompt


def test_pdf_parse_prompt_places_grade_rule_before_priority_rule():
    prompt = build_pdf_parse_system_prompt(TWO_BOOKS, False, grade_rule=build_grade_lock_rule("六年级上册"))
    assert prompt.index("册次锁定") < prompt.index("多模块融合题定位")
