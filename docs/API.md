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

| 飞书字段类型 | OpenAPI 返回形态                                                                              | 解析函数             |
| ------------ | --------------------------------------------------------------------------------------------- | -------------------- |
| 文本         | `[{"type":"text","text":"..."}]`                                                              | `parse_text`         |
| 单选         | `"P1"`                                                                                        | `parse_select`       |
| 多选         | `["论文","阅读"]`                                                                             | `parse_multi_select` |
| 数字         | `90`                                                                                          | `parse_number`       |
| 复选框       | `true`                                                                                        | 直接布尔转换         |
| 日期         | `1760006340000`（**毫秒时间戳**）                                                             | `parse_datetime`     |
| 关联         | `["recXXXX"]`、`{"link_record_ids":[...]}` 或 `[{"record_ids":[...],"text":...}]`（较新版本） | `parse_links`        |

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
| ai_breakdown   | AI拆解（M3）   |
| checklist      | 检查清单（M3） |
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

### 2.8 子任务表字段映射（M3 新增）

客户端详情对话框展示子任务。`SubTask` 模型与解析同样在 [repository.py](../client_windows/app/feishu/repository.py)，
字段名常量为 `SubtaskFields`（[constants.py](../client_windows/app/constants.py)）。

| 本地 SubTask 属性 | 飞书字段名 | 说明                             |
| ----------------- | ---------- | -------------------------------- |
| title             | 子任务名   |                                  |
| parent_record_id  | 所属任务   | 关联字段，取第一个 record_id     |
| status            | 状态       | 待办/进行中/已完成/取消          |
| order             | 顺序       | 展示时按顺序升序                 |
| estimate_min      | 预计耗时   |                                  |
| due_at            | 截止时间   |                                  |
| completed_at      | 完成时间   |                                  |
| ai_hint           | AI提示     | 详情中悬停子任务行时作为提示展示 |

拉取方式：每次同步（全量与增量）都对子任务表做一次**全量 list**（子任务量小，不做增量游标），
结果整体替换进 SQLite 的 `subtask_cache` 表。`FEISHU_SUBTASK_TABLE_ID` 未配置时自动跳过；
拉取失败只记日志，不影响任务同步。

---

## 3. 筛选规则（M1 在客户端本地完成）

为避免飞书 `filter` 语法与中文编码问题，M1 拉取全表后在本地筛选：

| 视图     | 规则                                                                      |
| -------- | ------------------------------------------------------------------------- |
| 待办任务 | `状态 ∉ {已完成, 已取消, 收集箱}` 且（`无截止时间` 或 `截止时间 ≥ 现在`） |
| 逾期任务 | `截止时间 < 现在` 且 `状态 ∉ {已完成, 已取消, 收集箱}`                    |
| 收集箱   | `状态 == 收集箱`                                                          |

三个视图互斥（同一任务只会出现在一个视图里），合起来恰好覆盖全部未结束任务：
没有截止时间的任务归入待办任务，已过截止时间的任务归入逾期任务，
状态为「收集箱」的任务（无论是否有截止时间）只出现在收集箱区——
用户一旦把状态改为「待办/进行中/等待」等，下次同步会自动从收集箱中移除。

待办列表按优先级分数降序排序，默认只显示前 5 个最重要的任务，
其余通过「展开全部」按钮查看。

