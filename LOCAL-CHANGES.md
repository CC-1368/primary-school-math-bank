# 本地改动说明（LOCAL-CHANGES）

本仓库基于开源项目 [JudgePeach/math-question-bank](https://github.com/JudgePeach/math-question-bank)，
以下内容为**本地化改造**记录。分支说明：

| 分支 | 含义 | 说明 |
|---|---|---|
| `main` | 官方基线 | 与上游保持一致，可随时 pull 升级，**不要在上面直接改** |
| `local/custom` | 本地改造层 | 所有本地改动都在这里，日常使用这个分支 |

> ⚠️ **不要执行 `git push`**。`origin` 指向原作者仓库，你没有写权限，也不应该推送。

---

## 一、日常命令（记住这 5 条就够）

```bash
# 看当前在哪个分支（带 * 的就是）
git branch

# 改坏了，把文件全部退回上次提交的状态
git checkout .

# 想看今天提交里改了什么
git show --stat HEAD

# 临时回到官方干净版本（你的改动仍在 local/custom 里，不会丢）
git checkout main
git checkout local/custom      # 再切回来
```

---

## 二、本地改造清单（2026-10-27 提交，commit 880804b）

128 个文件、+42,164 / -1,967 行。按能力分四块：

### 1. 文档项目流水线（10 个模块）
`mathbank/document_project_*.py`、`document_projects.py`、`document_task_store.py`

把"一份试卷从导入到入库"做成有状态的项目：支持草稿、任务恢复、断点续跑，
长任务中断后不用从头再来。

### 2. PDF 解析与图块治理（22 个模块）
`mathbank/pdf_*.py`

- 解析策略：原生矢量提取优先 → 页级置信度不足时降级 VLM 视觉识别 → 保留原页复核标记
- 图块处理：裁切、恢复、复核（`pdf_figure_*`）
- 稳定性：空白页剔除、跨页缝合、并行分片、任务断点续跑、OCR 归档与质量校验

### 3. 题目结构化
- `fillin.py`、`fillin_placeholders.py`：填空题标记的保守修复
- `ai_knowledge_points.py`、`knowledge_points.py`：知识点 AI 分类建议（AI 只建议，不创建身份）
- `markdown_tables.py`、`formula_notices.py`

### 4. 导出与流转
- `export_preflight.py`：导出前做本地图片完整性预检，**fail-closed**——缺图直接阻断并输出哈希定位，不静默出卷
- `background_export.py`：后台批量导出
- `library_transfer.py`：题库可移植迁移（append-only，导入前自动做完整备份）

### 5. 教材大纲收敛为仅人教版

删除了三套**高中数学**教材大纲（与本项目小学数学场景无关）：

| 文件 | 内容 | 体积 |
|---|---|---|
| `mathbank/resources/curriculums/B.json` | 人教B版高中数学 | 7 KB |
| `mathbank/resources/curriculums/H.json` | 沪教版高中数学 | 4 KB |
| `mathbank/resources/curriculums/S.json` | 苏教版高中数学 | 3 KB |

同时 `mathbank/curriculums.py` 的配置改为：

```python
CURRICULUM_NAMES = {"A": "人教版"}
```

---

## 三、以后想把教材大纲加回来

**只放回 json 文件不够，必须同时改代码，否则会报错 `不支持的教材大纲版本: B`。**

### 第 1 步：从 git 历史取回文件（14 KB，秒回）

```bash
# 取回全部三个
git checkout 1a6e50a -- mathbank/resources/curriculums/

# 或者只取一个
git checkout 1a6e50a -- mathbank/resources/curriculums/B.json
```

> `1a6e50a` 是删除前最后一个包含这些文件的官方提交，该提交永久保留在仓库里。

### 第 2 步：改 `mathbank/curriculums.py`

```python
CURRICULUM_NAMES = {
    "A": "人教版",
    "B": "人教B版",
    "S": "苏教版",
    "H": "沪教版",
}
```

### 第 3 步：重启服务

代码用了 `lru_cache` 缓存教材大纲，**必须重启进程**才会生效。

### 不确定删了哪些文件时

```bash
git log --diff-filter=D --name-only        # 查所有被删除的文件及对应提交
```

---

## 四、哪些东西没有进 git（这是对的，不是遗漏）

已在 `.gitignore` 中排除：

| 内容 | 原因 |
|---|---|
| `python/`（127 MB） | 内置的便携 Python 运行时，属于发布包，不是源码 |
| `tools/cloudflared.exe`（53 MB） | 公网共享用的二进制工具 |
| `*.db` / `*.db-wal` / `*.db-shm` | 你的题库数据，体积大且天天变 |
| `venv/` / `__pycache__/` | 依赖与编译缓存 |
| `.env` | **含 API Key，绝不能提交** |
| `logs/`、`*.bak`、`*.bak-*` | 日志与手工备份 |
| `data_backup/`、`static/uploads/` | 题库备份与上传的图片 |

---

## 五、以后每次改完都做一次

```bash
git add -A
git commit -m "一句话说明这次改了什么"
```

一次改一个功能、提交一次。这样出问题能立刻定位是哪次改动引入的，
也能随时退回任意一个"能正常跑"的版本。
