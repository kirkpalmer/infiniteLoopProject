"""Abstract base class for all InfiniteLoop 0DTE spread strategies.
Implements the classify_direction + get_spread_params interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class StrategyMetadata:
    name: str
    version: int
    description: str
    created_at: str
    params: dict
    scorecard: dict


class BaseStrategy(ABC):
    @abstractmethod
    def classify_direction(self, data: pd.DataFrame) -> str:
        """Return 'UP', 'DOWN', 'NEUTRAL', or 'SKIP'."""

    @abstractmethod
    def get_spread_params(self) -> dict:
        """Return spread parameters for the current strategy."""

    @abstractmethod
    def get_params(self) -> dict:
        """Return the full parameter dict for serialization."""

    @abstractmethod
    def get_name(self) -> str:
        """Return the unique strategy name."""
