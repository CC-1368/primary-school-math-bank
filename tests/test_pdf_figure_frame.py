"""Raster frame refinement: stems, gutters and sliced graphics.

The vision model draws a rectangle by eye, so the crop it produces regularly
swallows a stem line, a neighbouring column or the binding rule, and just as
regularly slices the bottom off the picture. These tests pin down what the
page raster may and may not be used to change.
"""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from mathbank import pdf_raster_guard as guard


FIGURE = (140, 430, 460, 590)  # left, top, right, bottom


def _text_line(draw, x0, x1, y, size=11):
    x = x0
    while x + size <= x1:
        draw.rectangle([x, y, x + size - 3, y + size], fill="black")
        x += size + 2


def _build_page(path: Path, *, right_column: bool = True, labels: bool = True) -> Path:
    image = Image.new("RGB", (1200, 860), "white")
    draw = ImageDraw.Draw(image)
    for y in range(60, 400, 26):
        _text_line(draw, 90, 560, y)
    for y in range(600, 820, 26):
        _text_line(draw, 90, 560, y)
    if right_column:
        for y in range(60, 820, 26):
            _text_line(draw, 660, 1140, y)
    left, top, right, bottom = FIGURE
    draw.rectangle([left, top, right, bottom], outline="black", width=3)
    draw.line([left, (top + bottom) // 2, right, (top + bottom) // 2], fill="black", width=2)
    draw.line([(left + right) // 2, top, (left + right) // 2, bottom], fill="black", width=2)
    if labels:
        _text_line(draw, left - 30, left - 4, top + 20, size=12)
        _text_line(draw, right + 6, right + 34, top + 20, size=12)
    image.save(path)
    return path


def _norm_to_px(box, size):
    return [box[0] * size[0] / 1000, box[1] * size[1] / 1000,
            box[2] * size[0] / 1000, box[3] * size[1] / 1000]


def _px_to_norm(box, size):
    return [round(box[0] * 1000 / size[0], 1), round(box[1] * 1000 / size[1], 1),
            round(box[2] * 1000 / size[0], 1), round(box[3] * 1000 / size[1], 1)]


@pytest.fixture()
def page(tmp_path):
    return _build_page(tmp_path / "page.png")


def test_stem_line_above_the_figure_is_not_shipped(page):
    size = Image.open(page).size
    box = _px_to_norm([100, 370, 600, 610], size)  # 含上方一行题干与整张图
    result = guard.refine_figure_frame(str(page), box)
    assert result["changed"] is True
    refined = _norm_to_px(result["bbox"], size)
    assert refined[1] >= FIGURE[1] - 6, "题干行仍在图框内"
    assert refined[3] >= FIGURE[3] - 6, "图形被裁掉了"


def test_sliced_figure_gets_its_missing_part_back(page):
    size = Image.open(page).size
    box = _px_to_norm([150, 440, 300, 580], size)  # 右侧与下侧被切
    result = guard.refine_figure_frame(str(page), box)
    refined = _norm_to_px(result["bbox"], size)
    assert refined[2] >= FIGURE[2] - 8, "图形右侧没有补全"
    assert refined[3] >= FIGURE[3] - 8, "图形底部没有补全"


def test_frame_never_crosses_the_neighbouring_column(page):
    size = Image.open(page).size
    box = _px_to_norm([380, 440, 760, 600], size)  # 跨到右栏文字
    result = guard.refine_figure_frame(str(page), box)
    refined = _norm_to_px(result["bbox"], size)
    assert refined[2] <= 620, "图框跨进了相邻栏"


def test_a_frame_that_already_hugs_the_figure_is_left_alone(page):
    size = Image.open(page).size
    # 图 + 两侧地名标注就是正确的裁框，校正不应把它挪走
    box = _px_to_norm([FIGURE[0] - 32, FIGURE[1] - 2, FIGURE[2] + 36, FIGURE[3] + 2], size)
    result = guard.refine_figure_frame(str(page), box)
    refined = _norm_to_px(result["bbox"], size)
    assert abs(refined[0] - (FIGURE[0] - 32)) <= 10
    assert abs(refined[2] - (FIGURE[2] + 36)) <= 10
    assert abs(refined[1] - (FIGURE[1] - 2)) <= 10
    assert abs(refined[3] - (FIGURE[3] + 2)) <= 10


def test_page_wide_slices_are_refused_as_unknown_evidence(page):
    size = Image.open(page).size
    box = _px_to_norm([0, 0, 1200, 860], size)
    result = guard.refine_figure_frame(str(page), box)
    assert result["changed"] is False or result["bbox"] != box


def test_missing_page_image_keeps_the_model_box(tmp_path):
    box = [100, 100, 400, 300]
    result = guard.refine_figure_frame(str(tmp_path / "missing.png"), box)
    assert result["bbox"] == box
    assert result["changed"] is False
    assert result["notes"]


def test_invalid_box_is_reported_not_guessed(page):
    result = guard.refine_figure_frame(str(page), [10, 10, 5, 300])
    assert result["changed"] is False
    assert result["warnings"]
