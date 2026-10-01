"""Quantitative Engine for Binance Options Chains, Greeks & Volume Distribution."""

import logging
import math
from datetime import datetime, date, timezone
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
from scipy.stats import norm

from src.binance_client import BinanceClient

logger = logging.getLogger(__name__)

class BinanceGreeksEngine:
    """Processes Binance live options chains into unified strike-level Greeks & OI/Volume records."""

    def __init__(self, client: BinanceClient):
        self.client = client

    def process_currency_options(
        self,
        currency: str,
        max_expirations: int = 4
    ) -> Dict[str, Any]:
        """Process complete live options chains across active expirations for a Binance crypto asset."""
        clean_curr = currency.upper().replace('-USD', '').replace('-USDT', '').replace('^', '').strip()
        spot_price = self.client.get_spot_price(clean_curr)
        raw_options = self.client.get_options_data(clean_curr)

        if not raw_options:
            logger.warning(f"No active Binance options contracts found for {clean_curr}")
            return {
                "currency": clean_curr,
                "underlyingPriceUsd": spot_price,
                "expirationsCount": 0,
                "chains": []
            }

        # Group options by Expiration Date (Binance symbol date is YYMMDD e.g. 260626)
        expirations_map: Dict[str, List[Dict[str, Any]]] = {}
        today = date.today()

        for opt in raw_options:
            sym = opt.get('symbol', '')
            match = self.client.symbol_regex.search(sym)
            if not match:
                continue

            _, date_str, strike_str, opt_type = match.groups()
            try:
                exp_date_obj = datetime.strptime(date_str, "%y%m%d").date()
                if exp_date_obj < today:
                    continue
                exp_iso = exp_date_obj.strftime("%Y-%m-%d")
            except ValueError:
                continue

            if exp_iso not in expirations_map:
                expirations_map[exp_iso] = []
            expirations_map[exp_iso].append({
                **opt,
                "_strike": float(strike_str),
                "_type": opt_type,
                "_exp_date_obj": exp_date_obj
            })

        sorted_expirations = sorted(list(expirations_map.keys()))[:max_expirations]
        logger.info(f"Processing {len(sorted_expirations)} Binance expirations for {clean_curr}: {sorted_expirations}")

        chains_results = []
        all_strike_records = []

        for exp_str in sorted_expirations:
            opts_for_exp = expirations_map[exp_str]
            exp_date_obj = opts_for_exp[0]["_exp_date_obj"]
            days_to_exp = max((exp_date_obj - today).days, 0.25)
            T = days_to_exp / 365.0
            r = 0.045
            sqrt_T = math.sqrt(max(T, 0.0001))
            discount = math.exp(-r * T)

            # Map options by Strike
            calls_by_strike: Dict[float, Dict[str, Any]] = {}
            puts_by_strike: Dict[float, Dict[str, Any]] = {}

            for opt in opts_for_exp:
                s = opt["_strike"]
                if opt["_type"] == 'C':
                    calls_by_strike[s] = opt
                else:
                    puts_by_strike[s] = opt

            all_strikes = sorted(list(set(calls_by_strike.keys()) | set(puts_by_strike.keys())))

            tot_call_oi, tot_put_oi = 0.0, 0.0
            tot_call_vol, tot_put_vol = 0.0, 0.0
            chain_strike_records = []

            for strike in all_strikes:
                c_opt = calls_by_strike.get(strike, {})
                p_opt = puts_by_strike.get(strike, {})

                c_mark = c_opt.get('markOptions', {})
                p_mark = p_opt.get('markOptions', {})

                c_price = float(c_opt.get('lastPrice') or c_mark.get('markPrice') or 0.0)
                p_price = float(p_opt.get('lastPrice') or p_mark.get('markPrice') or 0.0)

                c_iv = float(c_mark.get('markIV') or c_mark.get('askIV') or 0.0) * 100.0
                p_iv = float(p_mark.get('markIV') or p_mark.get('askIV') or 0.0) * 100.0
                avg_iv = (c_iv + p_iv) / 2.0 if (c_iv > 0 and p_iv > 0) else max(c_iv, p_iv, 50.0)
                sigma = avg_iv / 100.0

                c_oi = float(c_opt.get('amount') or c_opt.get('volume') or 0.0)
                p_oi = float(p_opt.get('amount') or p_opt.get('volume') or 0.0)
                c_vol = float(c_opt.get('volume') or 0.0)
                p_vol = float(p_opt.get('volume') or 0.0)

                tot_call_oi += c_oi
                tot_put_oi += p_oi
                tot_call_vol += c_vol
                tot_put_vol += p_vol

                # Delta from Binance or analytical Black-Scholes
                c_delta = float(c_mark.get('delta') or 0.0)
                p_delta = float(p_mark.get('delta') or 0.0)
                gamma_val = float(c_mark.get('gamma') or p_mark.get('gamma') or 0.0)
                vega_val = float(c_mark.get('vega') or p_mark.get('vega') or 0.0)
                theta_c_val = float(c_mark.get('theta') or 0.0)
                theta_p_val = float(p_mark.get('theta') or 0.0)

                # Higher-Order Greeks computation
                forward = spot_price * math.exp(r * T)
                try:
                    if sigma > 0 and strike > 0 and forward > 0:
                        d1 = (math.log(forward / strike) + (0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
                        d2 = d1 - sigma * sqrt_T
                        pdf_d1 = norm.pdf(d1)
                        if gamma_val == 0.0:
                            gamma_val = float((discount * pdf_d1) / (forward * sigma * sqrt_T))
                        if vega_val == 0.0:
                            vega_val = float(forward * discount * pdf_d1 * sqrt_T / 100.0)
                        if theta_c_val == 0.0:
                            theta_c_val = float((-(forward * discount * pdf_d1 * sigma) / (2.0 * sqrt_T) - r * c_price) / 365.0)
                        if theta_p_val == 0.0:
                            theta_p_val = float((-(forward * discount * pdf_d1 * sigma) / (2.0 * sqrt_T) - r * p_price) / 365.0)

                        vanna = float(-discount * pdf_d1 * (d2 / sigma))
                        charm_call = float(discount * pdf_d1 * ((r / (sigma * sqrt_T)) - (d2 / (2.0 * T))) + r * c_delta)
                        speed = float(-(gamma_val / forward) * ((d1 / (sigma * sqrt_T)) + 1.0))
                        vomma = float((vega_val * d1 * d2) / sigma)
                        color = float(-gamma_val * ((r / (sigma * sqrt_T)) + ((1.0 - d1 * d2) / (2.0 * T))))
                    else:
                        vanna, charm_call, speed, vomma, color = 0.0, 0.0, 0.0, 0.0, 0.0
                except Exception:
                    vanna, charm_call, speed, vomma, color = 0.0, 0.0, 0.0, 0.0, 0.0

                if c_delta == 0.0 and strike > 0 and sigma > 0:
                    c_delta = float(norm.cdf(d1))
                if p_delta == 0.0 and strike > 0 and sigma > 0:
                    p_delta = float(-norm.cdf(-d1))

                dec = 2 if spot_price > 10 else 4

                record = {
                    "currency": clean_curr,
                    "underlyingPriceUsd": round(spot_price, dec),
                    "expirationDate": exp_str,
                    "daysToExpiration": days_to_exp,
                    "strike": round(strike, dec),
                    "callLastPriceUsd": round(c_price, dec),
                    "putLastPriceUsd": round(p_price, dec),
                    "callMarkIvPct": round(c_iv, 2),
                    "putMarkIvPct": round(p_iv, 2),
                    "callDelta": round(c_delta, 4),
                    "putDelta": round(p_delta, 4),
                    "gamma": round(gamma_val, 6),
                    "vega": round(vega_val, 4),
                    "callTheta": round(theta_c_val, 4),
                    "putTheta": round(theta_p_val, 4),
                    "vanna": round(vanna, 6),
                    "charmCall": round(charm_call, 6),
                    "speed": round(speed, 8),
                    "vomma": round(vomma, 6),
                    "color": round(color, 8),
                    "callOpenInterest": round(c_oi, 2),
                    "putOpenInterest": round(p_oi, 2),
                    "netOpenInterest": round(c_oi - p_oi, 2),
                    "callVolume24h": round(c_vol, 2),
                    "putVolume24h": round(p_vol, 2),
                    "netVolume24h": round(c_vol - p_vol, 2),
                    "callBreakevenUsd": round(strike + c_price, dec),
                    "putBreakevenUsd": round(max(strike - p_price, 0.0), dec)
                }
                chain_strike_records.append(record)
                all_strike_records.append(record)

            pcr_oi = round(tot_put_oi / tot_call_oi, 2) if tot_call_oi > 0 else 1.0
            pcr_vol = round(tot_put_vol / tot_call_vol, 2) if tot_call_vol > 0 else 1.0

            chains_results.append({
                "currency": clean_curr,
                "expirationDate": exp_str,
                "daysToExpiration": days_to_exp,
                "totalCallOpenInterest": round(tot_call_oi, 2),
                "totalPutOpenInterest": round(tot_put_oi, 2),
                "netOpenInterest": round(tot_call_oi - tot_put_oi, 2),
                "totalCallVolume24h": round(tot_call_vol, 2),
                "totalPutVolume24h": round(tot_put_vol, 2),
                "netVolume24h": round(tot_call_vol - tot_put_vol, 2),
                "putCallRatioOI": pcr_oi,
                "putCallRatioVolume": pcr_vol,
                "strikesCount": len(chain_strike_records),
                "records": chain_strike_records
            })

        return {
            "currency": clean_curr,
            "underlyingPriceUsd": round(spot_price, 2),
            "expirationsCount": len(chains_results),
            "chains": chains_results,
            "allRecords": all_strike_records
        }
