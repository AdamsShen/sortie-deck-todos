# 计划文档（plan.md）

> 用途：功能实现前的技术方案先落地此文件，经确认后执行。每次新计划覆盖本文档，历史计划归档到 `docs/plans/`。

# local 模式 Office 文档解析（纯 Python 库，不接 MinerU）

## 需求背景

知识库 local 模式当前仅支持 md/txt/csv（直接 decode）与 pdf（pypdf），docx/ppt/xlsx 在 `app.py:639-644` 直接 400。用户决定**不接 MinerU**，用纯 Python 库本地解析 Office 文档。

## 方案

1. **依赖**：`python-docx`（docx）、`python-pptx`（pptx）、`openpyxl`（xlsx），加入 `pyproject.toml`；
2. **新模块** `src/sortie_deck/doc_extract.py`（已创建）：
   - `extract_docx`：按文档体顺序遍历段落与表格，标题样式映射 `#` Markdown、列表加 `-`、表格转 Markdown 表；
   - `extract_pptx`：逐页「## Slide N」+ 文本框 + 表格 + 演讲者备注；
   - `extract_xlsx`：逐工作表「## 工作表：名」+ TSV 行（上限 500 行 × 50 列，防超大文件撑爆正文）；
   - `extract_office_text(ext, raw)` 统一入口；损坏/加密文件抛 ValueError（路由转 400 中文提示）；
3. **路由接线**：`app.py` 导入端点 local 分支增加 office 处理（docx/pptx/xlsx → `extract_office_text`，提取为空报 400）；
4. **取舍点（已向用户说明）**：旧版二进制 .doc/.ppt/.xls 无纯 Python 可靠解析库，保持 400 并提示「另存为 docx/pptx/xlsx 或配置 WeKnora」；
5. **测试**：`tests/test_doc_extract.py` 用三个库在内存生成真实文档 → 解析断言 + 损坏文件报错；全量回归 + 重启后端。

## 状态

- [x] 方案确认（用户指示直接用 Python 库解析）
- [x] doc_extract.py 模块（docx/pptx/xlsx 三解析器 + 统一入口，表格 XML 需包 Table 代理的坑已踩过并修复）
- [x] 依赖安装（python-docx / python-pptx / openpyxl，pyproject 已声明）+ 路由接线（app.py local 分支 office 处理，旧版 .doc/.ppt/.xls 保持 400 并提示转换）
- [x] 测试：tests/test_doc_extract.py 5 个用例全过（真实文档内存生成 → 抽取断言 + 损坏文件报错）；全量回归 355 passed / 2 failed——失败项为另一会话并行改动（WeKnora graphrag，auth.py/weknora.py/test_weknora_gateway.py）在 WeKnora 服务 502 下的环境性失败，与本计划无关
- [x] 后端已重启验证（端口 8787）
