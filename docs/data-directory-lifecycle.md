# `data/` 目录结构与任务生命周期

> 对标 CDE 的「workspace 即隔离单元 + 元数据集中管理」模式：一个任务（initiative）一个根目录，任务元数据/状态集中存放。

## 一、`data/` 下的目录构成

`data/` 分**两大块**：任务隔离区 + 集中存储区。

### 1. 任务隔离区 `data/missions/<id>/`（一个任务一个根）

```
data/missions/<initiative_id>/
├── memory/                   任务记忆 + 编码会话（MissionMemoryStore）
├── artifacts/<stage>/        阶段产物
├── workspace/repos/<name>/   编码沙盒（clone 的目标仓库工作副本）
└── chat/<file>.jsonl         任务群聊记录
```

| 子目录 | 作用 | 是否随任务完成自动删 |
|---|---|---|
| `memory/` | 任务记忆 + 编码会话 | 否（档案，保留） |
| `artifacts/<stage>/` | 各阶段产物 | 否（档案，保留） |
| `chat/*.jsonl` | 任务群聊记录 | 否（档案，保留） |
| `workspace/` | 编码沙盒（见下） | **是（自动回收）** |

### 2. 集中存储区（跨任务共享，按 id 索引）

均为持久文件，不与某任务绑定（路径见 `settings.py:186-237`）：

| 文件 | 内容 |
|---|---|
| `initiatives.json` / `initiatives.sqlite` | 任务元数据（状态机、当前阶段、timeline） |
| `checkpoints.sqlite` | 图执行检查点（断点续跑） |
| `runs.sqlite` | 运行记录后端 |
| `users.json` | 用户 |
| `knowledge.json` | 知识库 |
| `memory.json` | 全局记忆/配置（区别于 `missions/<id>/memory` 任务记忆） |
| `agents.json` / `toolkit.json` / `bridges.json` | agent / toolkit / 桥接定义 |

> 旧布局 `data/artifacts/`、`data/worktrees/`、`data/rooms/` 在启动时由 `migrate_legacy_layout` move 进 `missions/<id>/` 对应子目录，原地只留空壳（`layout.py:69-114`）。

## 二、`workspace/` 是什么

`workspace/` 是任务的**编码沙盒**——agent 真正动手改代码的地方，不含任何任务档案。主要两类内容：

- `workspace/repos/<name>/`：每个目标仓库的 git 工作副本（浅克隆），agent 在此编码，再经 `git_publish` 回写分支、提 MR。
- 注入的规则/配置目录：`.cursor/rules/`、`.sortie-deck/`（知识包写入）；mock 测试还会生成 `demo.py`。

## 三、目录的创建 / 删除流程

### 创建阶段（基本都是**惰性** mkdir，首次写入时建）

- **提交需求**：`create_initiative` 在 `initiatives.sqlite` 落元数据。`missions/<id>/` 根及四个子目录不预建，由各模块第一次写时 `mkdir(parents=True)` 连带创建。
- `chat/`：第一条群聊消息即追加 `.jsonl`。
- `memory/`：第一次写记忆或编码会话时建（`mission_memory.py:71-73`、`106`、`112`、`123`）。
- `artifacts/<stage>/`：每个 stage 跑完落产物时建 `stage_dir`（`artifacts.py:79-80`）。
- `workspace/repos/<name>/`：进入**编码阶段**才 clone 目标仓库（`coding.py:184-185`），并注入 `.cursor/rules`、`.sortie-deck/`（`knowledge_pack.py:148-151`）。

### 删除 / 回收阶段

```
任务运行中：工作副本仅在编码期存在
   │
   ├─ push 全部成功 ──→ cleanup_repos：删 workspace/repos/（保留 workspace 壳）
   │                    git_publish.py:235
   │
   ├─ 任务终态 DONE/STOPPED 或用户 Stop
   │      └─→ cleanup_workspace：删整个 workspace/（repos+.cursor+.sortie-deck 全没）
   │           orchestrator.py:1615 / 1619
   │
   ├─ TTL sweep（每小时）──→ 非活跃任务 workspace 超期删
   │      干净 1 天 / 脏(有未提交改动) 7 天   git_publish.py:297
   │
   └─ 主动删除任务（界面/API delete_initiative）
          └─→ rmtree(missions/<id>)：memory+artifacts+chat+workspace 整个任务目录全删
               + initiatives.sqlite 移除该 id 记录
               orchestrator.py:1318-1326
```

### 清理边界速查

| 触发点 | 删除范围 | 代码 |
|---|---|---|
| push 成功后 | 仅 `workspace/repos/`，`workspace/` 壳保留 | `git_publish.py:235-244` |
| 任务终态 / 用户 Stop | 整个 `workspace/`（含 repos、`.cursor/rules`、`.sortie-deck` 等） | `orchestrator.py:1245-1257` → `git_publish.py:247-256` |
| TTL sweep（每小时） | 非活跃任务的 `workspace/`（干净 1 天 / 脏 7 天） | `git_publish.py:297-335` |
| 主动删除任务 | 整个 `data/missions/<id>/` + 元数据记录 | `orchestrator.py:1318-1326` |

## 四、关键结论

- **只有 `workspace/` 会被自动回收**；`memory/`、`artifacts/`、`chat/` 是任务档案，**永不自动删**，除非主动删除任务。
- `cleanup_repos`（push 后）是 `rmtree(repos_root)`，把 `repos/` 下**所有**目标仓库一起清，不按需求筛选只删某一个。
- 集中存储区的 `*.sqlite` / `*.json` 不会被单个任务删掉（共享文件）；主动删任务时仅从其中移除该 id 记录，文件本身保留。