实现：`repository.get_today_tasks()` / `get_overdue_tasks()` / `get_inbox_tasks()`

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
{"external_id": "win-abc123", "fields": { ...build_create_fields 的产物... }, "subtasks": ["步骤1", "步骤2"]}
```

complete / defer：
```json
{"record_id": "recXXX", "fields": {...}, "base_ms": 1759824000000, "title": "任务名"}
```

ai_breakdown（M3，采纳 AI 拆解）：
```json
{"record_id": "recXXX", "fields": {...AI拆解/检查清单/AI建议...}, "subtasks": ["步骤1"], "base_ms": 1759824000000, "title": "任务名"}
```

`base_ms` 是入队时该任务在本地缓存的 `modified_ms`（云端最后修改时间），用于冲突检测。

### 幂等策略

| 操作         | 幂等机制                                                                                                                                   |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| create       | 推送前按 `外部ID` 调 `search_records` 查重，命中即复用已存在记录，并对其**补写**本次携带的子任务（幂等重推不重复建主任务，但子任务不会丢） |
| complete     | `PUT` 本身幂等；状态/完成时间覆盖写，无副作用                                                                                              |
| defer        | 同上                                                                                                                                       |
| ai_breakdown | 同上（`base_ms` 冲突检测 + 覆盖写 AI拆解/检查清单/AI建议 三字段后写入子任务）                                                              |

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
手动「立即同步」/ 启动时        →  full_pull（list_tasks + replace_tasks + 重置游标 + 子任务全量刷）
45s 定时器 / 写操作触发         →  drain_queue + incremental_pull
                                     （search 修改时间 > cursor + 子任务全量刷）
                                     失败 / 无游标 → 回退 full_pull
```

实现：[sync.py](../client_windows/app/services/sync.py)

---

## 9. AI 模块（M3 新增）

### 调用链

```
UI（AI 建任务 / 详情页 AI 拆解按钮）
    │  QThread（AIInvoker，界面线程不做 IO）
    ▼
parser.parse_task / breakdown_task（编排：重试 1 次 + 兜底）
    │
    ├─ prompts.py   构造系统/用户提示词（含当前时间，硬编码学术诚信红线）
    └─ provider.py  DeepSeekProvider → POST {base_url}/chat/completions
                    model=deepseek-chat，response_format=json_object，temperature=0.2
```

- Key 从 `config/.env` 的 `DEEPSEEK_API_KEY` 读取，不入库、不打日志；
- 超时默认 60 秒（`settings.yaml ai.timeout_seconds`），可被 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 覆盖；
- **成本控制**：只在「AI 建任务 / 手动点 AI 拆解」时调用，无任何定时轮询；
- 每次调用记录 token 用量（日志 + 供应商累计计数 + 界面展示）。

### 输出契约（JSON Schema，对齐设计总纲 5.4）

```json
{
  "title": "交高数作业 第三章习题1-10",
  "priority": "P1",
  "task_type": "作业",
  "due_at": "2026-10-08T18:00:00+08:00",
  "estimate_min": 90,
  "energy": "中",
  "tags": ["数学"],
  "subtasks": ["复习第三章", "完成习题1-5"],
  "checklist": ["核对答案"],
  "confidence": 0.85
}
```

拆解建议（详情页）输出 `subtasks / checklist / advice / confidence`。

### 校验与容错（parser.py）

| 环节      | 策略                                                                                                                |
| --------- | ------------------------------------------------------------------------------------------------------------------- |
| JSON 提取 | 兼容剥离 Markdown 围栏；顶层非对象报错                                                                              |
| 字段钳制  | priority/type/energy 非法值回落默认（P2/其他/空）；estimate 钳 0~1440；confidence 钳 0~1                            |
| due_at    | 支持 ISO 8601（带时区转本地）与 `YYYY-MM-DD[ HH:MM]`；解析不了置 None                                               |
| 失败重试  | 网络错误与 Schema 校验失败都重试 1 次                                                                               |
| 兜底      | 解析两次失败 → `title_only_draft`（只取标题、confidence=0、写入收集箱），程序不崩；拆解无规则兜底，失败直接提示用户 |

### 置信度策略

`confidence < ai.confidence_threshold（默认 0.6）` 时：

- 状态写「**收集箱**」并在 UI 红色横幅提示待人工确认；
- 优先级**不采纳 AI 判断**，强制回落 P2。

### 写回路径

