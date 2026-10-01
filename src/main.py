"""Main Execution Entrypoint for Binance Crypto Options Greeks & Candlesticks Actor."""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, Any, List
from apify import Actor

from src.binance_client import BinanceClient
from src.greeks_engine import BinanceGreeksEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("binance-options-greeks-actor")


async def main() -> None:
    """Actor main routine."""
    async with Actor:
        actor_input = await Actor.get_input() or {}

        raw_currencies = actor_input.get("currencies", ["BTC", "ETH", "SOL"])
        if isinstance(raw_currencies, str):
            currencies_list = [c.strip().upper() for c in raw_currencies.split(",") if c.strip()]
        else:
            currencies_list = [str(c).strip().upper() for c in raw_currencies if str(c).strip()]

        if not currencies_list:
            currencies_list = ["BTC", "ETH", "SOL"]

        interval = str(actor_input.get("candlestickInterval", "1d")).strip().lower()
        limit = int(actor_input.get("candlestickLimit", 30))
        max_expirations = int(actor_input.get("maxExpirationsPerCurrency", 4))
        include_candles = bool(actor_input.get("includeCandlesticks", True))

        logger.info("Starting Binance Crypto Options Greeks & Candlesticks Actor...")
        logger.info(f"Target Currencies: {currencies_list}, Interval: {interval}, Max Expirations: {max_expirations}")

        client = BinanceClient()
        engine = BinanceGreeksEngine(client)

        all_results: List[Dict[str, Any]] = []
        dataset_records: List[Dict[str, Any]] = []

        for currency in currencies_list:
            logger.info(f"Fetching Binance data for {currency}...")

            # 1. Klines Candlesticks from Binance
            candles = []
            if include_candles:
                candles = client.get_klines(
                    currency=currency,
                    interval=interval,
                    limit=limit
                )
                logger.info(f"Retrieved {len(candles)} OHLCV candlesticks for {currency} from Binance.")

            # 2. Options Chains & Greeks
            processed_data = engine.process_currency_options(
                currency=currency,
                max_expirations=max_expirations
            )

            all_records = processed_data.pop("allRecords", [])
            dataset_records.extend(all_records)

            all_results.append({
                "currency": currency,
                "underlyingPriceUsd": processed_data["underlyingPriceUsd"],
                "candlesticksCount": len(candles),
                "candlesticks": candles,
                "expirationsCount": processed_data["expirationsCount"],
                "chains": processed_data["chains"]
            })

        # Push to Apify Default Dataset
        if dataset_records:
            logger.info(f"Pushing {len(dataset_records)} strike options records to Apify dataset...")
            await Actor.push_data(dataset_records)

        # Save comprehensive summary to Key-Value Store (OUTPUT)
        output_payload = {
            "title": "Binance Crypto Options Greeks, Candlesticks & Open Interest Report",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "currenciesAnalyzed": len(all_results),
            "totalOptionStrikes": len(dataset_records),
            "results": all_results
        }
        await Actor.set_value("OUTPUT", output_payload)

        logger.info(
            f"Actor completed successfully! Processed {len(all_results)} Binance crypto assets "
            f"across {len(dataset_records)} total live option strike records."
        )


if __name__ == "__main__":
    asyncio.run(main())
