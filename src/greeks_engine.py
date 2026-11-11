"""Quantitative Engine for Binance Options Chains, Greeks & Volume Distribution."""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Any
from scipy.stats import norm

from src.binance_client import BinanceClient

logger = logging.getLogger(__name__)


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _first_positive(*vals: Any) -> float:
    for v in vals:
        f = _to_float(v, 0.0)
        if f > 0.0:
            return f
    return 0.0


def _has_value(mapping: Dict[str, Any], key: str) -> bool:
    return isinstance(mapping, dict) and mapping.get(key) not in (None, "")


class BinanceGreeksEngine:
    """Processes Binance live options chains into unified strike-level Greeks & OI/Volume records."""

    def __init__(self, client: BinanceClient):
        self.client = client

    def process_currency_options(
        self,
        currency: str,
        max_expirations: int = 4,
        include_oi: bool = True,
    ) -> Dict[str, Any]:
        """Process complete live options chains across active expirations for a Binance crypto asset."""
        clean_curr = BinanceClient._clean_currency(currency)
        raw_options = self.client.get_options_data(clean_curr)
        spot_price = self.client.get_spot_price(clean_curr)
        dec = 2 if spot_price >= 10 else 4

        if not raw_options:
            logger.warning(f"No active Binance options contracts found for {clean_curr}")
            return {
                "currency": clean_curr,
                "underlyingPriceUsd": round(spot_price, dec),
                "expirationsCount": 0,
                "chains": [],
                "allRecords": [],
            }

        exchange_info = self.client.get_exchange_info()
        now_utc = datetime.now(timezone.utc)

        # Group options by Expiration Date (Binance symbol date is YYMMDD and contracts settle at 08:00 UTC)
        expirations_map: Dict[str, List[Dict[str, Any]]] = {}
        expiry_code_map: Dict[str, str] = {}
        expiry_dt_map: Dict[str, datetime] = {}

        for opt in raw_options:
            sym = opt.get('symbol', '')
            match = self.client.symbol_regex.search(sym)
            if not match:
                continue

            _, date_str, strike_str, opt_type = match.groups()
            try:
                sym_meta = exchange_info.get(sym, {})
                expiry_ms = sym_meta.get('expiryDate')
                if expiry_ms:
                    exp_dt_utc = datetime.fromtimestamp(float(expiry_ms) / 1000.0, tz=timezone.utc)
                else:
                    parsed_d = datetime.strptime(date_str, "%y%m%d")
                    exp_dt_utc = datetime(parsed_d.year, parsed_d.month, parsed_d.day, 8, 0, 0, tzinfo=timezone.utc)

                if exp_dt_utc < now_utc:
                    continue
                exp_iso = exp_dt_utc.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue

            if exp_iso not in expirations_map:
                expirations_map[exp_iso] = []
                expiry_code_map[exp_iso] = date_str
                expiry_dt_map[exp_iso] = exp_dt_utc

            expirations_map[exp_iso].append({
                **opt,
                "_strike": float(strike_str),
                "_type": opt_type,
                "_exp_dt_utc": exp_dt_utc,
            })

        sorted_expirations = sorted(expirations_map.keys())[:max_expirations]
        logger.info(f"Processing {len(sorted_expirations)} Binance expirations for {clean_curr}: {sorted_expirations}")

        chains_results = []
        all_strike_records = []

        for exp_str in sorted_expirations:
            opts_for_exp = expirations_map[exp_str]
            exp_dt_utc = expiry_dt_map[exp_str]
            days_to_exp = round(max((exp_dt_utc - now_utc).total_seconds() / 86400.0, 0.01), 2)
            T = max(days_to_exp / 365.0, 1e-6)
            sqrt_T = math.sqrt(T)

            # Real Open Interest per contract from /eapi/v1/openInterest (not ticker quote turnover `amount`)
            exp_yymmdd = expiry_code_map[exp_str]
            oi_map = self.client.get_open_interest(clean_curr, exp_yymmdd) if include_oi else {}

            # Map options by Strike
            calls_by_strike: Dict[float, Dict[str, Any]] = {}
            puts_by_strike: Dict[float, Dict[str, Any]] = {}

            for opt in opts_for_exp:
                s = opt["_strike"]
                if opt["_type"] == 'C':
                    calls_by_strike[s] = opt
                else:
                    puts_by_strike[s] = opt

            all_strikes = sorted(set(calls_by_strike.keys()) | set(puts_by_strike.keys()))

            tot_call_oi, tot_put_oi = 0.0, 0.0
            tot_call_vol, tot_put_vol = 0.0, 0.0
            chain_strike_records = []

            for strike in all_strikes:
                c_opt = calls_by_strike.get(strike, {})
                p_opt = puts_by_strike.get(strike, {})

                c_mark = c_opt.get('markOptions', {}) or {}
                p_mark = p_opt.get('markOptions', {}) or {}

                # Check positive > 0 because Binance sends string "0" when no recent trades occurred
                c_price = _first_positive(c_opt.get('lastPrice'), c_mark.get('markPrice'))
                p_price = _first_positive(p_opt.get('lastPrice'), p_mark.get('markPrice'))

                c_iv = _first_positive(c_mark.get('markIV'), c_mark.get('askIV'), c_mark.get('bidIV')) * 100.0
                p_iv = _first_positive(p_mark.get('markIV'), p_mark.get('askIV'), p_mark.get('bidIV')) * 100.0
                avg_iv = (c_iv + p_iv) / 2.0 if (c_iv > 0 and p_iv > 0) else max(c_iv, p_iv, 50.0)
                sigma = avg_iv / 100.0

                c_sym = c_opt.get('symbol', '')
                p_sym = p_opt.get('symbol', '')
                c_oi = _to_float(oi_map.get(c_sym), 0.0) if include_oi else 0.0
                p_oi = _to_float(oi_map.get(p_sym), 0.0) if include_oi else 0.0
                c_vol = _to_float(c_opt.get('volume'), 0.0)
                p_vol = _to_float(p_opt.get('volume'), 0.0)

                tot_call_oi += c_oi
                tot_put_oi += p_oi
                tot_call_vol += c_vol
                tot_put_vol += p_vol

                if _has_value(c_mark, 'riskFreeInterest'):
                    r = _to_float(c_mark.get('riskFreeInterest'), 0.0)
                elif _has_value(p_mark, 'riskFreeInterest'):
                    r = _to_float(p_mark.get('riskFreeInterest'), 0.0)
                else:
                    r = 0.045

                discount = math.exp(-r * T)

                # Primary Greeks directly from Binance EAPI /mark where reported
                has_c_delta = _has_value(c_mark, 'delta')
                has_p_delta = _has_value(p_mark, 'delta')
                c_delta = _to_float(c_mark.get('delta'), 0.0)
                p_delta = _to_float(p_mark.get('delta'), 0.0)

                gamma_val = (
                    _to_float(c_mark.get('gamma'), 0.0)
                    if _has_value(c_mark, 'gamma')
                    else _to_float(p_mark.get('gamma'), 0.0)
                )
                vega_val = (
                    _to_float(c_mark.get('vega'), 0.0)
                    if _has_value(c_mark, 'vega')
                    else _to_float(p_mark.get('vega'), 0.0)
                )
                theta_c_val = _to_float(c_mark.get('theta'), 0.0)
                theta_p_val = _to_float(p_mark.get('theta'), 0.0)

                # Higher-Order Greeks & Black-Scholes fallbacks
                forward = spot_price * math.exp(r * T)
                vanna, charm_call, speed, vomma, color = 0.0, 0.0, 0.0, 0.0, 0.0
                try:
                    if sigma > 0 and strike > 0 and forward > 0 and sqrt_T > 0:
                        d1 = (math.log(forward / strike) + (0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
                        d2 = d1 - sigma * sqrt_T
                        pdf_d1 = float(norm.pdf(d1))

                        if not has_c_delta:
                            c_delta = float(norm.cdf(d1))
                        if not has_p_delta:
                            p_delta = float(-norm.cdf(-d1))

                        if gamma_val == 0.0 and not (_has_value(c_mark, 'gamma') or _has_value(p_mark, 'gamma')):
                            gamma_val = float((discount * pdf_d1) / (forward * sigma * sqrt_T))
                        if vega_val == 0.0 and not (_has_value(c_mark, 'vega') or _has_value(p_mark, 'vega')):
                            vega_val = float(forward * discount * pdf_d1 * sqrt_T / 100.0)
                        if theta_c_val == 0.0 and not _has_value(c_mark, 'theta'):
                            theta_c_val = float(
                                (-(forward * discount * pdf_d1 * sigma) / (2.0 * sqrt_T) - r * strike * discount * float(norm.cdf(d2))) / 365.0
                            )
                        if theta_p_val == 0.0 and not _has_value(p_mark, 'theta'):
                            theta_p_val = float(
                                (-(forward * discount * pdf_d1 * sigma) / (2.0 * sqrt_T) + r * strike * discount * float(norm.cdf(-d2))) / 365.0
                            )

                        vanna = float(-discount * pdf_d1 * (d2 / sigma))
                        charm_call = float(discount * pdf_d1 * ((r / (sigma * sqrt_T)) - (d2 / (2.0 * T))) + r * c_delta)
                        speed = float(-(gamma_val / forward) * ((d1 / (sigma * sqrt_T)) + 1.0))
                        vomma = float((vega_val * d1 * d2) / sigma)
                        color = float(-gamma_val * ((r / (sigma * sqrt_T)) + ((1.0 - d1 * d2) / (2.0 * T))))
                except Exception:
                    vanna, charm_call, speed, vomma, color = 0.0, 0.0, 0.0, 0.0, 0.0

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
                    "putBreakevenUsd": round(max(strike - p_price, 0.0), dec),
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
                "records": chain_strike_records,
            })

        return {
            "currency": clean_curr,
            "underlyingPriceUsd": round(spot_price, dec),
            "expirationsCount": len(chains_results),
            "chains": chains_results,
            "allRecords": all_strike_records,
        }
