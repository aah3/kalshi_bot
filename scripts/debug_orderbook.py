import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credentials.credential_manager import CredentialManager
from discovery.market_client import MarketClient
from execution.rate_limiter import RateLimiter


async def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "KXPGATOP20-PGC26-SLOW"
    creds = CredentialManager()
    limiter = RateLimiter()
    async with MarketClient(creds, limiter) as mc:
        raw = await mc._get(f"/markets/{ticker}/orderbook", params={"depth": 5})
        print(json.dumps(raw, indent=2))
        book = await mc.get_order_book(ticker)
        print("parsed:", book)


if __name__ == "__main__":
    asyncio.run(main())
