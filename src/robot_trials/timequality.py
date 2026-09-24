"""观测时间的时间感知数据质量：迟到分类与可配置宽限边界。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# 分类取值
NORMAL = "normal"
LATE_PENDING = "late_pending"
REJECTED = "rejected"

# 分类原因代码（稳定取值，供报告与测试断言）
REASON_BEFORE_BATCH_START = "before_batch_start"
REASON_FUTURE_TIMESTAMP = "future_timestamp"
REASON_AFTER_SEAL_WITHIN_GRACE = "after_seal_within_grace"
REASON_BEYOND_LATE_GRACE = "beyond_late_grace"

# 默认与上限：迟到宽限 24 小时，未来容忍 5 分钟，最长 30 天
DEFAULT_LATE_GRACE_SECONDS = 24 * 60 * 60
DEFAULT_FUTURE_TOLERANCE_SECONDS = 5 * 60
MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60


def classify_observation(
    observed_at: datetime,
    *,
    started_at: datetime,
    sealed_at: datetime | None,
    now: datetime,
    late_grace_seconds: int,
    future_tolerance_seconds: int,
) -> tuple[str, str | None]:
    """依据批次开始、封存与宽限边界对观测时间分类。

    判定顺序固定，保证幂等重放得到相同分类：
    1. 早于批次开始 -> rejected/before_batch_start；
    2. 超出未来容忍 -> rejected/future_timestamp；
    3. 批次已封存且晚于封存时刻：宽限内 -> late_pending，超出宽限 -> rejected；
    4. 其余 -> normal。
    所有比较先统一到 UTC，边界本身（恰好等于）视为在内侧。
    """

    if observed_at.tzinfo is None or observed_at.tzinfo.utcoffset(observed_at) is None:
        raise ValueError("观测时间必须带时区")
    observed = observed_at.astimezone(timezone.utc)
    start = started_at.astimezone(timezone.utc)
    current = now.astimezone(timezone.utc)
    if observed < start:
        return REJECTED, REASON_BEFORE_BATCH_START
    if observed > current + timedelta(seconds=future_tolerance_seconds):
        return REJECTED, REASON_FUTURE_TIMESTAMP
    if sealed_at is not None:
        sealed = sealed_at.astimezone(timezone.utc)
        if observed > sealed:
            if observed <= sealed + timedelta(seconds=late_grace_seconds):
                return LATE_PENDING, REASON_AFTER_SEAL_WITHIN_GRACE
            return REJECTED, REASON_BEYOND_LATE_GRACE
    return NORMAL, None
