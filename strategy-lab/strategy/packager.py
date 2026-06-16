"""Serialize and package strategy objects for persistence."""

from __future__ import annotations

import logging

from .base import StrategyMetadata

LOGGER = logging.getLogger("infiniteloop.strategy.packager")


def pack_strategy(strategy, version: int = 1, description: str = "", scorecard: dict | None = None) -> StrategyMetadata:
    """Convert a strategy instance into serializable metadata."""

    metadata = StrategyMetadata(
        name=strategy.get_name(),
        version=version,
        description=description,
        created_at="",
        params=strategy.get_params(),
        scorecard=scorecard or {},
    )
    LOGGER.info("Packed strategy %s v%s", metadata.name, metadata.version)
    return metadata
