"""
orchestration/universe_manager.py

Periodic rediscovery + add/drop of watch tickers for a running StrategyInstance.
Never drops tickers with open exposure, resting orders, or blotter inventory.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from discovery.market_client import MarketSummary
from discovery.ticker_selector import (
    TickerCriteria,
    market_filter_rejection,
)
from logging_.structured_logger import logger
from orchestration.strategy_instance import DropAction, StrategyInstance, UniverseConfig
from strategy.base_strategy import BaseStrategy
from strategy.position_limits import ticker_is_protected


DiscoverFn = Callable[[TickerCriteria], Awaitable[tuple[list[str], list[MarketSummary]]]]
RegisterFn = Callable[[list[str]], Awaitable[None]]


@dataclass
class UniverseDiff:
    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    desired: list[str] = field(default_factory=list)


@dataclass
class UniverseManager:
    """
    Owns the active watchlist for one worker.

    ``watching`` starts as the startup ticker list and is updated on each refresh.
    """

    instance: StrategyInstance
    criteria: TickerCriteria | None
    strategy: BaseStrategy
    ingestor: Any  # MarketIngestor
    watching: list[str]
    discover_fn: DiscoverFn | None = None
    register_fn: RegisterFn | None = None
    blotter: Any | None = None
    execution: Any | None = None
    session_monitor: Any | None = None
    on_watchlist_changed: Callable[[list[str]], None] | None = None

    def compute_diff(
        self,
        candidates: list[str],
        markets: list[MarketSummary] | None = None,
    ) -> UniverseDiff:
        """Pure add/drop decision given rediscovery candidates (already ranked)."""
        uni = self.instance.universe
        max_n = uni.max_watchlist
        desired = list(dict.fromkeys(candidates))[:max_n]

        # Hybrid: keep static seeds in desired if still flat-eligible / always
        if uni.mode == "hybrid":
            for t in uni.static_tickers:
                if t not in desired:
                    desired.append(t)
            desired = desired[: max(max_n, len(desired))]

        market_by_ticker = {
            m.ticker: m for m in (markets or []) if getattr(m, "ticker", None)
        }

        protected: list[str] = []
        droppable: list[str] = []
        for ticker in self.watching:
            if self._is_protected(ticker):
                protected.append(ticker)
            else:
                droppable.append(ticker)

        added = [t for t in desired if t not in self.watching]
        dropped: list[str] = []
        retained_extra: list[str] = []
        for ticker in droppable:
            if ticker in desired:
                continue
            reason = self._drop_reason(ticker, desired, market_by_ticker, uni)
            if reason is None:
                continue
            action = self._action_for_reason(reason, uni)
            if action == "keep":
                retained_extra.append(ticker)
                continue
            if action == "drop_if_flat" and self._is_protected(ticker):
                protected.append(ticker)
                continue
            if action in ("drop", "drop_if_flat"):
                dropped.append(ticker)

        new_watch = list(
            dict.fromkeys([*protected, *desired, *retained_extra])
        )

        return UniverseDiff(
            added=added,
            dropped=dropped,
            protected=list(dict.fromkeys(protected)),
            desired=new_watch,
        )

    async def refresh_once(self) -> UniverseDiff:
        if self.criteria is None:
            logger.debug(
                "Universe refresh skipped — no discovery criteria",
                instance_id=self.instance.id,
            )
            return UniverseDiff(desired=list(self.watching))

        if self.discover_fn is None:
            raise RuntimeError("UniverseManager.discover_fn is not set")

        try:
            tickers, markets = await self.discover_fn(self.criteria)
        except Exception as exc:
            logger.warning(
                "Universe rediscovery failed — keeping current watchlist",
                instance_id=self.instance.id,
                error=str(exc),
            )
            return UniverseDiff(desired=list(self.watching))

        diff = self.compute_diff(tickers, markets)
        await self.apply_diff(diff)
        return diff

    async def apply_diff(self, diff: UniverseDiff) -> None:
        instance_id = self.instance.id

        if diff.added:
            self.ingestor.add_tickers(diff.added)
            for ticker in diff.added:
                if hasattr(self.strategy, "add_watch_ticker"):
                    self.strategy.add_watch_ticker(ticker)
            if self.register_fn:
                await self.register_fn(diff.added)
            logger.info(
                "Universe: added tickers",
                instance_id=instance_id,
                tickers=diff.added,
            )

        if diff.dropped:
            removable = [
                t for t in diff.dropped
                if not self._is_protected(t)
            ]
            for ticker in removable:
                if hasattr(self.strategy, "remove_watch_ticker"):
                    self.strategy.remove_watch_ticker(ticker)
            if removable:
                self.ingestor.remove_tickers(removable)
                logger.info(
                    "Universe: dropped tickers",
                    instance_id=instance_id,
                    tickers=removable,
                )

        self.watching = list(diff.desired)
        # Ensure watching matches ingestor after protected overflow
        for t in self.watching:
            if t not in self.ingestor.tickers:
                self.ingestor.add_tickers([t])
                if hasattr(self.strategy, "add_watch_ticker"):
                    self.strategy.add_watch_ticker(t)

        if self.session_monitor is not None and hasattr(
            self.session_monitor, "set_tickers"
        ):
            self.session_monitor.set_tickers(self.watching)

        if self.on_watchlist_changed:
            self.on_watchlist_changed(list(self.watching))

    async def run_loop(self, shutdown_event: asyncio.Event) -> None:
        refresh = self.instance.universe.refresh_seconds
        if refresh is None or refresh <= 0:
            return

        logger.info(
            "Universe refresh loop started",
            instance_id=self.instance.id,
            refresh_seconds=refresh,
        )
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(refresh))
                break
            except asyncio.TimeoutError:
                pass
            await self.refresh_once()

    def _is_protected(self, ticker: str) -> bool:
        open_order_tickers: set[str] = set()
        if self.execution is not None:
            for order in getattr(self.execution, "open_orders", {}).values():
                if not isinstance(order, dict):
                    continue
                t = order.get("ticker") or order.get("market_ticker")
                if t:
                    open_order_tickers.add(t)

        blotter_tickers: set[str] = set()
        if self.blotter is not None:
            try:
                for row in self.blotter.open_positions_summary():
                    t = row.get("ticker")
                    if t:
                        blotter_tickers.add(t)
            except Exception:
                pass

        return ticker_is_protected(
            self.strategy,
            ticker,
            open_order_tickers=open_order_tickers,
            blotter_open_tickers=blotter_tickers,
        )

    def _drop_reason(
        self,
        ticker: str,
        desired: list[str],
        market_by_ticker: dict[str, MarketSummary],
        uni: UniverseConfig,
    ) -> str | None:
        if ticker in desired:
            return None
        market = market_by_ticker.get(ticker)
        if market is not None:
            status = getattr(market, "status", "") or ""
            if status.lower() in ("closed", "settled", "finalized"):
                return "closed"
            if self.criteria is not None:
                rejection = market_filter_rejection(market, self.criteria)
                if rejection:
                    return "fail_filters"
        return "not_in_top_n"

    @staticmethod
    def _action_for_reason(reason: str, uni: UniverseConfig) -> DropAction:
        if reason == "closed":
            return uni.drop_policy.closed
        if reason == "fail_filters":
            return uni.drop_policy.fail_filters
        return uni.drop_policy.not_in_top_n
