# -*- coding: utf-8 -*-
"""比较本地（含自定义改动）与新版 3.0.0 各文件的差异规模。"""
import difflib, io, os, subprocess

OLD = r'C:/Users/EDY/WorkBuddy/2026-10-19-16-05-43/math-question-bank'
NEW = r'C:/Users/EDY/WorkBuddy/2026-10-19-16-05-43/_upgrade_tmp'

files = subprocess.run(['git', 'diff', '--name-only'], cwd=OLD,
                       capture_output=True, text=True, encoding='utf-8').stdout.split('\n')
files = [f.strip().strip('"') for f in files if f.strip()]
files = [f.encode('utf-8').decode('unicode_escape').encode('latin-1').decode('utf-8')
         if '\\345' in f else f for f in files]

for rel in files:
    old_p = os.path.join(OLD, rel)
    new_p = os.path.join(NEW, rel)
    if not os.path.exists(new_p):
        print(f'{rel}: 新版无此文件')
        continue
    if rel.endswith(('.png', '.ico')):
        print(f'{rel}: 二进制，{os.path.getsize(old_p)} vs {os.path.getsize(new_p)}')
        continue
    try:
        a = io.open(old_p, encoding='utf-8').read().splitlines()
        b = io.open(new_p, encoding='utf-8').read().splitlines()
    except Exception as e:
        print(f'{rel}: 读取失败 {e}')
        continue
    sm = difflib.SequenceMatcher(None, a, b)
    ratio = sm.ratio()
    print(f'{rel}: 本地{len(a)}行 新版{len(b)}行 相似度{ratio:.3f}')
