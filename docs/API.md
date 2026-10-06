# API 文档

> 适用阶段：**M1（Windows 客户端 v0.1）**
> 返回入口：[CLAUDE.md](../CLAUDE.md) ｜ 相关：[设计总纲.md](设计总纲.md)、[飞书表结构.md](飞书表结构.md)
> M1 没有自建服务端，客户端直接调用**飞书开放平台 OpenAPI**，数据中心为飞书多维表格。
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

| 参数 | 说明 |
|---|---|
| page_size | 每页条数，客户端使用 100 |
| page_token | 分页游标，首次不传 |

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

| 飞书字段类型 | OpenAPI 返回形态 | 解析函数 |
|---|---|---|
| 文本 | `[{"type":"text","text":"..."}]` | `parse_text` |
| 单选 | `"P1"` | `parse_select` |
| 多选 | `["论文","阅读"]` | `parse_multi_select` |
| 数字 | `90` | `parse_number` |
| 复选框 | `true` | 直接布尔转换 |
| 日期 | `1760006340000`（**毫秒时间戳**） | `parse_datetime` |
| 关联 | `["recXXXX"]` 或 `{"link_record_ids":[...]}` | `parse_links` |

解析实现：[repository.py](../client_windows/app/feishu/repository.py)

### 2.3 任务表字段映射

| 本地 Task 属性 | 飞书字段名 |
|---|---|
| title | 任务名 |
| status | 状态 |
| priority | 优先级 |
| task_type | 任务类型 |
| due_at | 截止时间 |
| start_at | 开始时间 |
| estimate_min | 预计耗时 |
| voice | 是否语音提醒 |
| energy | 精力需求 |
| delay_count | 延期次数 |
| tags | 标签 |
| depends_on | 依赖任务 |
| reminder_rules | 提醒规则 |

字段名常量：[constants.py](../client_windows/app/constants.py)

---

## 3. 筛选规则（M1 在客户端本地完成）

为避免飞书 `filter` 语法与中文编码问题，M1 拉取全表后在本地筛选：

| 视图 | 规则 |
|---|---|
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

| 选项 | 提前量 |
|---|---|
| 提前1天 | 1440 分钟 |
| 提前2小时 | 120 分钟 |
| 提前30分钟 | 30 分钟 |
| 提前10分钟 | 10 分钟 |

触发条件：`fire_at = 截止时间 - 提前量`，当 `fire_at ≤ 现在 ≤ fire_at + 补发窗口(默认60分钟)` 时触发。
去重键：`{record_id}:{规则文案}`，记录在本地 `reminder_log` 表，重启不重复提醒。

勿扰时段（默认 23:00–07:00，支持跨午夜）与「暂停提醒」时不触发。

实现：[reminder.py](../client_windows/app/services/reminder.py)

---

## 6. 错误处理与重试

| 情况 | 策略 |
|---|---|
| HTTP 429 / 500 / 502 / 503 / 504 | 指数退避重试，间隔 1s/2s/4s，最多 3 次 |
| HTTP 401 或 token 错误码 | 强制刷新 token 后重试 |
| 网络连接失败 / 超时 | 退避重试；最终失败则展示本地缓存并提示 |
| 业务 code 非 0 | 抛出 `FeishuAPIError`（含 code、msg） |

---

## 7. M2 及后续将新增的接口（预告，当前未实现）

- 记录新增/更新/删除：`POST|PUT|DELETE .../records[/{record_id}]`
- 批量接口：`.../records/batch_create`、`batch_update`
- 发送消息：`POST /open-apis/im/v1/messages`
- 卡片回调：事件订阅 `card.action.trigger`
- 自建服务端 REST：`/tasks`、`/sync/changes`、`/ai/parse` 等（M6）
