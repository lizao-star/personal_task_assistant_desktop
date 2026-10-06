# API 文档

> 适用阶段：**M2（Windows 客户端 v0.2，读写 + 增量同步）**
> 返回入口：[CLAUDE.md](../CLAUDE.md) ｜ 相关：[设计总纲.md](设计总纲.md)、[飞书表结构.md](飞书表结构.md)
> M2 没有自建服务端，客户端直接调用**飞书开放平台 OpenAPI**，数据中心为飞书多维表格。
> 后续 M6 引入自建服务端时，会在本文档新增「自建服务 API」章节。

---

## 1. 鉴权

### 获取 tenant_access_token

`POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`

请求体：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx"
}
```

响应：

```json
{
  "code": 0,
  "msg": "ok",
  "tenant_access_token": "t-g104...",
  "expire": 7200
}
```

**客户端实现**：[client.py](../client_windows/app/feishu/client.py) 中 `FeishuClient` 自动缓存 token，到期前 5 分钟刷新；遇到 401 或 token 类错误码（99991661/99991663/99991664）强制刷新后重试。

后续所有请求头携带：

```
Authorization: Bearer {tenant_access_token}
```

---

## 2. 多维表格记录

### 2.1 列出记录

`GET /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records`

查询参数：

| 参数       | 说明                     |
| ---------- | ------------------------ |
| page_size  | 每页条数，客户端使用 100 |
| page_token | 分页游标，首次不传       |

响应结构：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "items": [
      {
        "record_id": "recXXXX",
        "fields": {
          "任务名": [{"type": "text", "text": "写课程论文引言"}],
          "状态": "待办",
          "优先级": "P1",
          "任务类型": "作业",
          "截止时间": 1760006340000,
          "标签": ["论文"],
          "是否语音提醒": true,
          "提醒规则": ["提前1天", "提前2小时", "提前10分钟"],
          "依赖任务": ["recYYYY"],
          "延期次数": 0
        }
      }
    ],
    "has_more": false,
    "page_token": "..."
  }
}
```

**分页**：`has_more=true` 时以 `page_token` 继续请求。客户端封装为 `FeishuClient.iter_records()` 迭代器。

### 2.2 字段类型与返回形态

| 飞书字段类型 | OpenAPI 返回形态                             | 解析函数             |
| ------------ | -------------------------------------------- | -------------------- |
| 文本         | `[{"type":"text","text":"..."}]`             | `parse_text`         |
| 单选         | `"P1"`                                       | `parse_select`       |
| 多选         | `["论文","阅读"]`                            | `parse_multi_select` |
| 数字         | `90`                                         | `parse_number`       |
| 复选框       | `true`                                       | 直接布尔转换         |
| 日期         | `1760006340000`（**毫秒时间戳**）            | `parse_datetime`     |
| 关联         | `["recXXXX"]` 或 `{"link_record_ids":[...]}` | `parse_links`        |

解析实现：[repository.py](../client_windows/app/feishu/repository.py)

### 2.3 任务表字段映射

| 本地 Task 属性 | 飞书字段名     |
| -------------- | -------------- |
| title          | 任务名         |
| status         | 状态           |
| priority       | 优先级         |
| task_type      | 任务类型       |
| due_at         | 截止时间       |
| start_at       | 开始时间       |
| estimate_min   | 预计耗时       |
| voice          | 是否语音提醒   |
| energy         | 精力需求       |
| delay_count    | 延期次数       |
| tags           | 标签           |
| depends_on     | 依赖任务       |
| reminder_rules | 提醒规则       |
| description    | 描述           |
| external_id    | 外部ID（M2）   |
| completed_at   | 完成时间       |
| modified_at    | 修改时间（M2） |

字段名常量：[constants.py](../client_windows/app/constants.py)

### 2.4 搜索记录（M2 新增）

`POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search`

请求体：

```json
{
  "filter": {
    "conjunction": "and",
    "conditions": [
      {"field_name": "修改时间", "operator": "isGreater", "value": ["ExactDate", "1759852800000"]}
    ]
  },
  "page_size": 100,
  "page_token": "..."
}
```

`field_name` 直接用中文字段名即可（避开 URL 编码坑）；`value` 第一个元素为类型标识 `ExactDate`，第二个为毫秒时间戳字符串。

客户端封装为 `FeishuClient.search_records()`，用于：
- **幂等查重**：按 `外部ID` 搜索；
- **增量拉取**：按 `修改时间 > cursor` 搜索。

### 2.5 新建记录（M2 新增）

`POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records`

请求体：

```json
{"fields": {"任务名": "写报告", "状态": "待办", "优先级": "P1", "任务类型": "作业",
            "截止时间": 1760006340000, "外部ID": "win-abc123def456", "同步来源": "客户端",
            "延期次数": 0}}
```

日期字段统一用**毫秒时间戳**。客户端封装为 `FeishuClient.create_record()` 与 `repository.create_task()`。

### 2.6 更新记录（M2 新增）

`PUT /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}`

请求体只传要改的字段即可，未传字段保持不变。

完成任务：
```json
{"fields": {"状态": "已完成", "完成时间": 1759824000000}}
```

延期任务：
```json
{"fields": {"截止时间": 1760006340000, "延期次数": 2, "状态": "待办"}}
```

### 2.7 读取单条记录（M2 新增）

`GET /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}`

主要用于**冲突检测**：推送前先读一次云端 `修改时间`，与本地缓存的基准值比较。

---

## 3. 筛选规则（M1 在客户端本地完成）

为避免飞书 `filter` 语法与中文编码问题，M1 拉取全表后在本地筛选：

