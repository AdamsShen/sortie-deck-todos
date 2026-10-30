# 产物与存储

## 1. 概述

阶段产出的文件存储；任务维护 `ArtifactRef` 索引；API 提供下载。

| 项 | 值 |
|----|-----|
| 代码 | `artifacts.py` |
| 根 | `data/artifacts/<initiative>/<stage>/` |
| API | `GET .../artifacts/{path}` |

## 2. 写入路径

执行器 map → `write_text` → `upstream` → `_sync_artifacts_from_upstream` 追加引用。

## 3. 安全要求

- 防路径穿越
- 下载需登录（后续可加任务成员限制）
- 不执行产物内容

## 4. 技术方案（目标）

- 内容寻址 blob + 引用
- 工程回环 diff API
- S3 兼容后端
- 短时签名 URL

## 5. 开发计划

### P0（2 天）

| 任务 | 验收 |
|------|------|
| 穿越测试 | 拒绝 |
| 下载需鉴权 | 无 token 401 |
| 磁盘布局文档 | 本文 |

### P1（1 周）

| 任务 | 验收 |
|------|------|
| 两版本 diff | API + 简单 UI |
| Ref.meta 存 hash | 有 |

### P2（2 周）

| 任务 | 验收 |
|------|------|
| S3 后端 | 开关 |
| 保留清理任务 | 旧任务可删 |

## 6. 依赖

- Graph 写入、Orchestrator 同步、Web 预览、Contracts 文件名

## 7. 任务输入快照（`input/`，2026-10）

用户导入的资料不再只以「文本摘录」形式存在于任务 JSON 里，而是**原件落盘 + 文本抽取 + manifest 索引**：

| 路径 | 内容 |
|---|---|
| `data/missions/<id>/input/manifest.json` | 唯一索引：`id/name/kind/mime/size/sha256/pinned/extract_status` + 相对路径 |
| `input/files/<mat_id>__<name>` | 原始二进制（PDF / DOCX / XLSX / 图片…） |
| `input/text/<mat_id>.md` | 抽取文本（文本类、pdf、docx、xlsx；图片等为 `skipped`） |
| `input/brief.md` | brief 的唯一快照（pinned 资料按策略内联或给指针） |

- 代码：`mission_input.py` —— 原子写（锁 + `tmp`→`os.replace`）、sha256 幂等去重、单文件 50MB / 单任务 500MB 配额。
- API：`POST /api/missions/{id}/input`（multipart 上传）、`GET`（列表）、`PATCH`（置顶）、`DELETE`（删除并清理原件）。
- 注入策略（动态加载）：pinned 且文本 ≤4000 字符 → 内联进 brief；更长或二进制 → 只留 `input/...` 指针；执行器额外注入 `## Input index`（`StageContext.inputs`），正文由 agent 按需读取。
- 生命周期：随任务保留（不随 workspace 回收）；「会话开了但从未建队」的目录由 `maintenance.sweep_orphan_missions` 按 `TDT_INTAKE_TTL_DAYS`（默认 7 天）回收。
