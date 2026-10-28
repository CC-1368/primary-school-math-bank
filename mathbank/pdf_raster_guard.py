"""Bounded, local evidence for suspicious scanned-PDF illustration rectangles.

Only a clearly isolated connected line drawing with nearby short labels can
justify a correction. This is not OCR: text-heavy, ambiguous and unsupported
pages keep their original rectangles and request review. No files are written.
"""

from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
import statistics
import time

from PIL import Image, ImageFilter


MAX_SOURCE_PIXELS = 30_000_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ANALYSIS_SIDE = 1300
MAX_RUNS = 200_000
MAX_COMPONENTS = 12_000
MAX_SECONDS = 3.0
MAX_LINE_COMPONENTS = 16
_RUN = re.compile(b"\xff+")


class _EvidenceLimit(ValueError):
    pass


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
            return None
    except OverflowError:
        return None
    box = tuple(float(v) for v in value)
    return box if 0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000 else None


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _intersection(first, second):
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]))


def _union(first, second):
    return (min(first[0], second[0]), min(first[1], second[1]),
            max(first[2], second[2]), max(first[3], second[3]))


def _gap(first, second):
    dx = max(0, first[0] - second[2], second[0] - first[2])
    dy = max(0, first[1] - second[3], second[1] - first[3])
    return math.hypot(dx, dy)


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _components(mask, deadline):
    """Eight-connected row runs, with capped work and union-find components."""
    width, height = mask.size
    data = mask.tobytes()
    parents, boxes, counts, longest, run_boxes = [], [], [], [], []

    def find(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    def join(first, second):
        first, second = find(first), find(second)
        if first == second:
            return first
        if counts[first] < counts[second]:
            first, second = second, first
        parents[second] = first
        boxes[first] = _union(boxes[first], boxes[second])
        counts[first] += counts[second]
        longest[first] = max(longest[first], longest[second])
        return first

    previous = []
    for y in range(height):
        if y % 24 == 0 and time.monotonic() > deadline:
            raise _EvidenceLimit("本地图形分析达到时间上限")
        current, pointer = [], 0
        for match in _RUN.finditer(data, y * width, (y + 1) * width):
            left, right = match.start() - y * width, match.end() - y * width
            node = len(parents)
            if node >= MAX_RUNS:
                raise _EvidenceLimit("本页图像纹理过多，超过连通分析上限")
            parents.append(node)
            boxes.append((left, y, right, y + 1))
            run_boxes.append(boxes[-1])
            counts.append(right - left)
            longest.append(right - left)
            while pointer < len(previous) and previous[pointer][1] < left:
                pointer += 1
            other = pointer
            while other < len(previous) and previous[other][0] <= right:
                join(node, previous[other][2])
                other += 1
            current.append((left, right, node))
        previous = current
    result, tracked = [], {}
    for node, parent in enumerate(parents):
        if node == parent and counts[node] >= 5:
            component = {"bbox": boxes[node], "pixels": counts[node], "max_run": longest[node]}
            result.append(component)
            left, top, right, bottom = boxes[node]
            if ((right-left) / width >= 0.03 and (bottom-top) / height >= 0.03
                    and (right-left) * (bottom-top) / (width*height) >= 0.0025):
                component["runs"] = []
                tracked[node] = component
            if len(result) > MAX_COMPONENTS:
                raise _EvidenceLimit("本页小组件过多，无法可靠区分图形和文字")
    for node, box in enumerate(run_boxes):
        if node % 4096 == 0 and time.monotonic() > deadline:
            raise _EvidenceLimit("本地图形分析达到时间上限")
        component = tracked.get(find(node))
        if component is not None:
            component["runs"].append((box[0], box[1], box[2]))
    return result


def _text_rows(components, letter_height, deadline):
    rows = []
    small = [component for component in components
             if 0.45 * letter_height <= component["bbox"][3] - component["bbox"][1] <= 2.2 * letter_height]
    for index, component in enumerate(sorted(small, key=lambda item: (item["bbox"][1] + item["bbox"][3], item["bbox"][0]))):
        if index % 128 == 0 and time.monotonic() > deadline:
            raise _EvidenceLimit("本地图形分析达到时间上限")
        center = (component["bbox"][1] + component["bbox"][3]) / 2
        if rows and abs(center - rows[-1]["center"]) <= letter_height * 0.55:
            rows[-1]["items"].append(component)
        else:
            rows.append({"center": center, "items": [component]})
    prose = []
    for index, row in enumerate(rows):
        if index % 64 == 0 and time.monotonic() > deadline:
            raise _EvidenceLimit("本地图形分析达到时间上限")
        groups = []
        for component in sorted(row["items"], key=lambda item: item["bbox"][0]):
            box = component["bbox"]
            if groups and box[0] - groups[-1]["bbox"][2] <= letter_height * 2.5:
                groups[-1]["bbox"] = _union(groups[-1]["bbox"], box)
                groups[-1]["count"] += 1
            else:
                groups.append({"bbox": box, "count": 1})
        prose.extend(group["bbox"] for group in groups
                     if group["bbox"][2] - group["bbox"][0] >= letter_height * 6
                     and (group["count"] >= 4 or group["bbox"][2] - group["bbox"][0] >= letter_height * 10))
    return prose


def _is_regular_grid(mask, box):
    """A multi-row/column table is not independent line-drawing evidence."""
    def rules(image):
        width, height = image.size
        data = image.tobytes()
        levels = []
        for y in range(height):
            length = max((match.end() - match.start() for match in _RUN.finditer(data, y * width, (y + 1) * width)), default=0)
            if length >= width * 0.75 and (not levels or y - levels[-1][-1] > 2):
                levels.append([y])
            elif length >= width * 0.75:
                levels[-1].append(y)
        return len(levels)
    crop = mask.crop(box)
    return rules(crop) >= 3 and rules(crop.transpose(Image.Transpose.TRANSPOSE)) >= 3


@lru_cache(maxsize=4)
def _page_evidence(path: str, mtime_ns: int, file_size: int):
    del mtime_ns
    deadline = time.monotonic() + MAX_SECONDS
    if file_size > MAX_FILE_BYTES:
        raise _EvidenceLimit("原页图片超过本地核验大小上限")
    with Image.open(path) as image:
        if image.format != "PNG" or image.width * image.height > MAX_SOURCE_PIXELS:
            raise _EvidenceLimit("原页图片格式或像素数超出本地核验范围")
        source_size = image.size
        scale = min(1.0, MAX_ANALYSIS_SIDE / max(image.size))
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        gray = image.convert("L").resize(size, Image.Resampling.LANCZOS)
    # Small dilation reconnects thin scan strokes; it is bounded to one working
    # pixel and never used as a reason to consume nearby paragraph text.
    mask = gray.point(lambda value: 255 if value < 165 else 0).filter(ImageFilter.MaxFilter(3))
    components = _components(mask, deadline)
    if time.monotonic() > deadline:
        raise _EvidenceLimit("本地图形分析达到时间上限")
    heights = [c["bbox"][3] - c["bbox"][1] for c in components
               if 4 <= c["bbox"][3] - c["bbox"][1] <= size[1] * 0.035
               and c["bbox"][2] - c["bbox"][0] <= size[0] * 0.05]
    letter_height = float(statistics.median(heights)) if heights else 10.0
    text_rows = _text_rows(components, letter_height, deadline)
    large = []
    for component in components:
        box = component["bbox"]
        w, h = box[2] - box[0], box[3] - box[1]
        density = component["pixels"] / max(1, w * h)
        if (w / size[0] >= 0.075 and h / size[1] >= 0.065
                and 0.35 <= w / h <= 3.5 and w * h / (size[0] * size[1]) <= 0.25
                and 0.015 <= density <= 0.28 and component["max_run"] >= max(20, w * 0.25)):
            large.append(component)
    if len(large) > MAX_LINE_COMPONENTS:
        raise _EvidenceLimit("大图形候选过多，无法可靠确定独立图形")
    figures = []
    line_components = []
    for component in large:
        box = component["bbox"]
        if any(_intersection(box, row) > 0 for row in text_rows) or _is_regular_grid(mask, box):
            continue
        # A short label can already be connected to a graph's strokes, or lie
        # inside its core rectangle. Keep this independently filtered line
        # evidence even when there are too few separate outside labels below.
        line_components.append(component)
        radius = min(24.0, max(8.0, letter_height * 1.6))
        labels = []
        for index, other in enumerate(components):
            if index % 128 == 0 and time.monotonic() > deadline:
                raise _EvidenceLimit("本地图形分析达到时间上限")
            if other is component:
                continue
            label = other["bbox"]
            if (label[2] - label[0] <= letter_height * 3.5 and label[3] - label[1] <= letter_height * 2.0
                    and label[3] - label[1] >= 3 and other["pixels"] >= 7
                    and _gap(box, label) <= radius
                    and not _contains(box, label)
                    and not any(_intersection(label, row) > 0 for row in text_rows)):
                labels.append(label)
        if not 2 <= len(labels) <= 24:
            continue
        evidence = box
        for label in labels:
            evidence = _union(evidence, label)
        evidence = (max(0, evidence[0] - 2), max(0, evidence[1] - 2),
                    min(size[0], evidence[2] + 2), min(size[1], evidence[3] + 2))
        if any(_intersection(evidence, row) > 0 for row in text_rows):
            continue
        normalized = [evidence[0] * 1000 / size[0], evidence[1] * 1000 / size[1],
                      evidence[2] * 1000 / size[0], evidence[3] * 1000 / size[1]]
        figures.append({"bbox": tuple(normalized), "labels": len(labels),
                        "core_bbox": tuple([box[0] * 1000 / size[0], box[1] * 1000 / size[1],
                                            box[2] * 1000 / size[0], box[3] * 1000 / size[1]])})
    if time.monotonic() > deadline:
        raise _EvidenceLimit("本地图形分析达到时间上限")
    significant = tuple(
        (box[0] * 1000 / size[0], box[1] * 1000 / size[1],
         box[2] * 1000 / size[0], box[3] * 1000 / size[1])
        for component in components for box in [component["bbox"]]
        if (box[2] - box[0]) / size[0] >= 0.03 and (box[3] - box[1]) / size[1] >= 0.03
        and _area(box) / (size[0] * size[1]) >= 0.0025
    )
    run_records = {
        id(component): {"bbox": tuple(component["bbox"]), "pixels": component["pixels"],
                        "runs": tuple(component["runs"])}
        for component in components if "runs" in component
    }
    # Lettering-sized components are the only extra evidence a diagram is
    # allowed to recruit: vertex letters, 甲地/乙地, tick labels. Anything that
    # belongs to a prose line is excluded here, before any frame can see it.
    label_boxes = tuple(
        tuple(component["bbox"]) for component in components
        if 8 <= component["pixels"]
        and component["bbox"][3] - component["bbox"][1] <= letter_height * 2.4
        and component["bbox"][2] - component["bbox"][0] <= letter_height * 7
        and not any(_contains(row, component["bbox"]) for row in text_rows)
    )[:4000]
    return {"figures": tuple(figures), "text_rows": tuple(tuple([row[0] * 1000 / size[0], row[1] * 1000 / size[1],
             row[2] * 1000 / size[0], row[3] * 1000 / size[1]]) for row in text_rows),
            "significant_components": significant,
            "components": len(components), "large_components": len(large), "analysis_size": size,
            "source_size": source_size, "letter_height": letter_height,
            "ink_mask": mask.tobytes(), "label_boxes": label_boxes,
            "significant_runs": tuple(run_records.values()),
            "line_components": tuple(run_records[id(component)] for component in line_components)}


def _frame_ink(evidence: dict, bbox) -> int:
    """Count visible ink inside a box, independently of the line-graph detector."""
    width, height = evidence["analysis_size"]
    left, top = int(bbox[0] * width / 1000), int(bbox[1] * height / 1000)
    right, bottom = math.ceil(bbox[2] * width / 1000), math.ceil(bbox[3] * height / 1000)
    pixels = evidence["ink_mask"]
    return sum(pixels[y * width + left:y * width + right].count(255) for y in range(top, bottom))


def _cuts_component(evidence: dict, bbox) -> bool:
    """Use component pixels, not a huge page border's misleading bounding box."""
    width, height = evidence["analysis_size"]
    left, top = bbox[0] * width / 1000, bbox[1] * height / 1000
    right, bottom = bbox[2] * width / 1000, bbox[3] * height / 1000
    for component in evidence["significant_runs"]:
        inside = sum(max(0, min(x1, right) - max(x0, left))
                     for x0, y, x1 in component["runs"] if top <= y < bottom)
        outside = component["pixels"] - inside
        if inside >= 8 and outside > max(8, component["pixels"] * 0.01):
            return True
    return False


FRAME_MAX_TRIM_RATIO = 0.45
FRAME_MAX_GROW_RATIO = 0.30
FRAME_MIN_AREA_RATIO = 0.12
FRAME_PAD_PIXELS = 2
FRAME_BLANK_VALUE = 3


def _profile(strip: Image.Image) -> list[int]:
    """Per-column (or per-row) mean ink of an L-mode mask, 0..255."""
    return list(strip.tobytes())


def _two_dimensional_ink(ink: Image.Image) -> tuple:
    width, height = ink.size
    columns = _profile(ink.resize((width, 1), Image.Resampling.BOX))
    rows = _profile(ink.resize((1, height), Image.Resampling.BOX))
    return columns, rows


def _column_channels(ink: Image.Image) -> list[tuple[int, int]]:
    """Blank gutters and thin printed rules that a crop must never straddle.

    A three-up exam sheet separates printed pages with white gutters and a
    dotted/solid binding rule; a two-column page has its own white gutter.
    Both read as near-empty or as a single column of very dense ink, which no
    illustration interior produces. A channel must stay narrow relative to the
    page, otherwise the white interior of a large diagram would qualify.
    """
    width, height = ink.size
    pixels = ink.tobytes()
    profile = _profile(ink.resize((width, 1), Image.Resampling.BOX))
    max_width = max(6, int(width * 0.08))
    channels, start = [], None
    for x, value in enumerate(profile):
        blank = value <= 1
        if blank and start is None:
            start = x
        elif not blank and start is not None:
            if 3 <= x - start <= max_width:
                channels.append((start, x))
            start = None
    if start is not None and 3 <= width - start <= max_width:
        channels.append((start, width))
    dense = [x for x, value in enumerate(profile) if value >= 120]
    for x in dense:
        if any(a <= x < b for a, b in channels):
            continue
        left = x
        while left - 1 >= 0 and profile[left - 1] >= 120:
            left -= 1
        right = x
        while right + 1 < width and profile[right + 1] >= 120:
            right += 1
        if right + 1 - left <= 4 and _is_continuous_rule(pixels, width, height, (left, right + 1)):
            channels.append((left, right + 1))
    return sorted(channels)


def _is_continuous_rule(pixels: bytes, width: int, height: int, span) -> bool:
    """True when a narrow x span carries ink along most of the page height.

    A binding rule is a continuous line; a text column can be just as dense in
    total ink but is broken between lines, which is what this separates.
    """
    samples = 40
    hits = 0
    for step in range(samples):
        y = min(height - 1, int(height * (step + 0.5) / samples))
        row = y * width
        if any(pixels[row + x] for x in range(span[0], span[1])):
            hits += 1
    return hits >= samples * 0.8


def _component_ink(evidence: dict, box) -> int:
    """Ink of drawn components (not lettering) inside a frame."""
    total = 0
    for component in evidence["significant_runs"]:
        for x0, y, x1 in component["runs"]:
            if box[1] <= y < box[3]:
                total += max(0, min(x1, box[2]) - max(x0, box[0]))
    return total


def _ink_mass(ink: Image.Image, box) -> int:
    left, top, right, bottom = (max(0, int(box[0])), max(0, int(box[1])),
                                min(ink.size[0], int(math.ceil(box[2]))),
                                min(ink.size[1], int(math.ceil(box[3]))))
    if right - left < 1 or bottom - top < 1:
        return 0
    crop = ink.crop((left, top, right, bottom))
    return sum(crop.tobytes()) // 255


def _clamp_columns(frame, ink, evidence, width, height):
    """Never let a figure frame straddle a gutter, binding rule or page margin.

    Which side survives is decided by *drawn* ink, not by total ink: a
    neighbouring column of prose must never outweigh the illustration it
    borders, and a band of empty margin must not decide anything by itself.
    """
    channels = _column_channels(ink)
    if not channels:
        return frame, []
    notes = []
    for _ in range(3):
        changed = False
        for left, right in channels:
            if frame[2] <= left or frame[0] >= right:
                continue
            straddles = frame[0] < left and frame[2] > right
            lands = (left <= frame[0] < right) or (left < frame[2] <= right)
            if not (straddles or lands):
                continue
            left_box = (frame[0], frame[1], left, frame[3])
            right_box = (right, frame[1], frame[2], frame[3])
            keep_left = _component_ink(evidence, left_box)
            keep_right = _component_ink(evidence, right_box)
            if keep_left == keep_right:
                keep_left = _ink_mass(ink, left_box)
                keep_right = _ink_mass(ink, right_box)
            if straddles:
                if keep_left >= keep_right:
                    frame = [frame[0], frame[1], left, frame[3]]
                    changed = True
                else:
                    frame = [right, frame[1], frame[2], frame[3]]
                    changed = True
            elif left <= frame[0] < right and keep_right > keep_left:
                frame = [right, frame[1], frame[2], frame[3]]
                changed = True
            elif left < frame[2] <= right and keep_left >= keep_right:
                frame = [frame[0], frame[1], left, frame[3]]
                changed = True
        if not changed:
            break
        notes.append("已按栏间空白或装订线裁齐配图边界。")
    return frame, list(dict.fromkeys(notes))


def _trim_prose_rows(frame, evidence, width, height):
    """Cut a stem/option line that the frame merely swallowed.

    A removable line must span a good part of the frame, lie in its upper or
    lower half, leave the bulk of the frame intact, be separated from the rest
    by a visible blank gap (so a table cell's own line stays), and not be
    enclosed by a drawn component — that is how a speech bubble's text looks.
    """
    notes = []
    frame_ink = _ink_mass_bytes(evidence, frame)
    if frame_ink <= 0:
        return frame, notes

    def row_ink(y):
        return _ink_mass_bytes(evidence, (frame[0], y, frame[2], y + 1))

    def blank_below(start, limit):
        gap = 0
        y = int(math.ceil(start))
        while gap < limit and y < frame[3] and row_ink(y) == 0:
            gap += 1
            y += 1
        return gap

    def blank_above(start, limit):
        gap = 0
        y = int(math.floor(start))
        while gap < limit and y > frame[1] and row_ink(y - 1) == 0:
            gap += 1
            y -= 1
        return gap

    for _ in range(4):
        changed = False
        for row in evidence["text_rows"]:
            box = (row[0] * width / 1000, row[1] * height / 1000,
                   row[2] * width / 1000, row[3] * height / 1000)
            if _intersection(box, frame) <= 0:
                continue
            frame_h = frame[3] - frame[1]
            frame_w = frame[2] - frame[0]
            row_h = box[3] - box[1]
            if row_h < 2 or row_h > frame_h * 0.30:
                continue
            if any(_contains(component["bbox"], box) for component in evidence["significant_runs"]):
                continue
            overlap_w = max(0, min(box[2], frame[2]) - max(box[0], frame[0])) / max(
                1.0, min(frame_w, box[2] - box[0]))
            if overlap_w < 0.35:
                continue
            if box[3] <= frame[1] + frame_h * 0.45 and frame[3] - box[3] >= frame_h * 0.55:
                if blank_below(box[3] + 1, 3) >= 1:
                    removed = _ink_mass_bytes(evidence, (frame[0], frame[1], frame[2], box[3] + 1))
                    if removed <= frame_ink * 0.5:
                        frame = [frame[0], box[3] + 1, frame[2], frame[3]]
                        notes.append("已去掉混入配图框的题干或选项行。")
                        changed = True
                        break
            if box[1] >= frame[3] - frame_h * 0.45 and box[1] - frame[1] >= frame_h * 0.55:
                if blank_above(box[1] - 1, 3) >= 1:
                    removed = _ink_mass_bytes(evidence, (frame[0], box[1] - 1, frame[2], frame[3]))
                    if removed <= frame_ink * 0.5:
                        frame = [frame[0], frame[1], frame[2], box[1] - 1]
                        notes.append("已去掉混入配图框的题干或选项行。")
                        changed = True
                        break
        if not changed:
            break
    return frame, list(dict.fromkeys(notes))


def _ink_mass_bytes(evidence, box) -> int:
    """Ink pixels inside a frame, from the cached analysis mask."""
    width, height = evidence["analysis_size"]
    pixels = evidence["ink_mask"]
    left = max(0, min(width, int(box[0])))
    right = max(left, min(width, int(math.ceil(box[2]))))
    top = max(0, min(height, int(box[1])))
    bottom = max(top, min(height, int(math.ceil(box[3]))))
    if right - left < 1 or bottom - top < 1:
        return 0
    total = 0
    for y in range(top, bottom):
        total += pixels[y * width + left: y * width + right].count(255)
    return total


def _complete_cut_components(frame, evidence):
    """Give back the part of a graphic that the frame sliced through.

    Two kinds of own evidence qualify: a big drawn component the frame cuts,
    and a lettering-sized component (vertex letters, 甲地/乙地, tick labels)
    that the frame cuts or sits right next to. Paragraph ink never qualifies,
    so a neighbouring stem still cannot be recruited.
    """
    notes = []
    letter = max(4.0, float(evidence.get("letter_height") or 10.0))
    for _ in range(3):
        grown = list(frame)
        for component in evidence["significant_runs"]:
            box = component["bbox"]
            if _intersection(box, frame) <= 0:
                continue
            inside = sum(max(0, min(x1, frame[2]) - max(x0, frame[0]))
                         for x0, y, x1 in component["runs"] if frame[1] <= y < frame[3])
            missing = component["pixels"] - inside
            if inside < 12 or inside < component["pixels"] * 0.15:
                continue
            if missing <= max(12, component["pixels"] * 0.06):
                continue
            proposed, note = _bounded_union(frame, box, evidence=evidence)
            if proposed is None:
                continue
            grown = proposed
            notes.append(note or "已补全被裁框切掉一角的图形本体。")
        for box in evidence.get("label_boxes") or ():
            if _intersection(box, frame) > 0:
                if _contains(frame, box):
                    continue
            elif _gap(frame, box) > letter * 1.2:
                continue
            proposed, _note = _bounded_union(frame, box, slack=letter * 1.2, evidence=evidence)
            if proposed is None:
                continue
            grown = proposed
            notes.append("已把图边被切掉的字母或地名标注补回框内。")
        if grown == frame:
            break
        frame = grown
    return frame, list(dict.fromkeys(notes))


def _bounded_union(frame, box, slack: float = 4.0, evidence: dict | None = None):
    """Union, but only when every moved edge stays inside the growth allowance.

    A sliced graphic is the strongest evidence there is, so it may be completed
    up to a whole drawn object; the allowance still stops a page border or a
    merged table from swallowing the sheet.
    """
    proposed = list(_union(frame, box))
    frame_w = max(1.0, frame[2] - frame[0])
    frame_h = max(1.0, frame[3] - frame[1])
    page_w = page_h = 0.0
    if evidence:
        page_w, page_h = evidence["analysis_size"]
    limit_x = max(frame_w * FRAME_MAX_GROW_RATIO, page_w * 0.18) + slack
    limit_y = max(frame_h * FRAME_MAX_GROW_RATIO, page_h * 0.18) + slack
    if (frame[0] - proposed[0] > limit_x or proposed[2] - frame[2] > limit_x
            or frame[1] - proposed[1] > limit_y or proposed[3] - frame[3] > limit_y):
        return None, ""
    if (proposed[2] - proposed[0]) * (proposed[3] - proposed[1]) > frame_w * frame_h * 4:
        return None, ""
    note = ""
    if proposed[1] < frame[1] or proposed[3] > frame[3]:
        note = "已补全被裁框切掉一角的图形本体。"
    return proposed, note


def _snap_edges(frame, ink):
    """Drop the blank padding around a figure so the frame hugs its ink."""
    width, height = ink.size
    columns, rows = _two_dimensional_ink(ink)
    notes = []
    frame_w = max(1.0, frame[2] - frame[0])
    frame_h = max(1.0, frame[3] - frame[1])
    trim_x, trim_y = frame_w * FRAME_MAX_TRIM_RATIO, frame_h * FRAME_MAX_TRIM_RATIO

    def ink_at(profile, index):
        return 0 <= index < len(profile) and profile[index] > FRAME_BLANK_VALUE

    original = list(frame)
    trimmed = False
    while frame[2] - frame[0] > 8 and not ink_at(columns, int(frame[0])) and original[0] - frame[0] < trim_x:
        frame[0] += 1
        trimmed = True
    while frame[2] - frame[0] > 8 and not ink_at(columns, int(frame[2]) - 1) and frame[2] - original[2] < trim_x:
        frame[2] -= 1
        trimmed = True
    while frame[3] - frame[1] > 8 and not ink_at(rows, int(frame[1])) and original[1] - frame[1] < trim_y:
        frame[1] += 1
        trimmed = True
    while frame[3] - frame[1] > 8 and not ink_at(rows, int(frame[3]) - 1) and frame[3] - original[3] < trim_y:
        frame[3] -= 1
        trimmed = True
    if trimmed:
        notes.append("已收掉配图框四周的多余白边。")
    return frame, notes


def _ink_without_prose(ink: Image.Image, evidence: dict, width: int, height: int) -> Image.Image:
    """Blank out detached prose lines so a border can never stop on the stem.

    Geometry alone cannot tell a stem line from a dimension line, but the text
    evidence can: only lines outside every drawn component are erased, so
    lettering that belongs to a picture (vertex labels, a speech bubble) stays.
    """
    clean = ink.copy()
    for row in evidence.get("text_rows") or ():
        box = (row[0] * width / 1000, row[1] * height / 1000,
               row[2] * width / 1000, row[3] * height / 1000)
        if any(_contains(component["bbox"], box) for component in evidence["significant_runs"]):
            continue
        clean.paste(0, (int(box[0]) - 1, int(box[1]) - 1,
                        int(math.ceil(box[2])) + 1, int(math.ceil(box[3])) + 1))
    return clean


def refine_figure_frame(image_path: str, bbox) -> dict:
    """Rebuild an illustration frame on the page raster the model actually saw.

    Vision models overshoot onto the stem, the option row, a neighbouring
    column or the binding rule, and just as often cut a graphic in half. The
    page raster settles both questions: text lines, gutters and the drawn
    strokes are measurable, while the request itself carries no geometry to
    trust. Corrections stay bounded and are skipped whenever the evidence is
    unusable, so an unsupported page keeps the original rectangle.
    """
    original = _box(bbox)
    result = {"bbox": list(original) if original else bbox,
              "model_bbox": list(original) if original else bbox,
              "changed": False, "method": "unchanged", "warnings": [], "notes": []}
    if original is None:
        result["warnings"] = ["配图坐标无效，未做栅格边界校正。"]
        return result
    try:
        path = Path(image_path)
        stat = path.stat()
        evidence = _page_evidence(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except _EvidenceLimit as exc:
        result["notes"] = [str(exc) + "；未做栅格边界校正，保留原图框。"]
        return result
    except (OSError, ValueError, Image.DecompressionBombError):
        result["notes"] = ["原页图像不可读，未做栅格边界校正，保留原图框。"]
        return result

    width, height = evidence["analysis_size"]
    ink = Image.frombytes("L", (width, height), evidence["ink_mask"])
    frame = [original[0] * width / 1000, original[1] * height / 1000,
             original[2] * width / 1000, original[3] * height / 1000]
    start = list(frame)
    notes = []
    frame, column_notes = _clamp_columns(frame, ink, evidence, width, height)
    notes.extend(column_notes)
    frame, cut_notes = _complete_cut_components(frame, evidence)
    notes.extend(cut_notes)
    frame, row_notes = _trim_prose_rows(frame, evidence, width, height)
    notes.extend(row_notes)
    frame, snap_notes = _snap_edges(frame, _ink_without_prose(ink, evidence, width, height))
    notes.extend(snap_notes)

    pad = max(FRAME_PAD_PIXELS, int(round(float(evidence.get("letter_height") or 10.0) * 0.3)))
    frame = [max(0.0, frame[0] - pad), max(0.0, frame[1] - pad),
             min(float(width), frame[2] + pad), min(float(height), frame[3] + pad)]
    if frame[2] - frame[0] < 4 or frame[3] - frame[1] < 4:
        result["notes"] = notes + ["校正后的配图区域过小，保留原图框供核对。"]
        return result
    area_ratio = ((frame[2] - frame[0]) * (frame[3] - frame[1])) / max(1.0, (start[2] - start[0]) * (start[3] - start[1]))
    if not FRAME_MIN_AREA_RATIO <= area_ratio <= 4.5:
        result["notes"] = notes + ["校正幅度超出安全范围，保留原图框供核对。"]
        return result
    if _ink_mass_bytes(evidence, frame) < 12:
        result["warnings"] = ["校正后的图框几乎没有内容，保留原图框，请核对裁剪位置。"]
        return result
    refined = [round(frame[0] * 1000 / width, 4), round(frame[1] * 1000 / height, 4),
               round(frame[2] * 1000 / width, 4), round(frame[3] * 1000 / height, 4)]
    if max(abs(refined[i] - original[i]) for i in range(4)) < 2:
        result["notes"] = notes
        return result
    result.update(bbox=refined, changed=True, method="raster_frame", notes=notes)
    return result


def _text_only_frame(evidence: dict, bbox) -> bool:
    """Recognize a crop dominated by paragraph ink, not a labelled diagram."""
    if any(_contains(bbox, component) for component in evidence["significant_components"]):
        return False
    total = _frame_ink(evidence, bbox)
    if total < 8:
        return False
    covered = 0
    for row in evidence["text_rows"]:
        intersection = (max(bbox[0], row[0]), max(bbox[1], row[1]),
                        min(bbox[2], row[2]), min(bbox[3], row[3]))
        if _area(intersection) > 0:
            covered += _frame_ink(evidence, intersection)
    return covered >= total * 0.90


def _complete_single_component_edge(evidence: dict, original, deadline: float) -> dict | None:
    """Complete only a small clipped edge with uniquely owned line ink.

    This does not identify letters or infer diagram meaning. Detached labels,
    multiple missing sides, paragraph ink and other graphic components cannot
    justify this fallback. Coordinates and pixels stay in the bounded analysis
    image already produced by the normal guard.
    """
    width, height = evidence["analysis_size"]

    def analysis_box(box, *, integral=False):
        values = (box[0] * width / 1000, box[1] * height / 1000,
                  box[2] * width / 1000, box[3] * height / 1000)
        return (math.floor(values[0]), math.floor(values[1]),
                math.ceil(values[2]), math.ceil(values[3])) if integral else values

    def component_ink(component, rectangle):
        left, top, right, bottom = rectangle
        total = 0
        for index, (x0, y, x1) in enumerate(component["runs"]):
            if index % 4096 == 0 and time.monotonic() > deadline:
                raise _EvidenceLimit("本地单侧补边达到时间上限")
            if top <= y < bottom:
                total += max(0, min(x1, right) - max(x0, left))
        return total

    original_analysis = analysis_box(original)
    candidates = []
    for component in evidence.get("line_components", ()):
        inside = component_ink(component, original_analysis)
        outside = component["pixels"] - inside
        if inside >= component["pixels"] * 0.98 and outside > max(8, component["pixels"] * 0.01):
            candidates.append((component, inside))
    if len(candidates) != 1:
        return None
    component, inside = candidates[0]
    left, top, right, bottom = component["bbox"]
    core = (left * 1000 / width, top * 1000 / height,
            right * 1000 / width, bottom * 1000 / height)
    missing = [index for index in range(4)
               if (original[index] > core[index] if index < 2 else original[index] < core[index])]
    if len(missing) != 1:
        return None
    side = missing[0]
    axis_size = width if side % 2 == 0 else height
    proposal = list(original)
    proposal[side] = core[side] + (-2 if side < 2 else 2) * 1000 / axis_size
    growth = abs(proposal[side] - original[side]) * axis_size / 1000
    if (_box(proposal) is None or growth > min(24.0, evidence["letter_height"])
            or _area(proposal) > _area(original) * 1.1):
        return None
    if any(_intersection(proposal, row) > 0 for row in evidence["text_rows"]):
        return None
    proposal_analysis = analysis_box(proposal)
    # Use actual runs: a distant page border's bounding box is not proof that
    # it intersects this illustration, while any other graphic ink is a veto.
    if any(other is not component and component_ink(other, proposal_analysis) > 0
           for other in evidence["significant_runs"]):
        return None
    before_pixels = analysis_box(original, integral=True)
    after_pixels = analysis_box(proposal, integral=True)
    added_ink = _frame_ink(evidence, proposal) - _frame_ink(evidence, original)
    owned_ink = component_ink(component, after_pixels) - component_ink(component, before_pixels)
    if added_ink <= 0 or owned_ink != added_ink:
        return None
    if time.monotonic() > deadline:
        raise _EvidenceLimit("本地单侧补边达到时间上限")
    return {
        "bbox": [round(value, 4) for value in proposal],
        "changed": True, "method": "bounded_component_completion", "status": "corrected",
        "evidence_bbox": [round(value, 4) for value in core],
        "component_completion": {
            "inside_ink_fraction": round(inside / component["pixels"], 6),
            "component_ink_pixels": component["pixels"],
            "added_ink_pixels": added_ink, "unowned_added_ink_pixels": 0,
            "side": ("left", "top", "right", "bottom")[side],
            "growth_analysis_pixels": round(growth, 4),
            "analysis_letter_height": evidence["letter_height"],
        },
    }




def guard_raster_figure_bbox(image_path: str, bbox) -> dict:
    """Correct an XY/YX swap only with unique complete local graphic evidence."""
    original = _box(bbox)
    result = {"bbox": list(original) if original else bbox, "model_bbox": list(original) if original else bbox,
              "changed": False, "warnings": [], "notes": [], "method": "unchanged",
              "status": "suspect"}
    if original is None:
        result["warnings"] = ["扫描配图坐标无效，请对照原页重新框选。"]
        return result
    deadline = time.monotonic() + MAX_SECONDS
    try:
        path = Path(image_path)
        stat = path.stat()
        evidence = _page_evidence(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except _EvidenceLimit as exc:
        result.update(status="unavailable", notes=[str(exc) + "；本地边界检查未执行，保留原视觉图框。"])
        return result
    except (OSError, ValueError, Image.DecompressionBombError):
        result["warnings"] = ["无法取得可靠的扫描图形边界证据，请对照原页核对裁剪。"]
        return result
    result.update(component_count=evidence["components"], figure_candidates=len(evidence["figures"]),
                  analysis_size=list(evidence["analysis_size"]))
    figures = evidence["figures"]
    if not figures:
        # Inapplicability is not evidence of an incorrect crop. Unlabelled
        # partitions, tables and photos can be valid illustrations. Only a
        # concrete blank crop or a visibly cut substantial component blocks it.
        if _frame_ink(evidence, original) < 8:
            result["warnings"] = ["图框内几乎没有可见内容，可能裁到了空白区域，请核对裁剪位置。"]
        elif _cuts_component(evidence, original):
            try:
                completed = _complete_single_component_edge(evidence, original, deadline)
            except _EvidenceLimit as exc:
                result.update(status="unavailable", notes=[str(exc) + "；本地边界检查未执行，保留原视觉图框。"])
                return result
            if completed:
                result.update(completed)
                result["notes"] = ["已按同一扫描线图的墨迹做有限补边，并保留原模型框。"]
            else:
                result["warnings"] = ["图框截入了未完整覆盖的图形区域，请核对是否裁掉边缘或混入邻图。"]
        elif _text_only_frame(evidence, original):
            result["warnings"] = ["图框内主要是连续正文，未见独立图形，请核对是否截到了题干或页脚。"]
        else:
            result.update(status="not_applicable", notes=[
                "本地独立线图检查不适用于该配图；保留视觉图框，未发现可据此判定的裁剪错误。"])
        return result
    swapped = (original[1], original[0], original[3], original[2])

    def coverage(frame, figure):
        return _intersection(frame, figure["bbox"]) / _area(figure["bbox"])

    def safe_frame(frame):
        return not any(_intersection(frame, row) > 0 for row in evidence["text_rows"])

    normal = [figure for figure in figures if coverage(original, figure) >= 0.80]
    target, proposal, method = None, original, "unchanged"
    if len(normal) == 1:
        target = normal[0]
        proposal = _union(original, target["bbox"])
        growth = max(abs(proposal[i] - original[i]) for i in range(4))
        if growth > 12:
            result["warnings"].append("扫描图形或标注超出原框的安全补边距离，请核对是否裁掉了图的一部分。")
        if _area(proposal) > _area(original) * 1.25:
            result["warnings"].append("完整覆盖图形需要明显扩大原框，请对照原页重新框选。")
        if not safe_frame(proposal):
            result["warnings"].append("图框与相邻正文或页脚相交，未自动扩大裁剪范围。")
        if not result["warnings"]:
            method = "bounded_padding" if proposal != original else "unchanged"
    elif len(figures) == 1 and evidence["large_components"] == 1:
        figure = figures[0]
        old_coverage, new_coverage = coverage(original, figure), coverage(swapped, figure)
        result["coverage_before"] = round(old_coverage, 4)
        result["coverage_if_swapped"] = round(new_coverage, 4)
        combined = _area(original) + _area(swapped) - _intersection(original, swapped)
        near_swap = _intersection(original, swapped) / max(1, combined) >= 0.15
        scale_x = (swapped[2] - swapped[0]) / (figure["bbox"][2] - figure["bbox"][0])
        scale_y = (swapped[3] - swapped[1]) / (figure["bbox"][3] - figure["bbox"][1])
        proposal = _union(swapped, figure["bbox"])
        if (old_coverage < 0.75 and new_coverage >= 0.95 and new_coverage - old_coverage >= 0.25
                and near_swap and 0.75 <= scale_x <= 1.5 and 0.75 <= scale_y <= 1.5
                and _area(proposal) <= _area(swapped) * 1.25 and safe_frame(proposal)):
            target, method = figure, "xy_swap"
        else:
            result["warnings"] = ["图框未完整覆盖独立线图，坐标交换或移动也缺少唯一充分证据，请对照原页核对。"]
            result["evidence_bbox"] = list(figure["bbox"])
    else:
        result["warnings"] = ["本页有多幅可能的线图，无法唯一确认当前图框，未自动移动裁剪位置。"]
    if target:
        result["evidence_bbox"] = [round(value, 4) for value in target["bbox"]]
        result["label_components"] = target["labels"]
        if any(other is not target and _intersection(proposal, other["bbox"]) > 0 for other in figures):
            result["warnings"].append("当前图框还切入另一幅独立图形，未自动确认或扩大裁剪。")
        if method == "xy_swap" and any(
            not _contains(target["bbox"], component)
            and _intersection(original, component) > 0
            and _intersection(proposal, component) < _intersection(original, component)
            for component in evidence["significant_components"]
        ):
            result["warnings"].append("原框中还有交换后会丢弃的大块墨迹，无法确认属于同一配图，未自动交换坐标。")
    if target and not result["warnings"]:
        result["bbox"] = [round(value, 4) for value in proposal]
        result["changed"] = proposal != original
        result["method"] = method
        result["status"] = "corrected" if result["changed"] else "verified"
        if method == "xy_swap":
            result["notes"].append("已由独立线图及邻近短标注证据纠正横纵坐标交换，并保留原模型框。")
        elif method == "bounded_padding":
            result["notes"].append("已按扫描线图与短标注做有限补边。")
    return result