- **AI 建任务**：与手动新建同链路（离线队列 `create` op）；payload 额外携带 `subtasks`，推送阶段主任务落表后由 `_create_subtasks_safe` 写入子任务表并按「所属任务」关联（子任务表未配置/写入失败仅记日志，不影响主任务）。
- **AI 拆解采纳**：新队列 op `ai_breakdown`（复用完成/延期的冲突检测基准 `base_ms`），推送时更新 `AI拆解/检查清单/AI建议` 三字段并写子任务。
- AI 只产出建议文本，**绝不自动改任务状态**；采纳与否由用户点击决定。

实现：[ai/provider.py](../client_windows/app/ai/provider.py)、[ai/prompts.py](../client_windows/app/ai/prompts.py)、[ai/parser.py](../client_windows/app/ai/parser.py)、[services/sync.py](../client_windows/app/services/sync.py)

---

## 10. 后续将新增的接口（预告，当前未实现）

- 批量接口：`.../records/batch_create`、`batch_update`
- 发送消息：`POST /open-apis/im/v1/messages`
- 卡片回调：事件订阅 `card.action.trigger`
- 自建服务端 REST：`/tasks`、`/sync/changes`（M6）

---

## 11. 娱乐监督模块（M4 已实现）

### 11.1 监督数据流

```
前台窗口（Win32 psutil+pywin32）  ┐
                                  ├─→ MonitorWorker 5 秒采样 ─→ 状态机决策
浏览器扩展（MV3 content_script） ┘            │
                                              ├─ L1/L2/L3 → Notifier.notify + speak
                                              ├─ L4 → Overlay 全屏遮罩 / KillTab 关网页
                                              └─ 会话结束 → 聚合入队 → SyncWorker.push_monitor_logs → 飞书日志表
```

### 11.2 本地 HTTP API（仅本机回环 127.0.0.1:8765）

无鉴权，仅供浏览器扩展与本机客户端通信。

| 方法 | 路径          | 用途                                                | body                             |
| ---- | ------------- | --------------------------------------------------- | -------------------------------- |
| GET  | `/health`     | 扩展探测客户端存活（心跳）                          | —                                |
| POST | `/report`     | 扩展上报当前活跃标签                                | `{domain, title, seconds, ts}`   |
| POST | `/close-tab`  | 客户端记录关标签请求（扩展下次 `/poll-close` 取走） | `{domain}`                       |
| POST | `/poll-close` | 扩展轮询取走一条关标签请求                          | — 返回 `{ok, request: {domain}}` |

### 11.3 升级阈值与动作

| 级别 | 默认触发（分钟） | 动作                                                                          |
| ---- | ---------------- | ----------------------------------------------------------------------------- |
| L1   | 10               | 系统通知                                                                      |
| L2   | 20               | 系统通知 + TTS 语音                                                           |
| L3   | 30               | 再次通知 + 日志备注                                                           |
| L4   | 45               | `monitor.l4_action` 配置：`overlay`（全屏遮罩） \| `kill_tab`（强制关闭网页） |

阈值可在 `config/settings.yaml` 的 `monitor.escalation` 段覆盖；动作 `monitor.l4_action` 二选一。

### 11.4 强制关闭网页（kill_tab）双路径

1. 扩展在线（30 秒内有过 `/health`、`/report` 或 `/poll-close`）：客户端 `request_close_tab(domain)` 把请求塞进本地 API 队列，扩展下次轮询 `/poll-close` 时取走，经 `chrome.runtime.sendMessage` 交给 background.js 执行 `chrome.tabs.query({url:"*://domain/*"}) + chrome.tabs.remove`，只关匹配标签。
2. 扩展离线：降级 `pywin32.EnumWindows + GetWindowText`，按窗口标题含 domain 关键词 `PostMessage(WM_CLOSE)`，关匹配窗口（可能关整个浏览器，不杀进程）。

> 内容脚本无权访问 `chrome.tabs`，关标签必须经 `chrome.runtime.sendMessage` 交给 background service worker 执行。

