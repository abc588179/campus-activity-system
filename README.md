# 校园活动管理系统 V3.0

## 1. 项目简介

校园活动管理系统用于支持学校活动的发布、查看和报名管理。

V1.0 面向学生和活动组织教师两类用户，完成了「教师发布活动 → 学生查看活动 → 学生报名 → 教师查看报名学生」的核心业务闭环。

V2.0 在 V1.0 基础上引入**报名双状态模型**与**FIFO 候补队列**，并新增系统管理员角色。核心变化来自用户访谈：

- 学生无法判断自己是「已确认」还是「候补」，状态不透明；
- 热门活动满员后直接被拒绝，没有排队等待的途径；
- 学生临时不能参加时无法自行取消，教师名单无法及时更新。

V3.0 的依据是教师提供的模拟运行数据。分析显示报名需求高度集中在开放瞬间，峰值窗口内「满员 / 候补」分支的处理耗时和失败率都明显升高，因此本轮只做**性能与提交可靠性**方面的改造，不新增业务功能。

## 2. 用户角色

### 学生

注册、登录、浏览活动、报名或进入候补、取消报名、退出候补、查看自己的报名状态与候补位置。

### 活动组织教师

登录、发布活动、修改活动、关闭活动，查看本活动的**已确认名单**与**候补名单**。

### 系统管理员（V2.0 新增）

登录，查看全平台活动列表与用户账号状态。**不提供**报名审批、候补处理、活动编辑等业务操作入口。

## 3. 功能范围

### V1.0 已实现

- 学生注册、登录
- 教师发布、修改、关闭活动
- 学生查看活动列表与详情
- 学生报名（防重、满员、过期校验）
- 教师查看自己活动的报名学生
- 基于角色的访问控制

### V2.0 新增

- `Registration` 引入 `status`（confirmed / waiting / cancelled）与 `queue_order`
- 满员后报名转入候补队列，不再直接拒绝
- 候补队列按 FIFO 排序，学生可查看自己的候补位置
- 已确认学生取消报名后，候补队列首位自动递补，剩余候补顺序前移
- 候补学生可主动退出候补
- 教师端报名管理页分「已确认名单」「候补名单」两个区域
- 新增 `admin` 角色与 `/admin/activities`、`/admin/users` 页面

### V3.0 新增

- `Activity` 增加 `confirmed_count` / `waiting_count` 冗余计数
- 报名改为带条件的原子占位，写锁内不再做聚合查询
- 数据库启用 WAL、`synchronous=NORMAL`、`busy_timeout`
- 报名提交引入 `request_token` 幂等
- 报名表单提交按钮防抖
- 教师修改人数上限时不得小于当前已确认人数

### 明确不做

- 活动签到
- 评论留言、图片上传
- 短信 / 微信消息通知
- 第三方登录
- 管理员调整角色或停用账号
- 候补结果通知（需要外部通道）
- 活动检索与筛选

## 4. 核心业务流程

### 学生报名

1. 学生登录后进入活动详情页。
2. 系统按顺序校验：活动是否存在 → 是否已关闭 → 是否已结束 → 提交令牌是否已处理过 → 是否已报名。
3. 用 `UPDATE Activity SET confirmed_count = confirmed_count + 1 WHERE id = ? AND confirmed_count < max_participants` 原子占用名额。
4. 占位成功：状态 `confirmed`。
5. 占位失败：`waiting_count` 自增并返回新值作为 `queue_order`，状态 `waiting`。

### 学生取消 / 教师查看

1. 已确认学生取消报名，记录状态变为 `cancelled`，`confirmed_count` 减 1。
2. 系统取出该活动 `queue_order` 最小的候补记录改为 `confirmed`，`waiting_count` 减 1，并重排剩余候补顺序。
3. 教师在报名管理页分别查看已确认名单与候补名单。

## 5. 重要业务规则

1. 一个学生对一个活动只有一条报名记录（`UNIQUE (student_id, activity_id)`）。
2. 已确认或候补中的学生不能重复报名同一活动；同一 `request_token` 重放返回首次结果。
3. 活动人数达到上限后不再新增已确认记录（`confirmed_count` 不超过 `max_participants`）。
4. 已结束或已关闭的活动不能报名。
5. 候补递补严格按 `queue_order` 先到先得。
6. 只有已确认的报名可以取消；候补学生只能退出候补。
7. 教师只能管理自己发布的活动。
8. 管理员只能查看，不能处理报名业务。

## 6. 本地运行

```bash
pip install -r requirements.txt
python app.py
```

默认访问 `http://127.0.0.1:5000`。首次启动会自动建库并创建两个测试账号：

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| teacher | teacher123 | 活动组织教师 |
| admin | admin123 | 系统管理员 |

学生账号通过 `/register` 页面自行注册。

## 7. 数据库升级说明

V1.0 的 `Registration.status` 只有 `active` / `cancelled` 两个取值。`init_db()` 会在启动时按需迁移：

- V2.0：检测 `queue_order` 字段是否存在，若不存在则重建 `Registration` 表，`active` 映射为 `confirmed`；
- V3.0：为 `Activity` 追加 `confirmed_count` / `waiting_count` 并按 `Registration` 回填；为 `Registration` 追加 `request_token` 并建立索引。

迁移都在事务中完成，不需要手工删库。

## 8. V3.0 变更（基于运行数据）

数据结论、改动依据与取舍见 `docs/v3-iteration.md`；完整统计输出见 `docs/v3-analysis-output.txt`。

主要改动：

- `Activity` 增加 `confirmed_count` / `waiting_count` 冗余计数，报名与取消不再在写锁内执行 `COUNT(*)` 聚合；
- 报名改为带条件的 `UPDATE ... WHERE confirmed_count < max_participants` 原子占位；
- 数据库启用 WAL、`synchronous=NORMAL`、`busy_timeout=15000`，降低峰值期提交开销；
- 报名提交引入 `request_token` 幂等：同一令牌重放返回首次结果，而不是报「不能重复报名」；
- 报名表单增加提交按钮防抖；
- 为 `Registration (activity_id, status)` 与 `Registration (student_id)` 建立索引；
- 教师修改人数上限时校验不得小于当前已确认人数。

验证方式：

```bash
python -m unittest discover -s tests -v                          # 27 项功能与并发测试
python tools/analyze_data.py --data-dir <数据目录>                # 运行数据分析
python tools/loadtest.py --app-dir . --label V3.0 --out out.json  # 并发压测
```

压测结果见 `docs/v3-loadtest-result.json`：同一场景下 V3.0 相对 V2.0 的 p50 下降 35.5%、p90 下降 38.9%、p99 下降 55.6%，两版均无超卖、无 5xx。
