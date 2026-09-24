"""人形机器人结构化试验数据的基础组件。"""

from .contracts import Observation, Protocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .numeric import NumericSummary, WilsonInterval
from .service import TrialService
from .timequality import (
    TIME_STATUS_NORMAL,
    TIME_STATUS_PENDING,
    TIME_STATUS_REJECTED,
    ObservationTimeError,
    classify_observation_time,
    normalize_observed_at,
    parse_observed_at,
)

__all__ = [
    "NumericSummary",
    "Observation",
    "ObservationTimeError",
    "Protocol",
    "TIME_STATUS_NORMAL",
    "TIME_STATUS_PENDING",
    "TIME_STATUS_REJECTED",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "TrialService",
    "analyze",
    "bootstrap_mean_interval",
    "classify_observation_time",
    "normalize_observed_at",
    "parse_observed_at",
]

__version__ = "0.1.0"