### 11.5 娱乐监控日志表字段映射

由 `app/feishu/repository.build_monitor_log_fields()` 组装，写入由 `app/services/sync.SyncService.push_monitor_logs()` 推送。

| 飞书字段 | 类型     | 来源字段           | 说明                                   |
| -------- | -------- | ------------------ | -------------------------------------- |
| 时间     | 日期时间 | `occurred_at`      | 会话结束时刻，毫秒时间戳               |
| 进程名   | 文本     | `process_name`     | 如 `chrome.exe`                        |
| 窗口标题 | 文本     | `window_title`     | 取停留时间最长的样本                   |
| URL      | 超链接   | `url`              | 域名，写入为 `{"text","link"}` 对象    |
| 持续时长 | 数字     | `duration_seconds` | 秒                                     |
| 是否提醒 | 复选框   | `notified`         | `fired_count > 0` 即 true              |
| 提醒次数 | 数字     | `notify_count`     | 触发过的级别数                         |
| 是否关闭 | 复选框   | `closed`           | 遮罩被用户点关闭后置 true              |
| 备注     | 多行文本 | `note`             | 收尾原因（whitelist/idle/paused/stop） |

### 11.6 缓存表 schema（SQLite）

| 表                     | 用途                                                                                     |
| ---------------------- | ---------------------------------------------------------------------------------------- |
| `monitor_session`      | 单行，当前活跃娱乐会话状态（level/fired_count/closed/started_at/last_sample_at/payload） |
| `pending_monitor_logs` | 待推送飞书的聚合日志队列，建记录即删                                                     |

幂等：日志表只新增不更新，建记录即删本地行，无冲突检测。

### 11.7 娱乐命中判定（白名单 + 黑名单）

前台窗口先过白名单（`monitor.whitelist_titles` / `whitelist_domains`，命中即豁免），
再走 `monitor_rules.is_distraction()` 黑名单判定：**必须显式命中**以下任一维度才计入娱乐时长，避免任意窗口被误判。

| 维度       | 配置项                        | 匹配方式                               |
| ---------- | ----------------------------- | -------------------------------------- |
| 域名       | `monitor.blacklist_domains`   | 扩展上报域名包含关键词（大小写不敏感） |
| 标题关键词 | `monitor.blacklist_keywords`  | 前台窗口标题包含关键词                 |
| 进程名     | `monitor.blacklist_processes` | 前台进程名完全相等                     |

三个列表全为空时监督不生效。扩展上报的域名**只在前台进程是浏览器时采信**，且标题与域名**取自同一次上报**（避免「标题=飞书、URL=bilibili」错配）；上报超过 15 秒（`_REPORT_TTL_SECONDS`）即视为过期。

### 11.7.1 统一计时器与容忍窗口（M4 修正）

**所有娱乐平台共用一个会话与计时器**：会话 `started_at` 只在首次进入娱乐时设定，
在不同娱乐平台（抖音/bilibili/…）间切换**不清零**，累计为同一个会话。

**容忍窗口**：短暂切走（查资料、回消息）时**不立即结束会话**，而是在
`monitor.grace_seconds`（默认 60 秒）内保持「grace」状态：

- 窗口内切回娱乐：**沿用原会话**继续累计，不新建、不重复写日志；
- 窗口内离开的时间计入 `away_seconds`，从总时长中扣除（日志时长与升级计时口径一致）；
- 超过窗口仍未回到娱乐：才真正结束会话，聚合成**一条**日志。

这样保证「来回切换多个娱乐平台」只产出一条聚合记录。每次采样刷新 `last_sample_at`，
修复长会话因时间戳不更新而被误判为陈旧、计时器被重置的问题。

### 11.8 配置示例

见 `config/settings.yaml` 中 `monitor` 段；表 ID 在 `config/.env` 的 `FEISHU_MONITOR_LOG_TABLE_ID`，不填则仅本地缓存不入飞书。
