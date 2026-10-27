"""把题库里被误标的判断题改回 true_false（只改题型字段，不动题干与答案）。

判定口径与导入兜底一致（mathbank.question_types.looks_like_true_false）：
题干没有选项、原答案首行就是单一的“正确/错误（√/×）”。

用法（在项目根目录执行）：
  python scripts/migrate_true_false_questions.py           # 只列出将改动的题目
  python scripts/migrate_true_false_questions.py --apply   # 备份数据库后写入
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
from mathbank.question_types import looks_like_true_false  # noqa: E402


def _backup_database() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DATA_BACKUP_DIR / f"{DATABASE_FILE.name}.bak-{stamp}-truefalse"
    DATA_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DATABASE_FILE, target)
    return str(target)


def collect_changes(session) -> list[tuple[int, str, str]]:
    rows = []
    for q in session.query(Question).all():
        current = (q.question_type or "").strip()
        if current == "true_false":
            continue
        if looks_like_true_false(q.content or "", q.answer_markdown or ""):
            rows.append((q.id, current, (q.content or "").replace("\n", " ")[:60]))
    return rows


def run(apply: bool) -> int:
    session = SessionLocal()
    try:
        changes = collect_changes(session)
        if not changes:
            print("没有需要改判判断题的题目。")
            return 0
        print(f"命中 {len(changes)} 道题（当前题型 → true_false）：")
        for qid, current, snippet in changes:
            print(f"  #{qid}  {current or '(空)'} → true_false  | {snippet}")
        if not apply:
            print("\n这是预览。确认后加 --apply 执行修改（会先备份数据库）。")
            return 0

        backup = _backup_database()
        print(f"\n已备份数据库：{backup}")
        ids = [qid for qid, _, _ in changes]
        session.query(Question).filter(Question.id.in_(ids)).update(
            {Question.question_type: "true_false"}, synchronize_session=False
        )
        # 题干已改判题型，旧指纹（含题型）失效，删除后自动重建。
        session.query(QuestionFingerprint).filter(
            QuestionFingerprint.question_id.in_(ids)
        ).delete(synchronize_session=False)
        session.commit()
        print(f"已改判 {len(ids)} 道题为判断题。")
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
    parser = argparse.ArgumentParser(description="把误标的判断题改回 true_false")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    raise SystemExit(run(parser.parse_args().apply))
