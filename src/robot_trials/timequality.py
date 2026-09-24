"""观测时间解析、规范化与批次窗口分类。

观测时间一律要求携带时区偏移的 ISO 8601 时间戳，解析后统一归一化为 UTC
存储；分类结果在导入时刻确定并持久化，保证幂等重放返回相同结论。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .clock import isoformat


TIME_STATUS_NORMAL = "normal"
TIME_STATUS_PENDING = "pending_review"
TIME_STATUS_REJECTED = "rejected"
TIME_STATUSES = (TIME_STATUS_NORMAL, TIME_STATUS_PENDING, TIME_STATUS_REJECTED)

REASON_FUTURE = "future_timestamp"
REASON_BEFORE_START = "before_batch_start"
REASON_LATE_WITHIN_GRACE = "late_within_grace"
REASON_BEYOND_GRACE = "beyond_grace_period"


class ObservationTimeError(ValueError):
    """观测时间缺失、无法解析或不携带时区。"""


def parse_observed_at(raw: object, path: str = "observation.observed_at") -> datetime:
    """把提交值解析为带时区的 UTC 时间；朴素时间和非法格式一律拒绝。"""

    if not isinstance(raw, str) or not raw.strip():
        raise ObservationTimeError(f"{path} 必须是非空字符串")
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ObservationTimeError(f"{path} 不是可解析的 ISO 8601 时间: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservationTimeError(f"{path} 必须携带时区偏移: {text!r}")
    return parsed.astimezone(timezone.utc)


def normalize_observed_at(raw: object, path: str = "observation.observed_at") -> str:
    """返回统一存储的规范化 UTC 文本。"""

    return isoformat(parse_observed_at(raw, path))


def classify_observation_time(
    observed: datetime,
    *,
    started_at: datetime,
    sealed_at: datetime | None,
    grace: timedelta,
    now: datetime,
    future_tolerance: timedelta = timedelta(0),
) -> tuple[str, str | None]:
    """按批次开始、封存和宽限边界对观测时间分类。

    边界均为闭区间：恰好落在批次开始、封存时刻或宽限终点视为有效一侧；
    恰好等于当前时刻（加容忍）不视为未来时间。分类优先级：未来时间 >
    批次开始前 > 封存后宽限判断。
    """

    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ObservationTimeError("观测时间必须携带时区")
    if observed > now + future_tolerance:
        return TIME_STATUS_REJECTED, REASON_FUTURE
    if observed < started_at:
        return TIME_STATUS_REJECTED, REASON_BEFORE_START
    if sealed_at is not None:
        if observed <= sealed_at:
            return TIME_STATUS_NORMAL, None
        if observed <= sealed_at + grace:
            return TIME_STATUS_PENDING, REASON_LATE_WITHIN_GRACE
        return TIME_STATUS_REJECTED, REASON_BEYOND_GRACE
    return TIME_STATUS_NORMAL, None
