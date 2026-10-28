"""Local PDF figure candidates, bounded crops, and exact source anchors.

All public rectangles use 0..1000 coordinates on the *visible, rotated* page.
Candidates are evidence for layout review, never a claim that a figure belongs
to a particular question. Files are owned and registered by the calling task.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any
import uuid


MAX_CANDIDATES = 80
MAX_DRAWINGS = 3000
MAX_TEXT_BLOCKS = 500
MAX_TEXT_CHARS = 30000
MAX_FIGURE_AREA_RATIO = 0.80
MAX_CROP_SIDE = 2400
MAX_CROP_PIXELS = 4_000_000
CROP_DPI = 200
# A model-drawn figure box can hug the text column while the illustration
# overhangs it by a millimetre or two. The raster is the only reliable
# evidence of that, so an edge that still carries ink is widened by these
# bounded steps until the drawing is no longer sliced.
EDGE_EXPANSION_STEP_PT = 2.0
MAX_EDGE_EXPANSION_PT = 8.0
EDGE_INK_THRESHOLD = 245
MAX_LABEL_SEGMENTS = 6000
MAX_LABEL_GEOMETRY_WORK = 20000
MAX_LABEL_ANCHORS = 24


def normalize_model_bbox(value) -> list:
    """Normalize named model coordinates or a legacy XYXY list, without guessing.

    Geometric validation belongs to the caller's existing checker. In
    particular, a legacy array is never silently interpreted as YXYX.
    """
    keys = ("left", "top", "right", "bottom")
    if isinstance(value, dict) and set(value) == set(keys):
        coordinates = [value[key] for key in keys]
    elif isinstance(value, list) and len(value) == 4:
        coordinates = list(value)
    else:
        raise ValueError("PDF 模型图框必须是 left/top/right/bottom 命名对象或兼容的四项坐标列表。")
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in coordinates):
        raise ValueError("PDF 模型图框的四个坐标必须是数值。")
    return coordinates


_MATH_ENVIRONMENTS = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*",
    "displaymath", "math", "aligned", "alignedat", "gathered", "split",
    "array", "cases", "matrix", "pmatrix", "bmatrix", "Bmatrix",
    "vmatrix", "Vmatrix", "smallmatrix",
}
_LITERAL_ENVIRONMENTS = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "tikzpicture"}
_ENV_TOKEN = re.compile(r"\\(begin|end)\s*\{([^{}\n]+)\}")
_CONTROL_WORD = re.compile(r"\\(?:[A-Za-z@]+\*?|[^A-Za-z\s])")
_LOCAL_IMAGE = re.compile(r"/static/(?:uploads|test_uploads)/(?:tmp/)?[A-Za-z0-9_-][A-Za-z0-9_.-]*\.png")
_HTML_START = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s|/?>)")
_HTML_TAG = re.compile(r"<(?:[^\"'<>]|\"[^\"]*\"|'[^']*')*>")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_MATH_MACRO_ARGUMENTS = {
    name: count
    for count, names in (
        (2, ("frac", "dfrac", "tfrac", "cfrac", "sfrac", "binom", "dbinom", "tbinom")),
        (1, ("sqrt", "vec", "overline", "underline", "widehat", "widetilde",
             "hat", "tilde", "bar", "dot", "ddot", "overrightarrow", "overleftarrow",
             "overleftrightarrow", "overbrace", "underbrace", "boldsymbol", "mathbf",
             "mathrm", "mathit", "mathsf", "mathbb", "mathcal", "mathfrak",
             "wideparen", "overparen", "overarc", "widearc")),
    )
    for name in names
}


def _page_rect(page):
    import pymupdf as fitz

    rect = fitz.Rect(page.rect)
    if not all(math.isfinite(value) for value in rect) or rect.is_empty or rect.is_infinite:
        raise ValueError("PDF 页面尺寸无效。")
    return rect


def _display_rect(page, rectangle):
    """PyMuPDF extraction is unrotated and already relative to the CropBox."""
    import pymupdf as fitz

    rect = fitz.Rect(rectangle)
    if not all(math.isfinite(value) for value in rect) or rect.is_empty or rect.is_infinite:
        return None
    rect = (rect * page.rotation_matrix) & _page_rect(page)
    return None if rect.is_empty or rect.is_infinite else rect


def _normalized(rect, page_rect) -> list[float]:
    return [
        round((rect.x0 - page_rect.x0) / page_rect.width * 1000, 4),
        round((rect.y0 - page_rect.y0) / page_rect.height * 1000, 4),
        round((rect.x1 - page_rect.x0) / page_rect.width * 1000, 4),
        round((rect.y1 - page_rect.y0) / page_rect.height * 1000, 4),
    ]


def scan_background_candidate_ids(page_info: dict) -> set[str]:
    """Reject native shortcuts for scan strips, without removing any candidate.

    A full-page raster background plus several nearly page-long raster strips
    covering most of the page is evidence of tiled scanning, not independent
    diagrams. A single tall illustration, ordinary small figures, and layouts
    without the full-page background evidence retain their native geometry.
    """
    if not isinstance(page_info, dict) or page_info.get("full_page_image") is not True:
        return set()
    candidates = page_info.get("candidates", [])
    if not isinstance(candidates, (list, tuple)):
        return set()
    strips = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("type") != "raster" or not isinstance(candidate.get("id"), str):
            continue
        box = candidate.get("bbox")
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not 0 <= value <= 1000 or not math.isfinite(value) for value in box)):
            continue
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            continue
        width, height = x1 - x0, y1 - y0
        if (height >= 950 and width >= 150) or (width >= 950 and height >= 150):
            strips.append((candidate["id"], tuple(box)))
    if len(strips) < 2:
        return set()
    # Sweep disjoint x bands and merge their y intervals. Summed image areas
    # would mistake overlapping/repeated images for full-page coverage.
    xs = sorted({value for _, box in strips for value in (box[0], box[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((box[1], box[3]) for _, box in strips if box[0] <= left and right <= box[2])
        start, end, height = None, None, 0.0
        for low, high in intervals:
            if start is None:
                start, end = low, high
            elif low <= end:
                end = max(end, high)
            else:
                height += end - start
                start, end = low, high
        if start is not None:
            height += end - start
        area += (right - left) * height
    return {identifier for identifier, _ in strips} if area >= 850_000 else set()


def _vector_label_segments(page, drawings, visible):
    """Read bounded visible stroke geometry once, without inferring labels."""
    import pymupdf as fitz

    segments = []
    for drawing in drawings:
        if drawing.get("type") not in {"s", "fs"} or drawing.get("stroke_opacity", 1) <= 0:
            continue
        for item in drawing.get("items", []):
            if item[0] == "l" and len(item) == 3:
                pairs, curved = [(item[1], item[2])], False
            elif item[0] == "c" and len(item) == 5:
                pairs, curved = [(item[1], item[4])], True
            elif item[0] == "re" and len(item) == 3:
                box = fitz.Rect(item[1])
                points = (box.tl, box.tr, box.br, box.bl)
                pairs, curved = list(zip(points, (*points[1:], points[0]))), False
            else:
                continue
            for start, end in pairs:
                first, second = fitz.Point(start) * page.rotation_matrix, fitz.Point(end) * page.rotation_matrix
                if (not all(math.isfinite(v) for v in (*first, *second))
                        or not visible.contains(first) or not visible.contains(second)
                        or math.hypot(second.x-first.x, second.y-first.y) < .5):
                    continue
                segments.append(((first.x, first.y), (second.x, second.y), curved))
                if len(segments) > MAX_LABEL_SEGMENTS:
                    return None
    return segments


def _candidate_label_geometry(candidate, segments, visible, work):
    """Only outer stroke junctions support detached-label ownership."""
    if segments is None:
        return None
    points = {}
    directional = 0
    curved = False
    for first, second, is_curve in segments:
        work[0] += 1
        if work[0] > MAX_LABEL_GEOMETRY_WORK:
            return None
        if any(not (candidate.x0-.25 <= point[0] <= candidate.x1+.25
                    and candidate.y0-.25 <= point[1] <= candidate.y1+.25) for point in (first, second)):
            continue
        if abs(first[0]-second[0]) > .5 and abs(first[1]-second[1]) > .5:
            directional += 1
        curved = curved or is_curve
        keys = [tuple(round(value/.25) for value in point) for point in (first, second)]
        signature = tuple(sorted(keys))
        for key, point in zip(keys, (first, second)):
            record = points.setdefault(key, {"point": point, "segments": set()})
            record["segments"].add(signature)
    if not curved and directional < 2:
        return {"complete": True, "anchors": []}  # Plain frames/grids are ambiguous.
    anchors = []
    for record in points.values():
        x, y = record["point"]
        if min(abs(x-candidate.x0), abs(x-candidate.x1), abs(y-candidate.y0), abs(y-candidate.y1)) > .75:
            continue
        lengths=[math.hypot((first[0]-second[0])*.25,(first[1]-second[1])*.25)
                 for first,second in record["segments"]]
        arrow=sum(length<=6 for length in lengths)>=2 and any(length>=12 for length in lengths)
        anchors.append({"point": [round((x-visible.x0)/visible.width*1000,4),
                                   round((y-visible.y0)/visible.height*1000,4)],
                        "degree": len(record["segments"]), "kind":"arrow" if arrow else "vertex"})
    anchors.sort(key=lambda value: (-value["degree"], value["point"][1], value["point"][0]))
    if len(anchors) > MAX_LABEL_ANCHORS:
        return None
    return {"complete": True, "anchors": anchors}


def _drawn_short_label_evidence(page, visible):
    """Exclude invisible/optional-layer text, without decoding unknown fonts."""
    try:
        spans=page.get_texttrace()
        if len(spans)>2000 or sum(len(span.get("chars",())) for span in spans)>MAX_TEXT_CHARS:
            return None
        output=[]
        for span in spans:
            if span.get("type") not in {0,1,2} or span.get("opacity",1)<=0 or span.get("layer"):
                continue
            chars=span.get("chars",())
            if len(chars)!=1 or type(chars[0][0]) is not int or not 0<=chars[0][0]<=0x10FFFF:
                continue
            text=chr(chars[0][0])
            if not re.fullmatch(r"[A-Za-z]",text):
                continue
            rectangle=_display_rect(page,span.get("bbox"))
            if rectangle is not None:
                output.append((text,_normalized(rectangle,visible)))
        return output
    except (ValueError,TypeError,KeyError,AttributeError,RuntimeError):
        return None


def inspect_pdf_page(page, page_index: int) -> dict[str, Any]:
    """Return visible raster occurrences, vector clusters and native text boxes.

    Reused image resources retain every displayed occurrence. A near-full-page
    image triggers visual review but is not advertised as an independent figure.
    Tables and vector diagrams deliberately remain candidates for the vision
    stage to distinguish. Truncation or extraction failures stay explicit.
    """
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise ValueError("PDF 页码无效。")
    rect = _page_rect(page)
    warnings: list[str] = []
    raw_candidates: list[tuple[str, Any]] = []
    full_page_image = False
    label_segments = None

    try:
        for item in page.get_image_info():
            image_rect = _display_rect(page, item["bbox"])
            if image_rect is None:
                continue
            if image_rect.get_area() / rect.get_area() >= MAX_FIGURE_AREA_RATIO:
                full_page_image = True
                continue
            if min(image_rect.width, image_rect.height) >= 3 and image_rect.get_area() >= 24:
                raw_candidates.append(("raster", image_rect))
    except Exception:
        warnings.append("原生图片位置提取不完整，请对照原页核对。")

    try:
        drawings = page.get_drawings()
        if len(drawings) > MAX_DRAWINGS:
            warnings.append("本页矢量对象过多，已跳过自动聚类，请使用原页核对插图。")
        else:
            # A page border otherwise connects every nearby diagram into one
            # huge cluster. Remove obvious frames before clustering, not after.
            usable = []
            for drawing in drawings:
                drawing_rect = _display_rect(page, drawing["rect"])
                if drawing_rect is not None and (
                    drawing_rect.width >= rect.width * 0.95
                    and drawing_rect.height >= rect.height * 0.95
                ):
                    continue
                usable.append(drawing)
            for cluster in page.cluster_drawings(drawings=usable):
                cluster_rect = _display_rect(page, cluster)
                if cluster_rect is None:
                    continue
                if min(cluster_rect.width, cluster_rect.height) < 4 or cluster_rect.get_area() < 36:
                    continue
                if cluster_rect.get_area() / rect.get_area() >= MAX_FIGURE_AREA_RATIO:
                    warnings.append("存在覆盖大部分页面的矢量结构，请核对其插图或表格范围。")
                    continue
                raw_candidates.append(("vector", cluster_rect))
            try:
                label_segments = _vector_label_segments(page, usable, rect)
            except (ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                label_segments = None  # Optional evidence does not fail inspection.
    except Exception:
        warnings.append("矢量插图位置提取不完整，请对照原页核对。")

    # Keep the order deterministic, without deduplicating reused resources or
    # promising that this physical top-to-bottom order is semantic reading order.
    raw_candidates.sort(key=lambda value: (value[1].y0, value[1].x0, value[0], value[1].y1, value[1].x1))
    if len(raw_candidates) > MAX_CANDIDATES:
        warnings.append(f"本页插图候选超过 {MAX_CANDIDATES} 个，未显示的区域仍需核对。")
    counts: dict[str, int] = {}
    candidates = []
    label_work = [0]
    for kind, candidate_rect in raw_candidates[:MAX_CANDIDATES]:
        counts[kind] = counts.get(kind, 0) + 1
        candidate = {
            "id": f"p{page_index + 1}_{kind}_{counts[kind]:03d}",
            "bbox": _normalized(candidate_rect, rect),
            "type": kind,
        }
        if kind == "vector":
            geometry = _candidate_label_geometry(candidate_rect, label_segments, rect, label_work)
            if geometry is not None:
                candidate["_label_geometry"] = geometry
        candidates.append(candidate)

    text_blocks = []
    text_chars = 0
    drawn_labels=_drawn_short_label_evidence(page,rect)
    try:
        import pymupdf as fitz

        flags = fitz.TEXTFLAGS_BLOCKS & ~fitz.TEXT_PRESERVE_IMAGES
        for block in page.get_text("blocks", flags=flags, sort=True):
            if len(block) < 7 or block[6] != 0 or not str(block[4]).strip():
                continue
            text_rect = _display_rect(page, block[:4])
            if text_rect is None:
                continue
            text = str(block[4])
            if len(text_blocks) >= MAX_TEXT_BLOCKS or text_chars + len(text) > MAX_TEXT_CHARS:
                warnings.append("本页文字位置清单已截断，原始正文仍由文字提取链路保留。")
                break
            text_chars += len(text)
            block_info={"bbox":_normalized(text_rect,rect),"text":text}
            if re.fullmatch(r"[A-Za-z]",text.strip()) and drawn_labels is not None:
                a,b,c,d=block_info["bbox"]
                block_info["_label_visible"]=any(value==text.strip() and a<=(x+z)/2<=c and b<=(y+end)/2<=d
                    and max(0,min(c,z)-max(a,x))*max(0,min(d,end)-max(b,y))>=.95*(z-x)*(end-y)
                    for value,(x,y,z,end) in drawn_labels)
            text_blocks.append(block_info)
    except Exception:
        warnings.append("本页文字坐标提取不完整，请对照原页核对。")

    if full_page_image:
        warnings.append("检测到接近整页的图片，须从原页识别独立插图，不能把整页当作配图。")
    return {
        "width": rect.width,
        "height": rect.height,
        "page_index": page_index,
        "candidates": candidates,
        "text_blocks": text_blocks,
        "needs_visual": bool(candidates or full_page_image or warnings),
        "full_page_image": full_page_image,
        "warnings": warnings,
    }


def _validated_bbox(bbox) -> tuple[float, float, float, float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("插图区域必须包含四个 0–1000 坐标。")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in bbox):
        raise ValueError("插图区域坐标必须是有限数字。")
    values = tuple(float(value) for value in bbox)
    if any(not math.isfinite(value) or value < 0 or value > 1000 for value in values):
        raise ValueError("插图区域越出页面，或包含非有限坐标。")
    x0, y0, x1, y1 = values
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("插图区域为空、过小或坐标次序不正确。")
    if (x1 - x0) * (y1 - y0) > MAX_FIGURE_AREA_RATIO * 1_000_000:
        raise ValueError("插图区域覆盖过多页面，请缩小到独立插图。")
    return values


def _complete_detached_vector_labels(page_info, original, current, candidate_ids):
    """Complete only uniquely owned short labels at actual stroke endpoints."""
    width, height = page_info.get("width"), page_info.get("height")
    if (page_info.get("warnings") or page_info.get("full_page_image")
            or any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0
                   for value in (width, height))):
        return current, []
    def box(value):
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or any(type(v) not in {int,float} or not math.isfinite(v) or not 0 <= v <= 1000 for v in value)):
            return None
        return tuple(value) if value[0]<value[2] and value[1]<value[3] else None
    def area(value):
        return (value[2]-value[0])*(value[3]-value[1])
    def overlap(first,second):
        return max(0,min(first[2],second[2])-max(first[0],second[0]))*max(0,min(first[3],second[3])-max(first[1],second[1]))
    def contains(first,second):
        return all((first[0]<=second[0]+.0002,first[1]<=second[1]+.0002,
                    first[2]+.0002>=second[2],first[3]+.0002>=second[3]))
    records=[]
    for candidate in page_info.get("candidates",[]):
        geometry=candidate.get("_label_geometry") if isinstance(candidate,dict) else None
        candidate_box=box(candidate.get("bbox")) if isinstance(candidate,dict) else None
        if (candidate_box is not None and candidate.get("type")=="vector" and isinstance(geometry,dict)
                and geometry.get("complete") is True and isinstance(geometry.get("anchors"),list)
                and len(geometry["anchors"])<=MAX_LABEL_ANCHORS):
            records.append((candidate,candidate_box,geometry["anchors"]))
    requested=list(dict.fromkeys(candidate_ids))
    targets=[record for record in records if record[0].get("id") in requested
             and overlap(original,record[1])/area(record[1])>=.75 and contains(current,record[1])]
    if not targets and not requested:
        targets=[record for record in records if overlap(original,record[1])/area(record[1])>=.75 and contains(current,record[1])]
    if len(targets)!=1:
        return current, []
    target,target_box,_anchors=targets[0]
    texts=[]
    visibility={}
    for block in page_info.get("text_blocks",[]):
        value=box(block.get("bbox")) if isinstance(block,dict) else None
        if value is not None:
            texts.append((value,str(block.get("text") or "").strip()))
            visibility[value]=block.get("_label_visible")
    proposals=[]
    warnings=[]
    for label_box,text in texts:
        if not re.fullmatch(r"[A-Za-z]",text) or contains(current,label_box):
            continue
        em=max((label_box[2]-label_box[0])*width/1000,(label_box[3]-label_box[1])*height/1000)
        if not 3<=em<=24:
            continue
        owners={}
        for candidate,candidate_box,anchors in records:
            for anchor in anchors:
                point=anchor.get("point") if isinstance(anchor,dict) else None
                degree=anchor.get("degree") if isinstance(anchor,dict) else None
                if (not isinstance(point,(list,tuple)) or len(point)!=2 or type(degree) is not int or degree<1
                        or any(type(v) not in {int,float} or not math.isfinite(v) or not 0<=v<=1000 for v in point)):
                    continue
                distance=math.hypot(max(label_box[0]-point[0],0,point[0]-label_box[2])*width/1000,
                                    max(label_box[1]-point[1],0,point[1]-label_box[3])*height/1000)
                if distance<=.7*em:
                    if anchor.get("kind")=="arrow" and text.lower() not in {"x","y","z"}:
                        degree=1  # A bare option letter is not an axis name.
                    owners[candidate["id"]]=max(owners.get(candidate["id"],0),degree)
        if target["id"] not in owners:
            continue
        if visibility.get(label_box) is False:
            continue
        if visibility.get(label_box) is not True:
            warnings.append(f"原生短标签“{text}”的可见绘制证据不完整，未自动扩框，请核对标注。")
            continue
        if set(owners)!={target["id"]} or owners[target["id"]]<2 or len(requested)!=1:
            warnings.append(f"图框附近的原生短标签“{text}”尚无唯一端点归属，未自动扩框，请核对标注。")
            continue
        proposals.append((label_box,text,em))
    allowed={value for value,_text,_em in proposals}
    repaired=0
    for label_box,text,em in proposals:
        margin_x,margin_y=300/width,300/height  # .3 pt only, not a whole new line.
        proposed=(max(0,min(current[0],label_box[0]-margin_x)),max(0,min(current[1],label_box[1]-margin_y)),
                  min(1000,max(current[2],label_box[2]+margin_x)),min(1000,max(current[3],label_box[3]+margin_y)))
        growth=(max(0,target_box[0]-proposed[0])*width/1000,max(0,target_box[1]-proposed[1])*height/1000,
                max(0,proposed[2]-target_box[2])*width/1000,max(0,proposed[3]-target_box[3])*height/1000)
        competition=any(candidate.get("id")!=target["id"] and box(candidate.get("bbox")) is not None
                        and overlap(proposed,box(candidate["bbox"]))>overlap(current,box(candidate["bbox"]))+1e-6
                        for candidate in page_info.get("candidates",[]) if isinstance(candidate,dict))
        prose=any(value not in allowed and overlap(proposed,value)>overlap(current,value)+1e-6 for value,_ in texts)
        if (max(growth)>1.3*em+.001 or area(proposed)>area(original)*2.2
                or area(proposed)>MAX_FIGURE_AREA_RATIO*1_000_000 or competition or prose):
            warnings.append(f"原生短标签“{text}”未完整进入图框，补全会越界、扩大过多或碰到邻文，请核对标注。")
            continue
        current=proposed
        repaired+=1
    if repaired:
        warnings.append(f"已补全 {repaired} 处由唯一矢量端点确认的分离原生标签。")
    return current,warnings


def refine_figure_bbox(page_info: dict, bbox, candidate_ids: list[str]) -> dict[str, Any]:
    """Repair small clipping errors using intersecting, local native evidence.

    This never searches nearby text or turns an incomplete half-figure into a
    complete candidate. Every coverage/growth test uses the *original* model
    box, so a sequence of individually small repairs cannot grow without bound.
    Invalid/unknown candidate references preserve that box for explicit review.
    """
    original = _validated_bbox(bbox)
    current = original
    warnings: list[str] = []

    def evidence_box(value):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in value):
            return None
        result = tuple(float(number) for number in value)
        if any(not math.isfinite(number) or not 0 <= number <= 1000 for number in result):
            return None
        return result if result[0] < result[2] and result[1] < result[3] else None

    def area(rect):
        return (rect[2] - rect[0]) * (rect[3] - rect[1])

    def overlap(first, second):
        return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
            0, min(first[3], second[3]) - max(first[1], second[1])
        )

    def union(first, second):
        return (min(first[0], second[0]), min(first[1], second[1]),
                max(first[2], second[2]), max(first[3], second[3]))

    def bounded(proposed):
        return (
            area(proposed) <= area(original) * 1.5 + 1e-6
            and area(proposed) <= MAX_FIGURE_AREA_RATIO * 1_000_000
            and proposed[2] - proposed[0] <= (original[2] - original[0]) * 1.4 + 1e-6
            and proposed[3] - proposed[1] <= (original[3] - original[1]) * 1.4 + 1e-6
        )

    if not isinstance(page_info, dict):
        raise ValueError("PDF 页面位置证据无效。")
    candidates = {}
    for candidate in page_info.get("candidates", []):
        if isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
            candidates.setdefault(candidate["id"], []).append(candidate)
    if not isinstance(candidate_ids, list) or any(not isinstance(identifier, str) for identifier in candidate_ids):
        return {"bbox": list(original), "warnings": ["插图候选标识无效，未自动扩大裁框。"]}

    # First validate all references. Padding must not accidentally make a tiny
    # half-covered or unknown candidate appear to pass a later coverage check.
    named_boxes = []
    invalid_reference = False
    for identifier in dict.fromkeys(candidate_ids):
        records = candidates.get(identifier, [])
        candidate_box = evidence_box(records[0].get("bbox")) if len(records) == 1 else None
        if candidate_box is None:
            warnings.append(f"候选 {identifier} 不存在、标识不唯一或坐标无效，未自动扩大裁框。")
            invalid_reference = True
        elif overlap(original, candidate_box) / area(candidate_box) < 0.75:
            warnings.append(f"裁框未覆盖候选 {identifier} 的 75%，不能自动补成完整插图。")
            invalid_reference = True
        else:
            named_boxes.append((identifier, candidate_box))
    if invalid_reference:
        return {"bbox": list(original), "warnings": warnings}

    texts = []
    option_label_boxes = set()
    for block in page_info.get("text_blocks", []):
        if not isinstance(block, dict):
            continue
        block_box = evidence_box(block.get("bbox"))
        text = str(block.get("text") or "").strip()
        if block_box is None or not text:
            continue
        # A./B./C./D. are native option markers outside an embedded image,
        # sometimes merged into one text block across two columns. They must
        # not be recruited as clipped diagram annotations such as vertex A.
        if re.fullmatch(r"(?:[A-H]\s*[.．、:：]\s*)+", text):
            option_label_boxes.add(tuple(block_box))
            texts.append((block_box, False))
            continue
        # Small labels may be Latin letters, dimensions, ticks, or short Chinese
        # chart labels. Recognizable question/prose fragments remain a barrier.
        prose = bool(
            len(text) > 24 or block_box[3] - block_box[1] > 80
            or re.search(r"[。！？；]|^\s*\d{1,3}[.．、]\s*\S", text)
            or len(re.findall(r"[\u4e00-\u9fff]", text)) > 4
            or re.match(r"(?:find|calculate|given|determine|prove|solve|show|choose|which)\b", text, re.IGNORECASE)
        )
        texts.append((block_box, prose))
        if prose and overlap(original, block_box) > 0:
            warnings.append("裁框与原生正文相交，未按正文范围扩大，请核对是否包含题干或遗漏图内说明。")

    def crosses_prose(proposed):
        return any(prose and overlap(proposed, block_box) > overlap(original, block_box) + 1e-6
                   for block_box, prose in texts)

    for identifier, candidate_box in named_boxes:
        proposed = union(current, candidate_box)
        if proposed == current:
            continue
        if not bounded(proposed):
            warnings.append(f"补全候选 {identifier} 超出有限扩张范围，未自动补全，请核对裁框。")
        elif crosses_prose(proposed):
            warnings.append(f"补全候选 {identifier} 会跨入原生正文，未自动补全，请核对裁框。")
        else:
            current = proposed
            warnings.append(f"已按原生候选 {identifier} 补全被裁去的小幅边缘。")

    label_repairs = 0
    for block_box, prose in texts:
        # Test against original, not current: do not recruit an adjacent label
        # merely because an earlier expansion brought it into the rectangle.
        if prose or tuple(block_box) in option_label_boxes or overlap(original, block_box) <= 0:
            continue
        proposed = union(current, block_box)
        if proposed == current:
            continue
        extra_x = max(0, original[0] - proposed[0], proposed[2] - original[2])
        extra_y = max(0, original[1] - proposed[1], proposed[3] - original[3])
        if (
            not bounded(proposed)
            or extra_x > min(32, (original[2] - original[0]) * 0.2)
            or extra_y > min(32, (original[3] - original[1]) * 0.2)
            or crosses_prose(proposed)
        ):
            warnings.append("相交的短标签超出局部补边范围或邻接正文，未自动扩大，请核对图内标注。")
        else:
            current = proposed
            label_repairs += 1
    if label_repairs:
        warnings.append(f"已补全 {label_repairs} 处与原裁框相交的原生短标签边界。")

    width, height = page_info.get("width"), page_info.get("height")
    valid_size = all(
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
        for value in (width, height)
    )
    if valid_size:
        margin_x, margin_y = 1500 / width, 1500 / height  # 1.5 PDF points
        padded = (max(0, current[0] - margin_x), max(0, current[1] - margin_y),
                  min(1000, current[2] + margin_x), min(1000, current[3] + margin_y))
        touches_other_text = any(overlap(original, block_box) <= 0 and overlap(padded, block_box) > 0
                                 for block_box, _ in texts)
        if bounded(padded) and not crosses_prose(padded) and not touches_other_text:
            current = padded
        else:
            warnings.append("小边距会超过扩张上限或碰到其他文字，已保留当前边界。")
    else:
        warnings.append("页面尺寸无效，未添加裁图边距。")
    current, detached_warnings = _complete_detached_vector_labels(page_info, original, current, candidate_ids)
    warnings.extend(detached_warnings)
    return {"bbox": [round(value, 4) for value in current], "warnings": list(dict.fromkeys(warnings))}


def _figure_label(text: str) -> bool:
    """Short in-figure lettering: vertex letters, dimensions, ticks, 甲乙地."""
    value = text.strip()
    if not value or len(value) > 12:
        return False
    if re.search(r"[。！？；，、：:]|[（(][^)）]{0,8}[)）]", value):
        return False
    return len(re.findall(r"[\u4e00-\u9fff]", value)) <= 4


def tighten_figure_bbox(page_info: dict, bbox, candidate_ids=None) -> dict[str, Any]:
    """Pull a loose model box back onto the native geometry of the figure.

    Vision models draw generous rectangles: they swallow the option row of the
    same question, the stem of the next question, a neighbouring column
    fragment or the dotted answer margin. The frame is therefore rebuilt from
    native evidence (declared candidates first, then candidates mostly inside
    the box), absorbing only the short lettering that belongs to the figure.
    Geometry is never overruled: without trustworthy evidence, or when the
    evidence disagrees with the box, the model rectangle is preserved.
    """
    original = _validated_bbox(bbox)
    if not isinstance(page_info, dict):
        return {"bbox": list(original), "warnings": []}
    width, height = page_info.get("width"), page_info.get("height")
    sized = all(
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
        for value in (width, height)
    )
    margin_x = 1500 / width if sized else 1.5
    margin_y = 1500 / height if sized else 1.5

    def evidence_box(value):
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in value):
            return None
        result = tuple(float(number) for number in value)
        if any(not math.isfinite(number) or not 0 <= number <= 1000 for number in result):
            return None
        return result if result[0] < result[2] and result[1] < result[3] else None

    def area(rect):
        return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])

    def overlap(first, second):
        return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
            0.0, min(first[3], second[3]) - max(first[1], second[1]))

    def union(first, second):
        return (min(first[0], second[0]), min(first[1], second[1]),
                max(first[2], second[2]), max(first[3], second[3]))

    candidates = []
    background = scan_background_candidate_ids(page_info)
    for candidate in page_info.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("id") or "") in background:
            continue  # 扫描拼接条不是独立图形，不能拿来当外框证据
        candidate_box = evidence_box(candidate.get("bbox"))
        if candidate_box is not None:
            candidates.append((str(candidate.get("id") or ""), candidate_box))
    if not candidates:
        return {"bbox": list(original), "warnings": []}

    declared = {value for value in candidate_ids or [] if isinstance(value, str)} if isinstance(
        candidate_ids, list) else set()
    frame = None
    for identifier, candidate_box in candidates:
        if identifier and identifier in declared and overlap(original, candidate_box) >= 0.2 * area(candidate_box):
            frame = candidate_box if frame is None else union(frame, candidate_box)
    if frame is None:
        # No declared evidence: only a region that mostly sits inside the box
        # can be trusted as its content, otherwise a neighbour would shrink it.
        for _, candidate_box in candidates:
            if (overlap(original, candidate_box) >= 0.6 * area(candidate_box)
                    and area(candidate_box) >= 0.02 * area(original)):
                frame = candidate_box if frame is None else union(frame, candidate_box)
    if frame is None:
        return {"bbox": list(original), "warnings": []}

    # A diagram split into several clusters must not lose the pieces that touch
    # the frame it already owns; growth stays bounded below.
    for _ in range(4):
        grown = frame
        for _, candidate_box in candidates:
            if overlap(frame, candidate_box) >= 0.5 * area(candidate_box):
                grown = union(grown, candidate_box)
        if grown == frame:
            break
        frame = grown

    def too_large(rect):
        return (area(rect) > area(original) * 1.5 + 1e-6
                or rect[2] - rect[0] > (original[2] - original[0]) * 1.4 + 1e-6
                or rect[3] - rect[1] > (original[3] - original[1]) * 1.4 + 1e-6)

    if too_large(frame):
        return {"bbox": list(original),
                "warnings": ["原生图形范围与模型图框差异过大，未自动收缩，请核对裁框。"]}

    grown = frame
    for block in page_info.get("text_blocks") or []:
        if not isinstance(block, dict):
            continue
        block_box = evidence_box(block.get("bbox"))
        if block_box is None or not _figure_label(str(block.get("text") or "")):
            continue
        center_x = (block_box[0] + block_box[2]) / 2
        center_y = (block_box[1] + block_box[3]) / 2
        if (frame[0] - 4 * margin_x <= center_x <= frame[2] + 4 * margin_x
                and frame[1] - 4 * margin_y <= center_y <= frame[3] + 4 * margin_y):
            grown = union(grown, block_box)

    if too_large(grown):
        grown = frame  # lettering beyond the frame must not blow the crop up

    if grown == original:
        return {"bbox": list(original), "warnings": []}
    if area(grown) < 0.15 * area(original):
        return {"bbox": list(original), "warnings": ["收缩后的图形区域过小，已保留原图框供核对。"]}
    note = ("已把配图边界扩展到原生图形范围。" if area(grown) > area(original) + 1e-6
            else "已收缩配图边界，只保留图形本体。")
    return {"bbox": [round(value, 4) for value in grown], "warnings": [note]}


PAGE_FURNITURE_TITLE = re.compile(
    r"核心素养|单元\s*(?:测评|检测)|期中|期末|测评卷|检测卷|质量检测|满分|分值|答题时间|考试时间")
PAGE_FURNITURE_SCORE = (re.compile(r"题\s*号"), re.compile(r"得\s*分|总\s*分"))
# A numbered line that starts a question: "1." / "1、" / "（一）" / "一、".
QUESTION_ANCHOR = re.compile(r"^(?:[（(]?[一二三四五六七八九十]{1,3}[)）]?[、.．]|\d{1,2}[.．、])\s*\S")


def page_furniture_reason(page_info: dict, bbox) -> str:
    """Explain why a region is page furniture rather than a question figure.

    Vision models regularly mistake the running head, the unit banner or the
    score grid for an illustration. Those regions all sit above the first
    question of the page, so a box that ends above every question anchor is
    furniture with no ambiguity. Text-based rules cover banners that carry
    their own wording. Any doubt returns "" and leaves the region untouched.
    """
    box = _validated_bbox(bbox)
    x0, y0, x1, y1 = box
    width, height = page_info.get("width"), page_info.get("height")
    if not all(
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0
        for value in (width, height)
    ):
        return ""

    def overlap(first, second):
        return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
            0, min(first[3], second[3]) - max(first[1], second[1]))

    inside, anchors = [], []
    for block in page_info.get("text_blocks") or []:
        if not isinstance(block, dict):
            continue
        raw = block.get("bbox")
        text = str(block.get("text") or "").strip()
        try:
            block_box = _validated_bbox(raw)
        except ValueError:
            continue
        if overlap(box, block_box) > 0.6 * max(1e-6, (block_box[2] - block_box[0]) * (block_box[3] - block_box[1])):
            if text:
                inside.append(text)
        if text and QUESTION_ANCHOR.match(text) and overlap(box, block_box) <= 0:
            anchors.append(block_box)

    joined = "".join(inside)
    if inside and PAGE_FURNITURE_SCORE[0].search(joined) and PAGE_FURNITURE_SCORE[1].search(joined):
        return "得分统计表"
    if inside and PAGE_FURNITURE_TITLE.search(joined) and x1 - x0 >= 250:
        return "卷头标题横幅"
    if anchors and y1 <= min(anchor[1] for anchor in anchors) + 4:
        return "位于本页第一道题上方"
    if y0 <= 2 and y1 <= 180 and x1 - x0 >= 300 and y1 - y0 <= 90 and (x1 - x0) / max(1e-6, y1 - y0) >= 2.5:
        return "页眉通栏"
    return ""


def _render_crop_pixmap(page, clip, fitz):
    """Render one crop rectangle at the bounded crop resolution."""
    scale = min(
        CROP_DPI / 72,
        (MAX_CROP_SIDE - 2) / clip.width,
        (MAX_CROP_SIDE - 2) / clip.height,
        math.sqrt((MAX_CROP_PIXELS - 4 * MAX_CROP_SIDE) / clip.get_area()),
    )
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, colorspace=fitz.csRGB, alpha=False)
    if min(pixmap.width, pixmap.height) < 2 or max(pixmap.width, pixmap.height) > MAX_CROP_SIDE or pixmap.width * pixmap.height > MAX_CROP_PIXELS:
        raise ValueError("插图渲染尺寸无效或超过安全上限。")
    return pixmap


def _edge_ink_sides(pixmap) -> set:
    """Report crop edges whose outermost pixel line still carries ink.

    Geometry alone cannot tell a complete drawing from a sliced one; the
    raster can. Only the border lines are inspected, so the cost is one line
    per side regardless of crop size.
    """
    width, height, stride, components = pixmap.width, pixmap.height, pixmap.stride, pixmap.n
    samples = pixmap.samples
    if width < 2 or height < 2 or not samples:
        return set()

    def ink(offset):
        return samples[offset] < EDGE_INK_THRESHOLD

    sides = set()
    if any(ink(row * stride) for row in range(height)):
        sides.add("left")
    if any(ink(row * stride + (width - 1) * components) for row in range(height)):
        sides.add("right")
    if any(ink(column * components) for column in range(width)):
        sides.add("top")
    if any(ink((height - 1) * stride + column * components) for column in range(width)):
        sides.add("bottom")
    return sides


def _widen_clip_until_ink_clears(page, clip, page_rect, pixmap, fitz):
    """Give a sliced illustration back its overhang, without recruiting prose.

    Each edge that still touches ink is widened by one bounded step. A step is
    refused when it would leave the page or when the added strip reaches a text
    block, so a neighbouring column's text can never be pulled into a figure.
    Rotated pages are left untouched: their text rectangles and the rendered
    clip live in different coordinate spaces.
    """
    current = fitz.Rect(clip)
    if getattr(page, "rotation", 0):
        return current
    try:
        text_rects = [
            fitz.Rect(block[:4]) for block in page.get_text("blocks")
            if len(block) >= 4 and str(block[4]).strip()
        ]
    except Exception:
        return current

    expanded = 0.0
    while expanded < MAX_EDGE_EXPANSION_PT:
        sides = _edge_ink_sides(pixmap)
        if not sides:
            break
        step = min(EDGE_EXPANSION_STEP_PT, MAX_EDGE_EXPANSION_PT - expanded)
        changed = False
        for side in ("left", "top", "right", "bottom"):
            if side not in sides:
                continue
            candidate = fitz.Rect(current)
            if side == "left":
                candidate.x0 -= step
                band = fitz.Rect(candidate.x0, current.y0, current.x0, current.y1)
            elif side == "right":
                candidate.x1 += step
                band = fitz.Rect(current.x1, current.y0, candidate.x1, current.y1)
            elif side == "top":
                candidate.y0 -= step
                band = fitz.Rect(current.x0, candidate.y0, current.x1, current.y0)
            else:
                candidate.y1 += step
                band = fitz.Rect(current.x0, current.y1, current.x1, candidate.y1)
            if (candidate.x0 < page_rect.x0 or candidate.y0 < page_rect.y0
                    or candidate.x1 > page_rect.x1 or candidate.y1 > page_rect.y1):
                continue
            if any(band.intersects(rect) for rect in text_rects):
                continue
            current = candidate
            changed = True
        if not changed:
            break
        expanded += step
        pixmap = _render_crop_pixmap(page, current, fitz)
    return current


def crop_pdf_figure(
    page,
    bbox,
    output_dir: Path,
    url_prefix: str,
    asset_prefix: str,
) -> str:
    """Render the visible figure region, including text, to a bounded PNG.

    The caller supplies an absolute trusted output directory and registers the
    returned URL for cleanup immediately. No paths are accepted from the model.
    """
    import pymupdf as fitz

    x0, y0, x1, y1 = _validated_bbox(bbox)
    page_rect = _page_rect(page)
    output_dir = Path(output_dir)
    if not output_dir.is_absolute() or output_dir.is_symlink():
        raise ValueError("插图输出目录必须是可信的绝对目录。")
    if not isinstance(asset_prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", asset_prefix):
        raise ValueError("插图文件名前缀无效。")
    if not isinstance(url_prefix, str) or not re.fullmatch(r"/static/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?", url_prefix):
        raise ValueError("插图 URL 前缀必须属于本地静态资源目录。")
    clip = fitz.Rect(
        page_rect.x0 + x0 / 1000 * page_rect.width,
        page_rect.y0 + y0 / 1000 * page_rect.height,
        page_rect.x0 + x1 / 1000 * page_rect.width,
        page_rect.y0 + y1 / 1000 * page_rect.height,
    )
    # get_pixmap's display-list clip follows the rotated visible page, unlike
    # extraction boxes. Do not derotate this rectangle a second time.
    pixmap = _render_crop_pixmap(page, clip, fitz)
    widened = _widen_clip_until_ink_clears(page, clip, page_rect, pixmap, fitz)
    if widened != clip:
        clip = widened
        pixmap = _render_crop_pixmap(page, clip, fitz)
    png = pixmap.tobytes("png")
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{asset_prefix}_{uuid.uuid4().hex}.png"
    destination = output_dir / filename
    created = False
    try:
        with destination.open("xb") as handle:
            created = True
            handle.write(png)
    except Exception:
        if created and destination.exists() and not destination.is_symlink():
            destination.unlink(missing_ok=True)
        raise
    return f"{url_prefix.rstrip('/')}/{filename}"


def _escaped(text: str, index: int) -> bool:
    count = 0
    while index > 0 and text[index - 1] == "\\":
        count += 1
        index -= 1
    return bool(count % 2)


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(text)):
        if _escaped(text, index):
            continue
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text) + 1


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Protect syntax interiors while leaving choices/table cell text usable."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        end = None
        # Fences, inline code and unclosed code are treated conservatively.
        if text[cursor] in "`~" and not _escaped(text, cursor):
            run = re.match(r"(`+|~+)", text[cursor:]).group(0)
            line_start = text.rfind("\n", 0, cursor) + 1
            fence = len(run) >= 3 and not text[line_start:cursor].strip() and cursor - line_start <= 3
            if fence:
                pattern = re.compile(r"^[ \t]{0,3}" + re.escape(run[0]) + "{" + str(len(run)) + r",}[ \t]*(?:\n|$)", re.MULTILINE)
                first_newline = text.find("\n", cursor)
                match = pattern.search(text, first_newline + 1) if first_newline >= 0 else None
                end = match.end() if match else len(text) + 1
            elif run[0] == "`":
                pattern = re.compile(r"(?<!`)" + re.escape(run) + r"(?!`)")
                match = pattern.search(text, cursor + len(run))
                end = match.end() if match else len(text) + 1
        if end is None and text.startswith("![", cursor) and not _escaped(text, cursor):
            alt_end = _balanced_end(text, cursor + 1, "[", "]")
            end = alt_end
            if alt_end < len(text) and text[alt_end] in "([":
                opening = text[alt_end]
                end = _balanced_end(text, alt_end, opening, ")" if opening == "(" else "]")
        if end is None and text.startswith("<!--", cursor):
            close = text.find("-->", cursor + 4)
            end = close + 3 if close >= 0 else len(text) + 1
        if end is None and text[cursor] == "<" and _HTML_START.match(text, cursor):
            match = _HTML_TAG.match(text, cursor)
            end = match.end() if match else len(text) + 1
        if end is None and not _escaped(text, cursor):
            delimiter = next((value for value in ("$$", "$", r"\(", r"\[") if text.startswith(value, cursor)), None)
            if delimiter:
                closing = {r"\(": r"\)", r"\[": r"\]"}.get(delimiter, delimiter)
                close = text.find(closing, cursor + len(delimiter))
                while close >= 0 and _escaped(text, close):
                    close = text.find(closing, close + len(closing))
                end = close + len(closing) if close >= 0 else len(text) + 1
        if end is None and text[cursor] == "\\" and not _escaped(text, cursor):
            environment = _ENV_TOKEN.match(text, cursor)
            if environment:
                name = environment.group(2)
                end = environment.end()
                if environment.group(1) == "begin" and name in _MATH_ENVIRONMENTS | _LITERAL_ENVIRONMENTS:
                    depth = 1
                    end = len(text) + 1
                    for token in _ENV_TOKEN.finditer(text, environment.end()):
                        if token.group(2) != name or _escaped(text, token.start()):
                            continue
                        depth += 1 if token.group(1) == "begin" else -1
                        if depth == 0:
                            end = token.end()
                            break
                elif environment.group(1) == "begin":
                    # A table's column specification is syntax; its later cell
                    # bodies are normal anchorable source, including multicolumn.
                    probe = end
                    while probe < len(text) and text[probe].isspace():
                        probe += 1
                    while probe < len(text) and text[probe] in "[{":
                        opening = text[probe]
                        probe = _balanced_end(text, probe, opening, "]" if opening == "[" else "}")
                        end = probe
                        while probe < len(text) and text[probe].isspace():
                            probe += 1
            else:
                command = _CONTROL_WORD.match(text, cursor)
                if command:
                    end = command.end()
                    math_arguments = _MATH_MACRO_ARGUMENTS.get(command.group(0)[1:])
                    if math_arguments:
                        # Even imperfect native text may contain bare formula
                        # macros. Preserve their arguments without blocking
                        # ordinary text-bearing commands such as multicolumn.
                        probe = end
                        while probe < len(text) and text[probe].isspace():
                            probe += 1
                        if probe < len(text) and text[probe] == "[":
                            probe = _balanced_end(text, probe, "[", "]")
                            end = probe
                        for _ in range(math_arguments):
                            while probe < len(text) and text[probe].isspace():
                                probe += 1
                            if probe >= len(text) or text[probe] != "{":
                                break
                            probe = _balanced_end(text, probe, "{", "}")
                            end = probe
                    elif command.group(0) in (r"\verb", r"\verb*") and end < len(text):
                        close = text.find(text[end], end + 1)
                        end = close + 1 if close >= 0 else len(text) + 1
                    elif command.group(0) == r"\includegraphics":
                        probe = end
                        while probe < len(text) and text[probe].isspace():
                            probe += 1
                        if probe < len(text) and text[probe] == "[":
                            probe = _balanced_end(text, probe, "[", "]")
                        while probe < len(text) and text[probe].isspace():
                            probe += 1
                        if probe < len(text) and text[probe] == "{":
                            end = _balanced_end(text, probe, "{", "}")
        if end is not None:
            spans.append((cursor, end))
            cursor = end
        else:
            cursor += 1
    return spans


def _occurrences(text: str, fragment: str):
    start = 0
    while True:
        position = text.find(fragment, start)
        if position < 0:
            return
        yield position
        start = position + 1


def _anchor_positions(text: str, before: str, after: str) -> list[int]:
    if not before:
        positions = []
        for position in _occurrences(text, after):
            positions.append(position)
            if len(positions) == 2:
                break
        return positions
    if not after:
        positions = []
        for position in _occurrences(text, before):
            positions.append(position + len(before))
            if len(positions) == 2:
                break
        return positions
    positions = []
    for start in _occurrences(text, before):
        position = start + len(before)
        gap_end = position
        while gap_end < len(text) and text[gap_end].isspace():
            gap_end += 1
        # Whitespace inside an exact after-fragment remains significant too.
        after_start = text.find(after, position)
        if position <= after_start <= gap_end:
            positions.append(position)
            if len(positions) == 2:
                break
    return positions


def _inside_markdown_table(text: str, position: int) -> bool:
    """A new line inside a pipe-table cell would silently destroy its row."""
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end < 0:
        line_end = len(text)
    if "|" not in text[line_start:line_end]:
        return False
    # The separator may be above this row or below the header. Blank/non-table
    # lines end the block, preventing a distant table from changing an anchor.
    block_start, block_end = line_start, line_end
    while block_start > 0:
        previous_start = text.rfind("\n", 0, block_start - 1) + 1
        if "|" not in text[previous_start:block_start - 1]:
            break
        block_start = previous_start
    while block_end < len(text):
        next_end = text.find("\n", block_end + 1)
        if next_end < 0:
            next_end = len(text)
        if "|" not in text[block_end + 1:next_end]:
            break
        block_end = next_end
    return any(_TABLE_SEPARATOR.fullmatch(line) for line in text[block_start:block_end].splitlines())


def apply_figure_anchors(markdown: str, figures: list[dict]) -> dict[str, Any]:
    """Insert figure references only at unique, exact, syntactically safe anchors.

    Resolve all anchors against the unchanged source so earlier insertions do
    not invalidate later ones. Preserve multiple displays at a shared anchor in
    input order. Review flags and model confidence cannot bypass validation.
    """
    if not isinstance(markdown, str) or not isinstance(figures, list):
        raise ValueError("插图归位需要原始正文和插图数组。")
    protected = _protected_spans(markdown)
    insertions: dict[int, list[str]] = {}
    attached: list[str] = []
    unmatched: list[dict] = []
    for figure in figures:
        reason = ""
        if not isinstance(figure, dict):
            unmatched.append({"reason": "插图信息不是对象。"})
            continue
        before = figure.get("anchor_before", "")
        after = figure.get("anchor_after", "")
        identifier = figure.get("id")
        image_path = figure.get("image_path")
        position = None
        if not isinstance(identifier, str) or not identifier.strip():
            reason = "插图缺少稳定标识。"
        elif not isinstance(image_path, str) or not _LOCAL_IMAGE.fullmatch(image_path):
            reason = "插图路径不是已生成的本地 PNG 资源。"
        elif not isinstance(before, str) or not isinstance(after, str) or not (before.strip() or after.strip()):
            reason = "插图缺少非空的原文位置片段。"
        else:
            positions = _anchor_positions(markdown, before, after)
            if not positions:
                reason = "原文片段未精确匹配，或两个片段之间并非只有空白。"
            elif len(positions) != 1:
                reason = "原文片段对应多个位置，无法唯一归位。"
            else:
                position = positions[0]
                if any(start < position < end for start, end in protected):
                    reason = "插入点位于公式、代码、标签、控制词或已有图片内部。"
        if reason:
            unmatched.append({**figure, "reason": reason})
            continue
        spacing = " " if _inside_markdown_table(markdown, position) else "\n"
        insertions.setdefault(position, []).append(f"{spacing}![插图]({image_path}){spacing}")
        attached.append(identifier)

    result = markdown
    for position in sorted(insertions, reverse=True):
        result = result[:position] + "".join(insertions[position]) + result[position:]
    return {"markdown": result, "attached": attached, "unmatched": unmatched}
