# 账号体系增强：自助注册（管理员审核）+ 账号注销 + 管理员改权限

> 日期：2026-10-19 · 状态：已完成
> 注：自本计划起，计划文档统一存放于 `docs/plans/<日期>-<主题>.md`，不再写入项目根 `plan.md`。

## 一、需求背景

系统当前账号只能由 admin 通过 `POST /api/admin/users` 创建（`src/sortie_deck/api/app.py:361`），没有自助注册，也没有账号注销能力。本次需求：

1. **自助注册**：任何人可在登录页注册，注册后处于「待审核」状态，admin 审核通过后才能登录（用户已确认）；
2. **账号注销**：用户可永久删除自己的账号（含清除登录 token）；其创建的任务与共享关系保留原样（用户已确认）；
3. **管理员修改用户权限**：admin 可在用户管理页调整任意用户角色（member ↔ admin）；
4. **任务分享**：后端 `POST/GET/DELETE /api/initiatives/{id}/shares`（`src/sortie_deck/api/app.py:1543-1600`）与前端 `ShareManager` 组件均已存在，本次不改（用户已确认）。

## 二、业内调研（真实检索结果）

| 来源 | 做法 |
|---|---|
| Damvia「Users and approval」（https://damvia.com/docs/administration/users-and-approval） | 注册表单直接创建 unapproved member，等待管理员批准后才能使用 |
| Almabase 审批机制（https://docs.almabase.com/en/articles/8019741） | manual review：注册后进入 pending 队列，管理员逐个 approve/reject |
| Stack Overflow「organise database to account for changing status in users」（https://stackoverflow.com/q/12096015） | 主流做法是单张 users 表 + status 字段（unconfirmed/confirmed），而非两张表 |
| Ardalis「The Evolution of Status Pattern」（https://ardalis.com/the-evolution-of-status-pattern/） | 状态字段从枚举起步，登录/鉴权处统一校验状态 |

**共性归纳**：单表 + `status` 字段（pending / active / rejected）；注册只写记录不授权，登录与 token 校验统一拦截非 active 用户；管理员在用户管理页做 approve/reject。
**与本项目兼容点**：users.json 本来就是单文件单表模型（`src/sortie_deck/auth.py:85` AuthStore），加 `status` 字段零迁移成本；「最后一个 admin 保护」模式可直接复用到改角色逻辑。

## 三、技术设计

### 3.1 数据模型（src/sortie_deck/auth.py）

- `User` 增加 `status: Literal["pending", "active", "rejected"] = "active"`（存量数据兼容，内置账号默认 active；注册用户固定 pending + member）
- `PublicUser` 增加 `status` 字段；新增 `RegisterRequest(username, password, display_name)`

### 3.2 AuthStore 新增/修改方法

| 方法 | 说明 |
|---|---|
| `register(req)` | 重名校验 → 创建 pending/member 用户 |
| `set_status(user_id, status)` | 审核：approve（active）/ reject（rejected，并清该用户 token） |
| `update_role(user_id, role)` | 改角色；降级最后一个 admin 被拒绝 |
| `delete_self(user_id)` | 注销：删用户 + 清全部 token；保护最后一个 admin |
| `login()` 修改 | 非 active 抛错区分待审核/已拒绝 |
| `resolve()` 修改 | token 解析校验 status，被拒后旧 token 立即失效 |
| `list_users()` 修改 | 返回带 status |

### 3.3 API 端点（src/sortie_deck/api/app.py）

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/auth/register` | 免鉴权 | 自助注册；用户名重复 400 |
| `PATCH /api/admin/users/{user_id}` | require_admin | body `{role?, status?}`，改权限与审核复用 |
| `DELETE /api/auth/account` | require_user | 注销自己的账号，任务与共享关系保留 |
| `POST /api/auth/login` 修改 | 免鉴权 | 非 active 返回 403，区分 pending / rejected |
| `GET /api/admin/users` 修改 | require_admin | 列表含 status |

### 3.4 前端（apps/web/src/App.tsx + api.ts + i18n.ts）

1. 登录页登录/注册双模式切换；注册成功显示「等待管理员审核」；登录 403 按原因映射文案。
2. Admin 用户管理页：状态徽标（待审核/正常/已拒绝）+ pending 用户「通过/拒绝」按钮 + 角色下拉即改即提交。
3. 顶栏「注销账号」按钮，confirm 二次确认后调 `DELETE /api/auth/account`。
4. i18n 中英文案补齐；`api.ts` 增加 `registerUser` / `adminUpdateUser` / `deleteOwnAccount`。

### 3.5 测试（tests/test_auth.py）

注册 pending / 重名拒绝、pending 与 rejected 登录拦截、reject 后旧 token 失效、最后一个 admin 降级与注销保护、注销后 token 失效。

## 四、涉及文件清单

| 文件 | 改动 |
|---|---|
| `src/sortie_deck/auth.py` | status 字段 + register/set_status/update_role/delete_self + login/resolve 拦截 |
| `src/sortie_deck/api/app.py` | /api/auth/register、PATCH /api/admin/users/{id}、DELETE /api/auth/account、login 细分 |
| `apps/web/src/App.tsx` | 注册表单、admin 用户管理改版、注销入口、403 文案映射 |
| `apps/web/src/api.ts` | registerUser / adminUpdateUser / deleteOwnAccount 封装 |
| `apps/web/src/types.ts` | UserStatus 类型、PublicUser.status |
| `apps/web/src/i18n.ts` | 中英文案 key |
| `tests/test_auth.py` | 新增 4 组用例 |

## 五、备选方案（未采用）

1. 完全开放注册（无需审核）——用户明确选择了管理员审核模式。
2. 独立待审核列表接口 `/api/admin/pending-users`——收益有限，改为现有列表加徽标与筛选。
3. 注销时任务一并删除——用户明确选择保留任务。

## 六、状态

- [x] 方案已确认（2026-10-19）
- [x] 后端实现（auth.py / app.py）
- [x] 前端实现（App.tsx / api.ts / i18n）
- [x] 测试与回归（tests/test_auth.py 新增 4 组用例；全量 352 passed；TestClient 全链路冒烟通过）
