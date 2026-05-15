"""
metrics/settlement.py

Settlement watcher — detects market resolutions and reconciles open positions.

────────────────────────────────────────────────────────────────────────────────
WHAT IT DOES
────────────────────────────────────────────────────────────────────────────────

Kalshi resolves markets by setting their status to "settled" and paying out
$1.00 per contract to the winning side. The resolution can happen:
  - At a scheduled close time
  - Early if the outcome is already determined

This module runs as a background asyncio task, polling every
SETTLEMENT_POLL_INTERVAL_SECONDS (default: 5 minutes). On each poll it:

  1. Fetches all open parent trades from the blotter
  2. Calls GET /markets/{ticker} for each unique ticker
  3. If a market's status is "settled" or "finalized":
     a. Reads the result field ("yes" | "no" | "void")
     b. Computes settlement P&L for every open leg
     c. Writes settlement_pnl_cents and closes the leg in the blotter
     d. Closes the parent trade and rolls up totals
     e. Emits a RISK_BREACH-level log (settlement is significant)
     f. Updates the account equity snapshot in MetricsStore

  4. Supports manual override: force_settle(trade_id, resolution) lets
     the trader manually mark a position settled from the CLI if auto-detect
     misses it or if they want to record an early exit at a specific price.

────────────────────────────────────────────────────────────────────────────────
P&L AT SETTLEMENT
────────────────────────────────────────────────────────────────────────────────

For each leg:
    If resolution matches held side:
        settlement_pnl = (100 - entry_price) × contracts - fees_cents
        (you paid entry_price, collected 100 → profit = 100 - entry_price)

    If resolution does NOT match held side:
        settlement_pnl = -entry_price × contracts - fees_cents
        (you paid entry_price, collected 0 → loss = -entry_price)

    If resolution = "void":
        settlement_pnl = 0 - fees_cents
        (Kalshi refunds the contract cost; you only lose the fee)

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────

    watcher = SettlementWatcher(blotter, credentials, rate_limiter, metrics_store)

    # Start as background task (from main.py)
    task = asyncio.create_task(watcher.run())

    # Manual override from CLI
    await watcher.force_settle("T-0042", resolution="yes")

    # One-shot check (e.g. at startup or shutdown)
    await watcher.check_now()
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import BucketType, RateLimiter
from logging_.structured_logger import logger
from metrics.blotter import Blotter
from metrics.metrics_store import MetricsStore


# ── Settlement result ─────────────────────────────────────────────────────────

class SettlementResult:
    """Returned by _settle_trade() with full audit details."""
    __slots__ = (
        "trade_id", "ticker", "resolution", "legs_settled",
        "total_settlement_pnl_cents", "success",
    )

    def __init__(
        self,
        trade_id: str,
        ticker: str,
        resolution: str,
        legs_settled: int,
        total_settlement_pnl_cents: int,
        success: bool,
    ) -> None:
        self.trade_id                   = trade_id
        self.ticker                     = ticker
        self.resolution                 = resolution
        self.legs_settled               = legs_settled
        self.total_settlement_pnl_cents = total_settlement_pnl_cents
        self.success                    = success


# ── Watcher ───────────────────────────────────────────────────────────────────

class SettlementWatcher:
    """
    Background task that polls Kalshi for market resolutions and
    reconciles open positions in the blotter automatically.
    """

    def __init__(
        self,
        blotter:       Blotter,
        credentials:   CredentialManager,
        rate_limiter:  RateLimiter,
        metrics_store: MetricsStore | None = None,
    ) -> None:
        self._blotter       = blotter
        self._creds         = credentials
        self._limiter       = rate_limiter
        self._metrics_store = metrics_store
        self._session: aiohttp.ClientSession | None = None
        self._running       = False

        # Cache of tickers we've already confirmed as settled this session
        # (avoids re-processing after first detection)
        self._settled_cache: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main background loop. Runs until cancelled.
        Wire into main.py:
            asyncio.create_task(watcher.run(), name="settlement_watcher")
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS)
        )
        self._running = True
        logger.info(
            "SettlementWatcher started",
            poll_interval_seconds=config.SETTLEMENT_POLL_INTERVAL_SECONDS,
        )
        try:
            while self._running:
                await self.check_now()
                await asyncio.sleep(config.SETTLEMENT_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("SettlementWatcher cancelled")
        finally:
            if self._session:
                await self._session.close()

    async def stop(self) -> None:
        self._running = False

    # ── One-shot check ────────────────────────────────────────────────────────

    async def check_now(self) -> list[SettlementResult]:
        """
        Immediately scan all open positions for resolved markets.
        Returns a list of SettlementResult for every trade settled this call.
        """
        open_trades = self._blotter.open_positions_summary()
        if not open_trades:
            return []

        # Unique tickers (one market may have multiple open trades)
        unique_tickers = {t["ticker"] for t in open_trades
                          if t["ticker"] not in self._settled_cache}
        if not unique_tickers:
            return []

        # Fetch market status for each unique ticker
        market_statuses = await self._fetch_market_statuses(list(unique_tickers))

        results: list[SettlementResult] = []
        for trade in open_trades:
            ticker   = trade["ticker"]
            trade_id = trade["trade_id"]
            status   = market_statuses.get(ticker, {})

            market_status = status.get("status", "open")
            resolution    = status.get("result")   # "yes" | "no" | "void" | None

            if market_status not in ("settled", "finalized") or not resolution:
                continue

            # Market has resolved — settle all open legs for this trade
            result = await self._settle_trade(trade_id, ticker, resolution)
            results.append(result)

            if result.success:
                self._settled_cache.add(ticker)

                # Log alert (visible in structured log at WARNING level)
                logger.warning(
                    "SETTLEMENT DETECTED — position closed automatically",
                    trade_id=trade_id,
                    ticker=ticker,
                    resolution=resolution,
                    legs_settled=result.legs_settled,
                    settlement_pnl_cents=result.total_settlement_pnl_cents,
                    settlement_pnl_usd=round(result.total_settlement_pnl_cents / 100, 2),
                    won=(resolution == "yes"),
                )

                # Record equity snapshot if metrics store is available
                if self._metrics_store:
                    try:
                        bal = await self._fetch_balance_cents()
                        if bal is not None:
                            self._metrics_store.record_equity_snapshot(bal)
                    except Exception as exc:
                        logger.error(f"Failed to record equity after settlement: {exc}")

        return results

    # ── Manual override ───────────────────────────────────────────────────────

    async def force_settle(
        self,
        trade_id:   str,
        resolution: str,    # "yes" | "no" | "void"
        exit_price: int | None = None,
    ) -> SettlementResult | None:
        """
        Manually mark a trade as settled.

        Used when:
          - Auto-detect missed the resolution
          - You want to record an early manual exit at a specific price
          - Demo mode (where resolution may not propagate via API)

        Args:
            trade_id:   The parent trade ID (e.g. "T-0042")
            resolution: "yes" | "no" | "void"
            exit_price: Override the settlement price (cents).
                        Defaults to 100 for winning side, 0 for losing.
        """
        trade = self._blotter.get_trade(trade_id)
        if not trade:
            logger.error("force_settle: trade not found", trade_id=trade_id)
            return None

        result = await self._settle_trade(
            trade_id=trade_id,
            ticker=trade.ticker,
            resolution=resolution,
            force_exit_price=exit_price,
        )

        logger.warning(
            "MANUAL SETTLEMENT recorded",
            trade_id=trade_id,
            ticker=trade.ticker,
            resolution=resolution,
            settlement_pnl_usd=round(result.total_settlement_pnl_cents / 100, 2),
        )
        return result

    # ── Settlement logic ──────────────────────────────────────────────────────

    async def _settle_trade(
        self,
        trade_id:         str,
        ticker:           str,
        resolution:       str,
        force_exit_price: int | None = None,
    ) -> SettlementResult:
        """
        Close every open leg of a trade with settlement P&L.

        P&L per leg:
            resolution matches leg side  →  payout = 100c/contract
            resolution doesn't match     →  payout = 0c/contract
            resolution = "void"          →  payout = entry_price (full refund)
        """
        legs        = self._blotter.get_legs_for_trade(trade_id)
        open_legs   = [l for l in legs if l.status == "open"]
        total_pnl   = 0
        settled     = 0

        for leg in open_legs:
            # Determine exit price for this leg
            if force_exit_price is not None:
                exit_price = force_exit_price
            elif resolution == "void":
                exit_price = leg.entry_price   # full refund
            elif resolution == leg.side:
                exit_price = 100               # winner collects $1
            else:
                exit_price = 0                 # loser collects $0

            pnl = self._blotter.close_leg(
                leg_id=leg.leg_id,
                exit_price=exit_price,
                close_type="settlement",
                resolution=resolution,
            )
            total_pnl += pnl
            settled   += 1

        # Roll up the parent record
        if settled > 0:
            self._blotter.close_trade(trade_id=trade_id, resolution=resolution)

        return SettlementResult(
            trade_id=trade_id,
            ticker=ticker,
            resolution=resolution,
            legs_settled=settled,
            total_settlement_pnl_cents=total_pnl,
            success=settled > 0,
        )

    # ── API helpers ───────────────────────────────────────────────────────────

    async def _fetch_market_statuses(
        self,
        tickers: list[str],
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch status + result for a list of tickers.
        Returns {ticker: {"status": ..., "result": ...}}
        """
        results = {}
        sem = asyncio.Semaphore(5)

        async def _fetch_one(ticker: str) -> None:
            async with sem:
                path    = f"/markets/{ticker}"
                headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")
                async with self._limiter.throttle(BucketType.READ):
                    try:
                        resp = await self._session.get(
                            f"{config.BASE_URL}{path}", headers=headers
                        )
                        if resp.status == 200:
                            data   = await resp.json()
                            market = data.get("market", data)
                            results[ticker] = {
                                "status": market.get("status", "open"),
                                "result": market.get("result"),
                            }
                        elif resp.status == 404:
                            logger.warning("Settlement: market not found", ticker=ticker)
                        else:
                            logger.warning(
                                "Settlement: unexpected status fetching market",
                                ticker=ticker,
                                http_status=resp.status,
                            )
                    except Exception as exc:
                        logger.error(f"Settlement: error fetching {ticker}: {exc}")

        await asyncio.gather(*[_fetch_one(t) for t in tickers])
        return results

    async def _fetch_balance_cents(self) -> int | None:
        """Fetch current account balance in cents for equity snapshot."""
        path    = "/portfolio/balance"
        headers = self._creds.sign_request("GET", f"/trade-api/v2{path}")
        async with self._limiter.throttle(BucketType.READ):
            try:
                resp = await self._session.get(f"{config.BASE_URL}{path}", headers=headers)
                if resp.status == 200:
                    data = await resp.json()
                    return int(data.get("balance", 0))
            except Exception as exc:
                logger.error(f"Settlement: error fetching balance: {exc}")
        return None
