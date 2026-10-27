# -*- coding: utf-8 -*-
"""MathBank 覆盖升级：新版文件 + 保留本地小学大纲 + 保留数据。"""
import os, shutil, sys, json, io, zipfile, time

OLD = r'C:/Users/EDY/WorkBuddy/2026-10-19-16-05-43/math-question-bank'
NEW = r'C:/Users/EDY/WorkBuddy/2026-10-19-16-05-43/_upgrade_tmp'

# 受保护的路径（发布清单 + 本地数据）
PROTECTED = {
    '.env', '.system_generated', 'data_backup', 'venv',
    'math_question_bank.db', 'math_question_bank.db-wal', 'math_question_bank.db-shm',
    'static/uploads', '.git', '.gitignore', '.gitattributes', '.pytest_cache',
    '__pycache__', 'start_mathbank.bat', '.env.example',
}

# 本地教材大纲：按用户决定保留本地版本（小学二年级）
KEEP_LOCAL = {
    'mathbank/resources/curriculums/A.json',
    'mathbank/resources/curriculums/B.json',
    'mathbank/resources/curriculums/H.json',
    'mathbank/resources/curriculums/S.json',
}

# 本地新增、新版没有的文件，需保留
LOCAL_ONLY = {
    'static/CC教研工作台.ico',
}

# 本地品牌/自定义图标：新版会被我们的图标替换
KEEP_ICONS = {
    'static/favicon.svg', 'static/favicon.png', 'static/favicon.ico',
    'static/apple-touch-icon.png',
}


def copy_tree(src_root, dst_root, rel='', stats=None):
    if stats is None:
        stats = {'copied': 0, 'skipped': 0, 'kept': 0}
    src_dir = os.path.join(src_root, rel) if rel else src_root
    for name in sorted(os.listdir(src_dir)):
        rel_path = os.path.join(rel, name) if rel else name
        rel_norm = rel_path.replace('\\', '/')
        src = os.path.join(src_root, rel_path)
        dst = os.path.join(dst_root, rel_path)

        if rel_norm.split('/')[0] in PROTECTED or rel_norm in PROTECTED:
            stats['skipped'] += 1
            continue
        if rel_norm in KEEP_LOCAL or rel_norm in KEEP_ICONS:
            stats['kept'] += 1
            continue

        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            copy_tree(src_root, dst_root, rel_path, stats)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            stats['copied'] += 1
    return stats


def main():
    print('=== 步骤 1：复制新版程序文件 ===')
    stats = copy_tree(NEW, OLD)
    print(f"  复制 {stats['copied']} 个文件，跳过受保护 {stats['skipped']} 个，保留本地 {stats['kept']} 个")

    print('=== 步骤 2：恢复本地独有文件 ===')
    for rel in LOCAL_ONLY:
        src = os.path.join(NEW, rel)
        dst = os.path.join(OLD, rel)
        if os.path.exists(dst):
            print(f'  {rel}: 已存在，跳过')
            continue
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f'  {rel}: 已恢复')

    print('=== 步骤 3：确认教材大纲为本地版本 ===')
    for rel in KEEP_LOCAL:
        p = os.path.join(OLD, rel)
        d = json.load(io.open(p, encoding='utf-8'))
        print(f'  {rel}: {list(d.keys())}')

    print('=== 步骤 4：确认数据文件完好 ===')
    db = os.path.join(OLD, 'math_question_bank.db')
    print(f'  DB: {os.path.getsize(db)} bytes')
    print(f"  uploads: {len(os.listdir(os.path.join(OLD, 'static/uploads')))} 个文件")
    print(f"  .env: {'存在' if os.path.exists(os.path.join(OLD, '.env')) else '缺失'}")
    print(f"  venv: {'存在' if os.path.isdir(os.path.join(OLD, 'venv')) else '缺失'}")
    print('=== 完成 ===')


if __name__ == '__main__':
    main()
