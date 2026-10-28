# -*- coding: utf-8 -*-
"""Generate the primary-school knowledge-point catalogue from curriculum A.

One stable point per textbook unit (chapter level), each path bound to the
exact chapter string of the installed curriculum, so AI suggestions can only
land on real books/chapters.
"""
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
curriculum = json.loads((ROOT / "mathbank" / "resources" / "curriculums" / "A.json").read_text(encoding="utf-8"))

UNIT_NUM_RE = re.compile(r"^[一二三四五六七八九十]+\s+")
GRADES = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def clean_name(chapter: str) -> str:
    name = chapter
    if name.startswith("☆ "):
        name = name[2:]
    name = UNIT_NUM_RE.sub("", name)
    return name.strip()


def aliases_for(name: str) -> list[str]:
    aliases = []
    if "(" in name or ")" in name:
        wide = name.replace("(", "（").replace(")", "）")
        if wide != name:
            aliases.append(wide)
    if "数学广角——" in name:
        tail = name.split("数学广角——", 1)[1]
        if tail and tail not in aliases:
            aliases.append(tail)
    return aliases


points = []
cleaned_names = Counter()
entries = []
for book, chapters in curriculum.items():
    grade = GRADES[book[0]]
    volume = {"上": "a", "下": "b"}[book[3]]
    code_prefix = f"local:g{grade}{volume}"
    for index, chapter in enumerate(chapters, start=1):
        name = clean_name(chapter)
        entries.append({
            "book": book, "chapter": chapter, "name": name, "index": index,
            "code": f"{code_prefix}-u{index}",
        })
        cleaned_names[name] += 1

for entry in entries:
    name = entry["name"]
    if cleaned_names[name] > 1:
        name = f"{name}（{entry['book']}）"
    aliases = aliases_for(entry["name"])
    points.append({
        "code": entry["code"],
        "name": name,
        "original_name": name,
        "source": "local",
        "aliases": aliases,
        "curriculum_paths": [{
            "version_code": "A",
            "compulsory": entry["book"],
            "chapter": entry["chapter"],
            "knowledge": "",
            "reference_section": entry["chapter"],
            "level": "chapter",
        }],
    })

payload = {
    "schema_version": "mathbank-knowledge-points-v1",
    "source": "local-curriculum-A",
    "provenance": {
        "note": "小学人教版（A 版）12 册单元级知识点目录，由教材大纲章节生成；"
                "原高中平台目录（zhixue 773 条）已于 2026-10-28 整体退役，备份见 data_backup/。",
        "retired": "data_backup/knowledge_points.json.bak-20261028-highschool",
        "generated_at": "2026-10-28",
    },
    "counts": {
        "points": len(points),
        "platform_points": 0,
        "local_points": len(points),
        "paths_by_version": {"A": len(points)},
    },
    "source_code_aliases": {},
    "curriculum_hierarchies": {},
    "points": points,
}

out = ROOT / "mathbank" / "resources" / "knowledge_points.json"
out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("points:", len(points))
dup = [n for n, c in cleaned_names.items() if c > 1]
print("disambiguated:", dup)
