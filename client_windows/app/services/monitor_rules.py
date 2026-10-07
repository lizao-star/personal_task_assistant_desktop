"""娱乐监督纯函数模块（M4）。

包含阈值判断、动作选择与会话聚合三类无 IO 纯函数。
全部基于「规则 + 数据」决策，不调用任何 AI，便于复现与单元测试。

调用方（MonitorWorker）持有这些函数的输出，再决定是否发 Qt 信号、是否操作 UI。
本模块不接触 Qt / 网络 / 文件系统。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..constants import (
    DEFAULT_ESCALATION_MINUTES,
    MONITOR_ACTION_KILL_TAB,
    MONITOR_ACTION_OVERLAY,
    MONITOR_LEVEL_L1,
    MONITOR_LEVEL_L2,
    MONITOR_LEVEL_L3,
    MONITOR_LEVEL_L4,
    MONITOR_LEVEL_NONE,
)


# ----------------------------------------------------------------------
# 豁免判断
# ----------------------------------------------------------------------
def is_exempt(
    title: str,
    domain: str,
    whitelist_titles: list[str],
    whitelist_domains: list[str],
) -> bool:
    """判断当前窗口标题/域名是否命中白名单。

    命中白名单即视为「非娱乐」，会话不计入、不提醒。
    匹配规则：标题/域名任一字段包含白名单关键词（大小写不敏感）。
    """
    title_lower = (title or "").lower()
    domain_lower = (domain or "").lower()

    for kw in whitelist_titles or []:
        if kw and kw.lower() in title_lower:
            return True
    for kw in whitelist_domains or []:
        if kw and kw.lower() in domain_lower:
            return True
    return False


# ----------------------------------------------------------------------
# 娱乐命中判断（黑名单）
# ----------------------------------------------------------------------
def is_distraction(
    title: str,
    domain: str,
    process_name: str,
    blacklist_domains: list[str],
    blacklist_keywords: list[str],
    blacklist_processes: list[str],
) -> bool:
    """判断当前前台窗口是否属于「娱乐」（黑名单命中才计时）。

    必须显式命中黑名单之一才算娱乐，避免把记事本、资源管理器等
    任意非白名单窗口都误判为娱乐：
    - 域名包含 blacklist_domains 任一关键词，或
    - 窗口标题包含 blacklist_keywords 任一关键词，或
    - 进程名命中 blacklist_processes。
    匹配大小写不敏感；列表为空表示该维度不参与判断。
    """
    title_lower = (title or "").lower()
    domain_lower = (domain or "").lower()
    process_lower = (process_name or "").lower()

    for kw in blacklist_domains or []:
        if kw and kw.lower() in domain_lower:
            return True
    for kw in blacklist_keywords or []:
        if kw and kw.lower() in title_lower:
            return True
    for kw in blacklist_processes or []:
        if kw and kw.lower() == process_lower:
            return True
    return False


# ----------------------------------------------------------------------
# 等级判断
# ----------------------------------------------------------------------
def pick_level(elapsed_seconds: int, thresholds_minutes: dict[int, int]) -> int:
    """根据已持续秒数返回当前应到的级别（0=无，1~4=L1~L4）。

    thresholds_minutes 形如 {1: 10, 2: 20, 3: 30, 4: 45}（分钟）。
    返回的是「达到的最高级别」，不是增量。
    """
    minutes = elapsed_seconds / 60
    level = MONITOR_LEVEL_NONE
    # 按级别从低到高判断，命中阈值即提升
    for lv in (MONITOR_LEVEL_L1, MONITOR_LEVEL_L2, MONITOR_LEVEL_L3, MONITOR_LEVEL_L4):
        threshold = thresholds_minutes.get(lv)
        if threshold is not None and minutes >= threshold:
            level = lv
    return level


def normalize_thresholds(
    escalation_cfg: dict[str, Any] | None,
) -> dict[int, int]:
    """把 settings.yaml 中的 escalation 字典规范化为 {level: 分钟}。

    缺失项使用 constants.DEFAULT_ESCALATION_MINUTES 兜底。
    """
    cfg = escalation_cfg or {}
    return {
        MONITOR_LEVEL_L1: int(cfg.get("l1_minutes", DEFAULT_ESCALATION_MINUTES[MONITOR_LEVEL_L1])),
        MONITOR_LEVEL_L2: int(cfg.get("l2_minutes", DEFAULT_ESCALATION_MINUTES[MONITOR_LEVEL_L2])),
        MONITOR_LEVEL_L3: int(cfg.get("l3_minutes", DEFAULT_ESCALATION_MINUTES[MONITOR_LEVEL_L3])),
        MONITOR_LEVEL_L4: int(cfg.get("l4_minutes", DEFAULT_ESCALATION_MINUTES[MONITOR_LEVEL_L4])),
    }


# ----------------------------------------------------------------------
# 动作决策
# ----------------------------------------------------------------------
# 动作类型常量（字符串，便于调用方 switch）
ACTION_NONE = "none"            # 无事可做
ACTION_NOTIFY = "notify"        # 触发通知/语音
ACTION_ESCALATE = "escalate"    # 触发 L4 动作（遮挡或关网页）
ACTION_FINISH = "finish"        # 会话结束，需聚合写日志


def decide_action(
    cur_level: int,
    target_level: int,
    fired_levels: set[int],
    closed: bool,
) -> dict[str, Any]:
    """决定本帧要执行的动作。

    参数：
        cur_level: 上一帧已达到的级别（MONITOR_LEVEL_NONE~L4）
        target_level: 本帧根据持续时长应到的级别
        fired_levels: 已经触发过提醒的级别集合（避免每个级别重复提醒）
        closed: 会话是否已被标记为关闭（L4 遮罩点过按钮后置 True）

    返回 dict：
        action: ACTION_NONE / ACTION_NOTIFY / ACTION_ESCALATE / ACTION_FINISH
        level: 触发的级别（notify/escalate 时有效）
    """
    # 会话已被用户关闭标记，且当前级别回落到 0：收尾
    if target_level == MONITOR_LEVEL_NONE:
        if cur_level != MONITOR_LEVEL_NONE or closed:
            return {"action": ACTION_FINISH, "level": MONITOR_LEVEL_NONE}
        return {"action": ACTION_NONE, "level": MONITOR_LEVEL_NONE}

    # 达到 L4：触发遮挡/关网页（只触发一次，避免反复遮挡）
    if target_level >= MONITOR_LEVEL_L4 and MONITOR_LEVEL_L4 not in fired_levels:
        return {"action": ACTION_ESCALATE, "level": MONITOR_LEVEL_L4}

    # 升级到新级别（L1/L2/L3）：发提醒
    if target_level > cur_level and target_level not in fired_levels:
        # L4 已被 fired 时不再 escalate；这里只处理 L1~L3 通知
        if target_level < MONITOR_LEVEL_L4:
            return {"action": ACTION_NOTIFY, "level": target_level}

    return {"action": ACTION_NONE, "level": target_level}


# ----------------------------------------------------------------------
# L4 动作选择
# ----------------------------------------------------------------------
def pick_l4_action(l4_action_cfg: str) -> str:
    """把配置中的 l4_action 规范化为可识别的常量。

    非法值兜底为 overlay（更保守、不主动关窗口）。
    """
    if l4_action_cfg == MONITOR_ACTION_KILL_TAB:
        return MONITOR_ACTION_KILL_TAB
    return MONITOR_ACTION_OVERLAY


# ----------------------------------------------------------------------
# 会话聚合
# ----------------------------------------------------------------------
def aggregate_session(
    samples: list[dict[str, Any]],
    started_at: datetime,
    ended_at: datetime,
    fired_count: int,
    closed: bool,
    note: str = "",
    away_seconds: int = 0,
) -> dict[str, Any]:
    """把一次娱乐会话的采样列表聚合成一条日志记录字段。

    samples: 每个采样为 {timestamp, process_name, title, domain, seconds}
    away_seconds: 容忍窗口内离开娱乐的累计秒数，从总时长中扣除，
                  保证与升级计时器口径一致。
    返回 dict，调用方可直接传给 build_monitor_log_fields。
    """
    if not samples:
        return {
            "occurred_at": ended_at,
            "duration_seconds": 0,
            "process_name": "",
            "window_title": "",
            "url": "",
            "notified": fired_count > 0,
            "notify_count": fired_count,
            "closed": closed,
            "note": note,
        }

    # 取停留时间最长的样本作为代表（domain/title/process_name）
    longest = max(samples, key=lambda s: s.get("seconds", 0))
    # 总持续时长 = 会话起止差扣除离开娱乐的时间（更准）；
    # samples 中 seconds 是每次采样停留秒数，不直接累加
    raw = int((ended_at - started_at).total_seconds())
    if raw <= 0:
        # 兜底：起止差为 0（极端快进快出）时用采样 seconds 求和
        duration = sum(int(s.get("seconds", 0)) for s in samples)
    else:
        # 扣除容忍窗口内离开娱乐的时间，结果不小于 0
        duration = max(0, raw - max(0, int(away_seconds)))

    return {
        "occurred_at": ended_at,
        "duration_seconds": duration,
        "process_name": longest.get("process_name", ""),
        "window_title": longest.get("title", ""),
        "url": longest.get("domain", ""),
        "notified": fired_count > 0,
        "notify_count": fired_count,
        "closed": closed,
        "note": note,
    }
