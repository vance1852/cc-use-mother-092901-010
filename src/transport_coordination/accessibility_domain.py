"""无障碍设施整改与复开领域的枚举、分级和时限规则。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# 设施类别
FACILITY_TYPES = frozenset({
    "elevator",        # 无障碍电梯
    "ramp",            # 坡道
    "lift",            # 轮椅升降平台
    "toilet",          # 无障碍卫生间
    "slipway",         # 出入口缘石坡道
    "guidance_path",   # 盲道 / 引导路径
    "parking_bay",     # 无障碍停车位
    "other",
})

# 受影响人群
AFFECTED_GROUPS = frozenset({
    "wheelchair",        # 轮椅使用者
    "mobility_aid",      # 助行器/拐杖
    "visual_impaired",   # 视障旅客
    "hearing_impaired",  # 听障旅客
    "elderly",           # 老年旅客
    "families_infants",  # 携婴幼儿家庭
    "temporary_injury",  # 临时伤病
})

# 风险等级：低、中、高、紧急
RISK_LEVELS = ("low", "medium", "high", "critical")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}

# 临时措施类型
INTERIM_KINDS = frozenset({
    "staff_assist",   # 工作人员协助
    "alt_route",      # 替代通道
    "replacement",    # 替代设备（如爬楼机）
    "escort",         # 专人护送
})

# 各风险等级对应的响应、修复和临时措施期限（小时）
SLA_LIMITS_HOURS: dict[str, dict[str, int]] = {
    "low":      {"response": 24, "repair": 168, "interim": 72},
    "medium":   {"response": 8,  "repair": 72,  "interim": 48},
    "high":     {"response": 2,  "repair": 24,  "interim": 12},
    "critical": {"response": 1,  "repair": 4,   "interim": 2},
}

# 达到哪些影响组合时，风险必须升级
HIGH_IMPACT_GROUPS = frozenset({"wheelchair", "mobility_aid", "temporary_injury"})

FACILITY_STATUS_IN_SERVICE = "in_service"
FACILITY_STATUS_OUT_OF_SERVICE = "out_of_service"

EVENT_INITIAL = "initial"
EVENT_RECURRENCE = "recurrence"
EVENT_EVIDENCE_WITHDRAWN = "evidence_withdrawn"

EVENT_OPEN = "open"
EVENT_IN_PROGRESS = "in_progress"
EVENT_AWAITING = "awaiting_reinspection"
EVENT_CLOSED = "closed"

WO_OPEN = "open"
WO_IN_PROGRESS = "in_progress"
WO_COMPLETED = "completed"
WO_REJECTED = "rejected"
WO_TRANSFERRED = "transferred"

MEASURE_ACTIVE = "active"
MEASURE_EXPIRED = "expired"
MEASURE_HANDED_OVER = "handed_over"

EVIDENCE_VALID = "valid"
EVIDENCE_WITHDRAWN = "withdrawn"
EVIDENCE_PASSED = "passed"
EVIDENCE_FAILED = "failed"


def is_facility_type(value: str) -> bool:
    return value in FACILITY_TYPES


def is_risk_level(value: str) -> bool:
    return value in RISK_RANK


def effective_risk(declared: str, impact_groups: set[str]) -> tuple[str, bool]:
    """根据申报等级与受影响人群计算生效风险，返回(等级, 是否被自动升级)。"""

    if not is_risk_level(declared):
        raise ValueError("risk_level 不合法")
    level = declared
    escalated = False
    if impact_groups & HIGH_IMPACT_GROUPS and RISK_RANK[level] < RISK_RANK["high"]:
        level = "high"
        escalated = True
    return level, escalated


def deadline(base: datetime, risk_level: str, kind: str) -> datetime:
    """按风险等级计算响应/修复/临时措施到期时刻。"""

    return base + timedelta(hours=SLA_LIMITS_HOURS[risk_level][kind])


def parse_iso(value: str, field: str) -> datetime:
    """解析服务内部使用的 ISO8601 时间（结尾允许 Z）。"""

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须带时区")
    return parsed.astimezone()


def normalize_sla(raw: Any) -> dict[str, dict[str, int]]:
    """把合同 SLA 归一为各风险等级 response/repair/interim 三个小时数，缺省取标准时限。"""

    sla = raw if isinstance(raw, dict) else {}
    limits: dict[str, dict[str, int]] = {}
    for level in RISK_LEVELS:
        entry = sla.get(level)
        entry = entry if isinstance(entry, dict) else {}
        limits[level] = {
            "response": int(entry.get("response", SLA_LIMITS_HOURS[level]["response"])),
            "repair": int(entry.get("repair", SLA_LIMITS_HOURS[level]["repair"])),
            "interim": int(entry.get("interim", SLA_LIMITS_HOURS[level]["interim"])),
        }
        if any(limits[level][key] <= 0 for key in ("response", "repair", "interim")):
            raise ValueError("SLA 时限必须为正整数")
    return limits
