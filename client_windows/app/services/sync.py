"""同步服务：离线队列推送 + 全量/增量拉取 + 冲突检测。

职责（M2）：
1. push_pending：按序把 pending_ops 推送到飞书，成功即删，冲突时保留并返回给 UI 确认；
2. full_pull：拉取全表并重建本地缓存（首次/手动/异常回退）；
3. incremental_pull：按"修改时间"游标做增量同步，失败回退全量。

约束：
- 所有网络请求都在调用方提供的后台线程执行（本模块只是纯调用，不带线程）；
- 任何单条失败不影响后续 op 的继续推送；
- 幂等：create 推送前先按 external_id 查重。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from ..feishu.client import FeishuClient
from ..feishu.repository import (
    Task,
    create_task,
    find_by_external_id,
    get_remote_modified_ms,
    list_tasks,
    to_task,
    update_task,
)
from ..constants import TaskFields
from .cache import LocalCache, PendingOp


class SyncService:
    """任务写回与增量同步（线程无关，纯调用）。"""

    # 缓存中增量游标的键名
    CURSOR_KEY = "pull_cursor_ms"

    def __init__(
        self,
        client: FeishuClient,
        app_token: str,
        table_id: str,
        cache: LocalCache,
    ):
        self._client = client
        self._app_token = app_token
        self._table_id = table_id
        self._cache = cache

    # ------------------------------------------------------------------
    # 推送离线队列
    # ------------------------------------------------------------------
    def push_pending(self) -> tuple[int, list[dict[str, Any]], str | None]:
        """把队列中的全部待推送操作按顺序推到飞书。

        返回 (成功数, 冲突列表, 最后一条错误消息)。
        冲突项 dict 含 op_id/op_type/title/record_id，由 UI 弹窗让用户决定覆盖或放弃。
        """
        success = 0
        conflicts: list[dict[str, Any]] = []
        last_error: str | None = None
        for op in self._cache.list_pending_ops():
            try:
                ok, conflict = self._push_one(op)
            except Exception as e:
                # 单条异常不影响后续；保留该 op 等待下次推送
                # 但要把错误透传出去，避免 UI 一直显示"待推送"却看不到原因
                last_error = f"[{op.op_type}] {e}"
                logger.warning("推送失败 op=%s: %s", op.op_id, e, exc_info=True)
                continue
            if conflict is not None:
                conflicts.append(conflict)
            elif ok:
                self._cache.delete_op(op.op_id)
                success += 1
        return success, conflicts, last_error

    def _push_one(self, op: PendingOp) -> tuple[bool, dict[str, Any] | None]:
        """推送单条 op。

        返回 (是否成功, 冲突信息)。冲突时不删 op，由上层决定。
        """
        payload = op.payload
        op_type = op.op_type

        if op_type == "create":
            external_id = payload.get("external_id", "")
            fields = payload.get("fields", {})
            # 幂等：先按 external_id 查重，命中即视为成功
            if external_id:
                existing = find_by_external_id(
                    self._client, self._app_token, self._table_id, external_id
                )
                if existing is not None:
                    return True, None
            create_task(self._client, self._app_token, self._table_id, fields)
            return True, None

        if op_type in ("complete", "defer"):
            record_id = payload.get("record_id", "")
            fields = payload.get("fields", {})
            base_ms = int(payload.get("base_ms", 0))
            title = payload.get("title", "")

            # 冲突检测：远端修改时间比 op 记录的基准新，且用户未确认覆盖
            if not op.force and base_ms > 0:
                remote_ms = get_remote_modified_ms(
                    self._client, self._app_token, self._table_id, record_id
                )
                if remote_ms > base_ms:
                    return False, {
                        "op_id": op.op_id,
                        "op_type": op_type,
                        "title": title,
                        "record_id": record_id,
                    }
            update_task(self._client, self._app_token, self._table_id, record_id, fields)
            return True, None

        # 未知类型：直接丢弃，避免阻塞队列
        return True, None

    # ------------------------------------------------------------------
    # 拉取
    # ------------------------------------------------------------------
    def full_pull(self) -> list[Task]:
        """全量拉取并重建本地缓存。返回最新任务列表。"""
        tasks = list_tasks(self._client, self._app_token, self._table_id)
        self._cache.replace_tasks(tasks)
        # 重置增量游标为本次拉到的最大修改时间
        cursor = max((t.modified_ms for t in tasks), default=0)
        self._cache.set_meta(self.CURSOR_KEY, str(cursor))
        return tasks

    def incremental_pull(self) -> list[Task]:
        """按游标增量拉取。失败/无游标时回退全量。返回最新缓存任务列表。"""
        cursor_str = self._cache.get_meta(self.CURSOR_KEY, "")
        if not cursor_str:
            return self.full_pull()
        try:
            cursor = int(cursor_str)
        except ValueError:
            return self.full_pull()

        try:
            conditions = [
                {
                    "field_name": TaskFields.UPDATED_AT,
                    "operator": "isGreater",
                    "value": ["ExactDate", str(cursor)],
                }
            ]
            records = self._client.search_records(
                self._app_token, self._table_id, conditions
            )
            changed = [to_task(r) for r in records]
        except Exception:
            # 增量失败（search 不可用/限流/权限），回退全量
            return self.full_pull()

        if changed:
            self._cache.upsert_tasks(changed)
            new_cursor = max(cursor, max(t.modified_ms for t in changed))
            self._cache.set_meta(self.CURSOR_KEY, str(new_cursor))
        return self._cache.load_tasks()
