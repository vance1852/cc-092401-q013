"""人形机器人结构化试验数据的基础组件。"""

from .contracts import Observation, Protocol, ValidationError, parse_observed_at
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .numeric import NumericSummary, WilsonInterval
from .service import TrialService
from .timequality import classify_observation

__all__ = [
    "NumericSummary",
    "Observation",
    "Protocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "TrialService",
    "analyze",
    "bootstrap_mean_interval",
    "classify_observation",
    "parse_observed_at",
]

__version__ = "0.1.0"
