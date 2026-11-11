"""Main Execution Entrypoint for Binance Crypto Options Greeks & Candlesticks Actor."""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from apify import Actor

from src.binance_client import BinanceClient
from src.greeks_engine import BinanceGreeksEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("binance-options-greeks-actor")


async def resolve_eapi_connection(
    client: BinanceClient,
    proxy_input: Optional[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Ensure a reachable, non-geo-blocked route to Binance Options EAPI (eapi.binance.com)."""
    # 1. Try user-supplied proxy configuration first
    if isinstance(proxy_input, dict) and (proxy_input.get("useApifyProxy") or proxy_input.get("proxyUrls")):
        try:
            proxy_cfg = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
            if proxy_cfg:
                has_country = bool(proxy_input.get("apifyProxyCountry"))
                custom_only = bool(proxy_input.get("proxyUrls")) and not proxy_input.get("useApifyProxy", False)
                max_tries = 1 if (has_country or custom_only) else 2
                for idx in range(max_tries):
                    session_id = f"binance_eapi_{idx}" if max_tries > 1 else None
                    proxy_url = await proxy_cfg.new_url(session_id=session_id)
                    client.set_proxy(proxy_url, verify_ssl=not bool(proxy_cfg.is_man_in_the_middle))
                    ok, status, err = await asyncio.to_thread(client.probe_eapi)
                    if ok:
                        country_label = proxy_input.get("apifyProxyCountry") or ("custom" if custom_only else "AUTO")
                        logger.info(f"Connected to Binance Options EAPI via configured proxy ({country_label}).")
                        return True, f"proxy:{country_label}"
                    logger.warning(f"Configured proxy EAPI check returned HTTP {status}: {err}")
                    if status not in (451, 403, 0):
                        break
        except Exception as e:
            logger.warning(f"Configured proxy initialization failed: {e}")

    # 2. Try direct connection (works automatically when running outside restricted US regions)
    client.set_proxy(None)
    ok, status, err = await asyncio.to_thread(client.probe_eapi)
    if ok:
        logger.info("Connected to Binance Options EAPI directly.")
        return True, "direct"
    logger.warning(f"Direct Binance Options EAPI probe failed (status={status}): {err}")

    # 3. Auto-fallback across non-US Apify Proxy countries when geo-blocked (e.g. HTTP 451 on Apify AWS US)
    if status in (451, 403, 0) and Actor.is_at_home():
        user_groups = proxy_input.get("apifyProxyGroups") if isinstance(proxy_input, dict) else None
        group_candidates = [user_groups] if user_groups else [None, ["RESIDENTIAL"]]
        for groups in group_candidates:
            for cc in ("DE", "NL", "SG", "JP", "FR", "GB"):
                try:
                    cand_cfg = await Actor.create_proxy_configuration(country_code=cc, groups=groups)
                    if not cand_cfg:
                        continue
                    cand_url = await cand_cfg.new_url()
                    client.set_proxy(cand_url, verify_ssl=not bool(cand_cfg.is_man_in_the_middle))
                    cand_ok, cand_status, cand_err = await asyncio.to_thread(client.probe_eapi)
                    if cand_ok:
                        grp_label = "+".join(groups) if groups else "AUTO"
                        logger.info(f"Connected to Binance Options EAPI via Apify Proxy ({cc}, groups={grp_label}).")
                        return True, f"proxy:{cc}:{grp_label}"
                    logger.debug(f"Fallback proxy ({cc}, groups={groups}) returned status={cand_status}: {cand_err}")
                except Exception as proxy_err:
                    logger.debug(f"Fallback proxy ({cc}, groups={groups}) unavailable: {proxy_err}")

    return False, "unavailable"


async def main() -> None:
    """Actor main routine."""
    async with Actor:
        actor_input = await Actor.get_input() or {}

        raw_currencies = actor_input.get("currencies", ["BTC", "ETH", "SOL"])
        if isinstance(raw_currencies, str):
            currencies_list = [c.strip().upper() for c in raw_currencies.split(",") if c.strip()]
        elif isinstance(raw_currencies, list):
            currencies_list = [str(c).strip().upper() for c in raw_currencies if str(c).strip()]
        else:
            currencies_list = ["BTC", "ETH", "SOL"]

        if not currencies_list:
            currencies_list = ["BTC", "ETH", "SOL"]

        raw_interval = str(actor_input.get("candlestickInterval", "1d")).strip()
        interval = "1d" if raw_interval.upper() == "D" else raw_interval.lower()
        if interval not in {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}:
            interval = "1d"

        try:
            limit = max(1, min(1000, int(actor_input.get("candlestickLimit", 30))))
        except (TypeError, ValueError):
            limit = 30

        try:
            max_expirations = max(1, min(20, int(actor_input.get("maxExpirationsPerCurrency", 4))))
        except (TypeError, ValueError):
            max_expirations = 4

        include_candles = bool(actor_input.get("includeCandlesticks", True))
        include_oi = bool(actor_input.get("includeOiVolumeTable", True))
        proxy_input = actor_input.get("proxyConfiguration")

        logger.info("Starting Binance Crypto Options Greeks & Candlesticks Actor...")
        logger.info(
            f"Target Currencies: {currencies_list}, Interval: {interval}, "
            f"Max Expirations: {max_expirations}, Include OI: {include_oi}"
        )

        client = BinanceClient()
        eapi_ok, route_label = await resolve_eapi_connection(client, proxy_input)
        engine = BinanceGreeksEngine(client)

        all_results: List[Dict[str, Any]] = []
        dataset_records: List[Dict[str, Any]] = []

        for currency in currencies_list:
            logger.info(f"Fetching Binance data for {currency}...")

            # 1. Klines Candlesticks from Binance (data-api.binance.vision works globally without geo-block)
            candles = []
            if include_candles:
                candles = await asyncio.to_thread(
                    client.get_klines,
                    currency=currency,
                    interval=interval,
                    limit=limit,
                )
                logger.info(f"Retrieved {len(candles)} OHLCV candlesticks for {currency} from Binance.")

            # 2. Options Chains & Greeks
            if eapi_ok:
                processed_data = await asyncio.to_thread(
                    engine.process_currency_options,
                    currency=currency,
                    max_expirations=max_expirations,
                    include_oi=include_oi,
                )
                all_records = processed_data.pop("allRecords", [])
                dataset_records.extend(all_records)
            else:
                spot = await asyncio.to_thread(client.get_spot_price, currency)
                dec = 2 if spot >= 10 else 4
                processed_data = {
                    "currency": BinanceClient._clean_currency(currency),
                    "underlyingPriceUsd": round(spot, dec),
                    "expirationsCount": 0,
                    "chains": [],
                }

            all_results.append({
                "currency": currency,
                "underlyingPriceUsd": processed_data["underlyingPriceUsd"],
                "candlesticksCount": len(candles),
                "candlesticks": candles,
                "expirationsCount": processed_data["expirationsCount"],
                "chains": processed_data["chains"],
            })

        if dataset_records:
            logger.info(f"Pushing {len(dataset_records)} strike options records to Apify dataset...")
            await Actor.push_data(dataset_records)

        if not eapi_ok:
            status_code = client.last_eapi_status
            err_detail = client.last_eapi_error or "Connection failed"
            if client.eapi_geo_blocked or status_code == 451:
                run_status = "GEO_BLOCKED"
                reason_msg = (
                    "Binance Options EAPI (eapi.binance.com) blocked this IP with HTTP 451 (restricted region, e.g. US AWS). "
                    "Select a non-US country in Proxy Configuration (e.g. Germany DE, Netherlands NL, Singapore SG, Japan JP) or Residential/Custom proxy."
                )
            else:
                run_status = "EAPI_ERROR"
                reason_msg = f"Failed to reach Binance Options EAPI (status={status_code}): {err_detail}"

            output_payload = {
                "title": "Binance Crypto Options Greeks, Candlesticks & Open Interest Report",
                "status": run_status,
                "error": reason_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "currenciesAnalyzed": len(all_results),
                "totalOptionStrikes": len(dataset_records),
                "results": all_results,
            }
            await Actor.set_value("OUTPUT", output_payload)
            logger.error(reason_msg)
            Actor.status_message = reason_msg[:250]
            Actor.exit_code = 1
            return

        output_payload = {
            "title": "Binance Crypto Options Greeks, Candlesticks & Open Interest Report",
            "status": "COMPLETED",
            "eapiRoute": route_label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "currenciesAnalyzed": len(all_results),
            "totalOptionStrikes": len(dataset_records),
            "results": all_results,
        }
        await Actor.set_value("OUTPUT", output_payload)

        logger.info(
            f"Actor completed successfully! Processed {len(all_results)} Binance crypto assets "
            f"across {len(dataset_records)} total live option strike records."
        )


if __name__ == "__main__":
    asyncio.run(main())
