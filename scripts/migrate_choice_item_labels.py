"""清理选择题 choices 环境里重复的原卷标号（`\\item A. 3元` → `\\item 3元`）。

前端卡片、A4 预览和 LaTeX/Word 导出都会按顺序自动生成 A./B./C./D.，库里
再保留一份原卷标号就会显示成「A. A. 3元」。判定与导入链路共用
`mathbank.tex_helper.strip_choice_item_labels`。

用法（在项目根目录执行）：
  python scripts/migrate_choice_item_labels.py           # 只列出将改动的题目
  python scripts/migrate_choice_item_labels.py --apply   # 备份数据库后写入
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mathbank.database import Question, QuestionFingerprint, SessionLocal  # noqa: E402
from mathbank.paths import DATABASE_FILE, DATA_BACKUP_DIR  # noqa: E402
from mathbank.tex_helper import strip_choice_item_labels  # noqa: E402


def _backup_database() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DATA_BACKUP_DIR / f"{DATABASE_FILE.name}.bak-{stamp}-choicelabels"
    DATA_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DATABASE_FILE, target)
    return str(target)


def collect_changes(session) -> list[tuple[int, str, str]]:
    rows = []
    for question in session.query(Question).all():
        content = question.content or ""
        if "\\begin{choices}" not in content:
            continue
        cleaned = strip_choice_item_labels(content)
        if cleaned != content:
            rows.append((question.id, content, cleaned))
    return rows


def _preview(content: str) -> str:
    first_item = next(
        (line.strip() for line in content.replace("\r\n", "\n").split("\n") if "\\item" in line),
        "",
    )
    return first_item[:70]


def run(apply: bool) -> int:
    session = SessionLocal()
    try:
        changes = collect_changes(session)
        if not changes:
            print("没有需要清理选项标号的题目。")
            return 0
        print(f"命中 {len(changes)} 道题（删除 \\item 后重复的 A./B. 标号）：")
        for qid, before, _after in changes:
            print(f"  #{qid}  {_preview(before)}")
        if not apply:
            print("\n这是预览。确认后加 --apply 执行修改（会先备份数据库）。")
            return 0

        backup = _backup_database()
        print(f"\n已备份数据库：{backup}")
        ids = []
        for qid, _before, after in changes:
            session.query(Question).filter(Question.id == qid).update(
                {Question.content: after}, synchronize_session=False
            )
            ids.append(qid)
        # 题干内容变了，旧指纹（含内容摘要）失效，删除后按新内容重建。
        session.query(QuestionFingerprint).filter(
            QuestionFingerprint.question_id.in_(ids)
        ).delete(synchronize_session=False)
        session.commit()
        print(f"已清理 {len(ids)} 道题的选项标号。")
        try:
            from mathbank.sync_helper import export_database_to_files

            export_database_to_files()
            print("data_backup 镜像已同步。")
        except Exception as exc:  # 镜像失败不影响主库，提示人工重跑即可
            print(f"镜像同步失败（不影响主库）：{exc}")
        return 0
    except Exception as exc:
        session.rollback()
        print(f"处理失败，已回滚：{exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理选择题选项里重复的原卷标号")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    raise SystemExit(run(parser.parse_args().apply))
