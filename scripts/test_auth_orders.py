"""Probe Kalshi auth: balance (portfolio) vs orderbook (market)."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import config
from credentials.credential_manager import CredentialManager
from execution.rate_limiter import RateLimiter
from discovery.market_client import MarketClient


async def main() -> None:
    creds = CredentialManager()
    limiter = RateLimiter()
    ticker = sys.argv[1] if len(sys.argv) > 1 else "KXPGATOP20-PGC26-SLOW"
    print("ENV", config.ENV, "BASE", config.BASE_URL)
    print("KEY", creds.api_key_id[:8] + "...")

    import aiohttp

    async with aiohttp.ClientSession() as session:
        # balance
        path = "/portfolio/balance"
        sign_path = f"/trade-api/v2{path}"
        headers = creds.sign_request("GET", sign_path)
        async with session.get(f"{config.BASE_URL}{path}", headers=headers) as r:
            text = await r.text()
            print(f"GET balance: {r.status} {text[:200]}")

        # orderbook
        path2 = f"/markets/{ticker}/orderbook"
        sign2 = f"/trade-api/v2{path2}"
        headers2 = creds.sign_request("GET", sign2)
        async with session.get(
            f"{config.BASE_URL}{path2}", headers=headers2, params={"depth": 3}
        ) as r:
            print(f"GET orderbook: {r.status} (auth)")

        async with session.get(
            f"{config.BASE_URL}{path2}", params={"depth": 3}
        ) as r:
            print(f"GET orderbook: {r.status} (no auth)")

        # POST signature probe (invalid price → 400 is OK; 401 = bad signing)
        import uuid
        order_path = "/portfolio/orders"
        sign_order = f"/trade-api/v2{order_path}"
        body = json.dumps(
            {
                "ticker": ticker,
                "action": "buy",
                "side": "yes",
                "count": 1,
                "type": "limit",
                "yes_price": 1,
                "client_order_id": str(uuid.uuid4()),
            },
            separators=(",", ":"),
        )
        headers3 = creds.sign_request("POST", sign_order, body)
        headers3["Content-Type"] = "application/json"
        async with session.post(
            f"{config.BASE_URL}{order_path}", headers=headers3, data=body
        ) as r:
            text3 = await r.text()
            print(f"POST order (probe): {r.status} {text3[:200]}")

    async with MarketClient(creds, limiter) as mc:
        book = await mc.get_order_book(ticker)
        print("parsed book", book.best_bid if book else None, book.best_ask if book else None)


if __name__ == "__main__":
    asyncio.run(main())