| 视图     | 规则                                           |
| -------- | ---------------------------------------------- |
| 今日待办 | `截止时间` 为今天 且 `状态 ∉ {已完成, 已取消}` |
| 逾期任务 | `截止时间 < 现在` 且 `状态 ∉ {已完成, 已取消}` |

实现：`repository.get_today_tasks()` / `get_overdue_tasks()`

---

## 4. 优先级算法

纯规则计算（不调用 AI），公式：

```
priority_score = 0.40 * urgency
               + 0.30 * importance
               + 0.20 * blocking
               + 0.10 * energy_match
               - 延期次数 * 0.02
```

- urgency 分档：逾期 1.00 / 24h 内 0.95 / 3 天内 0.70 / 7 天内 0.50 / 更远 0.30
- importance：P0=1.0，P1=0.8，P2=0.6，P3=0.4
- blocking：任务被其他任务依赖的数量，3 个及以上为 1.0
- energy_match：精力需求（高/中/低）与当前时段匹配度（1.0 / 0.6 / 0.2）

实现：[priority.py](../client_windows/app/services/priority.py)，测试：[test_priority.py](../client_windows/tests/test_priority.py)

---

## 5. 提醒规则映射

「提醒规则」多选项 → 相对截止时间的提前量：

| 选项       | 提前量    |
| ---------- | --------- |
| 提前1天    | 1440 分钟 |
| 提前2小时  | 120 分钟  |
| 提前30分钟 | 30 分钟   |
| 提前10分钟 | 10 分钟   |

触发条件：`fire_at = 截止时间 - 提前量`，当 `fire_at ≤ 现在 ≤ fire_at + 补发窗口(默认60分钟)` 时触发。
去重键：`{record_id}:{规则文案}`，记录在本地 `reminder_log` 表，重启不重复提醒。

勿扰时段（默认 23:00–07:00，支持跨午夜）与「暂停提醒」时不触发。

实现：[reminder.py](../client_windows/app/services/reminder.py)

---

## 6. 错误处理与重试

| 情况                             | 策略                                   |
| -------------------------------- | -------------------------------------- |
| HTTP 429 / 500 / 502 / 503 / 504 | 指数退避重试，间隔 1s/2s/4s，最多 3 次 |
| HTTP 401 或 token 错误码         | 强制刷新 token 后重试                  |
| 网络连接失败 / 超时              | 退避重试；最终失败则展示本地缓存并提示 |
| 业务 code 非 0                   | 抛出 `FeishuAPIError`（含 code、msg）  |

---

## 7. 离线队列与幂等（M2）

### 设计

客户端不直接调飞书写接口，而是把写操作（create/complete/defer）**先写入本地 SQLite 表 `pending_ops`**，再由后台线程按序推送。带来的好处：

- **断网可用**：操作不丢，恢复后自动补交；
- **顺序保证**：同一任务的多次操作按入队顺序执行；
- **失败可重试**：单条失败不影响后续 op。

### 队列 schema

```sql
CREATE TABLE pending_ops (
    op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type    TEXT NOT NULL,           -- create / complete / defer
    payload    TEXT NOT NULL,           -- JSON：完整字段 + 冲突检测基准
    force      INTEGER NOT NULL DEFAULT 0,  -- 用户确认覆盖后为 1
    created_at TEXT NOT NULL
)
```

### payload 形态

create：
```json
{"external_id": "win-abc123", "fields": { ...build_create_fields 的产物... }}
```

complete / defer：
```json
{"record_id": "recXXX", "fields": {...}, "base_ms": 1759824000000, "title": "任务名"}
```

`base_ms` 是入队时该任务在本地缓存的 `modified_ms`（云端最后修改时间），用于冲突检测。

### 幂等策略

| 操作     | 幂等机制                                                                                           |
| -------- | -------------------------------------------------------------------------------------------------- |
| create   | 推送前按 `外部ID` 调 `search_records` 查重，命中即视为成功（说明上次推送已成功但客户端没收到响应） |
| complete | `PUT` 本身幂等；状态/完成时间覆盖写，无副作用                                                      |
| defer    | 同上                                                                                               |

### 冲突检测

推送 complete/defer 前，若 `force=0` 且 `base_ms > 0`：

1. 调 `GET .../records/{record_id}` 拿云端 `修改时间`；
2. 若云端 > `base_ms`：保留该 op，记入冲突列表；
3. 主线程弹出 `QMessageBox`：「云端已变更，是否覆盖」；
4. 覆盖 → `set_op_force` 后重新推送；放弃 → `delete_op` 并拉取云端版本。

---

## 8. 增量同步（M2）

### 游标

`sync_meta` 表存键 `pull_cursor_ms`，值为上次拉取后所有任务中**最大的修改时间（毫秒）**。

### 流程

```
手动「立即同步」/ 启动时        →  full_pull（list_tasks + replace_tasks + 重置游标）
45s 定时器 / 写操作触发         →  drain_queue + incremental_pull
                                     （search 修改时间 > cursor）
                                     失败 / 无游标 → 回退 full_pull
```

实现：[sync.py](../client_windows/app/services/sync.py)

---

## 9. M3 及后续将新增的接口（预告，当前未实现）

- 批量接口：`.../records/batch_create`、`batch_update`
- 发送消息：`POST /open-apis/im/v1/messages`
- 卡片回调：事件订阅 `card.action.trigger`
- AI 解析：DeepSeek Chat Completion（M3）
- 自建服务端 REST：`/tasks`、`/sync/changes`、`/ai/parse` 等（M6）
