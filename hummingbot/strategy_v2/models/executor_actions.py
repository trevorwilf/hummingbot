from decimal import Decimal
from typing import Optional, TypeVar

from pydantic import BaseModel

from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase

ExecutorConfigType = TypeVar("ExecutorConfigType", bound=ExecutorConfigBase)


class ExecutorAction(BaseModel):
    """
    Base class for bot actions.
    """
    controller_id: Optional[str] = "main"


class CreateExecutorAction(ExecutorAction):
    """
    Action to create an executor.
    """
    executor_config: ExecutorConfigType
    # Optional hint for the budget preflight: if a resize would shrink this order below
    # min_fill_ratio * amount, DROP it instead of placing a dust-sized order (the
    # originating controller retries at full size once balances settle). None keeps the
    # legacy resize behavior.
    min_fill_ratio: Optional[Decimal] = None


class StopExecutorAction(ExecutorAction):
    """
    Action to stop an executor.
    """
    executor_id: str
    keep_position: Optional[bool] = False


class StoreExecutorAction(ExecutorAction):
    """
    Action to store an executor.
    """
    executor_id: str
