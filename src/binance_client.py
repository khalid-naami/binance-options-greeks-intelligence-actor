"""Official Binance Options EAPI and Klines REST Connector."""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
import requests
import urllib3

logger = logging.getLogger(__name__)


class BinanceClient:
    """Official Binance Public REST Endpoints for European Options & Klines."""

    DATA_VISION_BASE = "https://data-api.binance.vision/api/v3"
    EAPI_BASE = "https://eapi.binance.com/eapi/v1"
    SPOT_BASE = "https://api.binance.com/api/v3"
    FUTURES_BASE = "https://fapi.binance.com/fapi/v1"
    US_BASE = "https://api.binance.us/api/v3"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; BinanceOptionsQuantActor/1.0)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        })
        self.proxies: Optional[Dict[str, str]] = None
        self.verify_ssl: bool = True

        # Per-run response caches so multi-currency runs do not re-download global EAPI tables
        self._eapi_ticker_cache: Optional[List[Dict[str, Any]]] = None
        self._eapi_mark_cache: Optional[List[Dict[str, Any]]] = None
        self._eapi_exchange_info_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._oi_cache: Dict[Tuple[str, str], Dict[str, float]] = {}

        # Diagnostic state for transparent reporting on geo-blocks or rate limits
        self.last_eapi_status: Optional[int] = None
        self.last_eapi_error: Optional[str] = None
        self.eapi_geo_blocked: bool = False

        # Binance Option Symbol regex: BTC-260626-140000-C or ETH-260626-3500-P
        self.symbol_regex = re.compile(
            r'^([A-Z0-9]+)-(\d{6})-(\d+(?:\.\d+)?)-([CP])$'
        )

    @staticmethod
    def _clean_currency(currency: str) -> str:
        return currency.upper().replace('-USD', '').replace('-USDT', '').replace('^', '').strip()

    def set_proxy(self, proxy_url: Optional[str], verify_ssl: bool = True) -> None:
        """Configure HTTP(S) proxy for geo-restricted Binance endpoints."""
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self.verify_ssl = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # Reset cached EAPI failures/data when changing egress IP
        self._eapi_ticker_cache = None
        self._eapi_mark_cache = None
        self._eapi_exchange_info_cache = None
        self._oi_cache.clear()
        self.last_eapi_status = None
        self.last_eapi_error = None
        self.eapi_geo_blocked = False

    def _request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
        use_proxy: bool = True,
    ) -> Tuple[Optional[Any], int, str]:
        try:
            res = self.session.get(
                url,
                params=params,
                timeout=timeout,
                proxies=self.proxies if use_proxy else None,
                verify=self.verify_ssl if use_proxy else True,
            )
            if res.status_code == 200:
                return res.json(), 200, ""
            return None, res.status_code, (res.text or "")[:300]
        except Exception as e:
            logger.debug(f"GET failed for {url}: {e}")
            return None, 0, str(e)

    def _fetch_eapi_json(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        url = f"{self.EAPI_BASE}/{endpoint.lstrip('/')}"
        data, status, err = self._request_json(url, params=params, timeout=12, use_proxy=True)
        if status == 200:
            self.last_eapi_status = 200
            self.last_eapi_error = None
            self.eapi_geo_blocked = False
            return data

        # -6010 on openInterest is a per-expiration asset code when OI is empty/unavailable, not an IP block
        is_empty_oi = endpoint == "openInterest" and status == 400 and "-6010" in err
        if is_empty_oi:
            logger.debug(f"Binance EAPI openInterest unavailable for {params}: {err}")
            return None

        self.last_eapi_status = status
        self.last_eapi_error = err or f"HTTP {status}"
        if status in (451, 403) or "restricted location" in err.lower():
            self.eapi_geo_blocked = True
        logger.warning(f"Binance EAPI /{endpoint} failed (status={status}): {err}")
        return None

    def probe_eapi(self) -> Tuple[bool, int, str]:
        """Verify access to Binance Options EAPI and cache the global ticker payload if successful."""
        data, status, err = self._request_json(f"{self.EAPI_BASE}/ticker", timeout=10, use_proxy=True)
        if status == 200 and isinstance(data, list):
            self._eapi_ticker_cache = data
            self.last_eapi_status = 200
            self.last_eapi_error = None
            self.eapi_geo_blocked = False
            return True, 200, "OK"

        self.last_eapi_status = status
        self.last_eapi_error = err or f"HTTP {status}"
        if status in (451, 403) or "restricted location" in err.lower():
            self.eapi_geo_blocked = True
        return False, status, self.last_eapi_error

    def get_exchange_info(self) -> Dict[str, Dict[str, Any]]:
        """Fetch and cache active Binance Options contract metadata keyed by symbol."""
        if self._eapi_exchange_info_cache is not None:
            return self._eapi_exchange_info_cache

        data = self._fetch_eapi_json("exchangeInfo")
        sym_map: Dict[str, Dict[str, Any]] = {}
        if isinstance(data, dict):
            for item in data.get("optionSymbols", []) or []:
                if isinstance(item, dict) and item.get("symbol"):
                    sym_map[item["symbol"]] = item
        self._eapi_exchange_info_cache = sym_map
        return sym_map

    def get_open_interest(self, currency: str, expiration_yymmdd: str) -> Dict[str, float]:
        """Fetch open interest (in base contracts) for all strikes on a currency & YYMMDD expiry."""
        clean = self._clean_currency(currency)
        cache_key = (clean, expiration_yymmdd)
        if cache_key in self._oi_cache:
            return self._oi_cache[cache_key]

        data = self._fetch_eapi_json(
            "openInterest",
            params={"underlyingAsset": clean, "expiration": expiration_yymmdd},
        )
        oi_map: Dict[str, float] = {}
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("symbol"):
                    try:
                        oi_map[item["symbol"]] = float(item.get("sumOpenInterest") or 0.0)
                    except (TypeError, ValueError):
                        pass

        self._oi_cache[cache_key] = oi_map
        return oi_map

    def get_spot_price(self, currency: str) -> float:
        """Fetch current live price for the underlying crypto asset."""
        clean = self._clean_currency(currency)
        pair = f"{clean}USDT"

        # 1. Dedicated global public market data endpoint (no US 451 geo-block)
        for url, use_proxy in (
            (f"{self.DATA_VISION_BASE}/ticker/price?symbol={pair}", False),
            (f"{self.SPOT_BASE}/ticker/price?symbol={pair}", True),
            (f"{self.FUTURES_BASE}/ticker/price?symbol={pair}", True),
            (f"{self.US_BASE}/ticker/price?symbol={pair}", False),
        ):
            data, status, _ = self._request_json(url, timeout=8, use_proxy=use_proxy)
            if status == 200 and isinstance(data, dict) and "price" in data:
                try:
                    price = float(data["price"])
                    if price > 0:
                        return price
                except (TypeError, ValueError):
                    pass

        # 2. Live Options Index price from cached EAPI ticker (covers XAU/XAG or spot outages)
        if self._eapi_ticker_cache:
            prefix = f"{clean}-"
            for t in self._eapi_ticker_cache:
                if isinstance(t, dict) and str(t.get("symbol", "")).startswith(prefix):
                    ex_price = t.get("exercisePrice")
                    if ex_price:
                        try:
                            val = float(ex_price)
                            if val > 0:
                                return val
                        except (TypeError, ValueError):
                            pass

        defaults = {
            'BTC': 75000.0, 'ETH': 2500.0, 'SOL': 90.0, 'BNB': 600.0,
            'DOGE': 0.15, 'XRP': 0.60, 'XAU': 2700.0, 'XAG': 32.0,
        }
        return defaults.get(clean, 100.0)

    def get_options_data(self, currency: str) -> List[Dict[str, Any]]:
        """Fetch all active options tickers and mark Greeks for a currency from Binance EAPI."""
        clean = self._clean_currency(currency)
        prefix = f"{clean}-"

        if self._eapi_ticker_cache is None:
            t_data = self._fetch_eapi_json("ticker")
            if isinstance(t_data, list):
                self._eapi_ticker_cache = t_data

        if self._eapi_mark_cache is None:
            m_data = self._fetch_eapi_json("mark")
            if isinstance(m_data, list):
                self._eapi_mark_cache = m_data

        t_list = self._eapi_ticker_cache or []
        m_list = self._eapi_mark_cache or []

        if not t_list and not m_list:
            logger.warning(
                f"No options ticker/mark data returned from Binance EAPI for {clean} "
                f"(status={self.last_eapi_status}, error={self.last_eapi_error})"
            )
            return []

        tickers_map: Dict[str, Dict[str, Any]] = {}
        for t in t_list:
            if isinstance(t, dict) and str(t.get("symbol", "")).startswith(prefix):
                tickers_map[t["symbol"]] = t

        marks_map: Dict[str, Dict[str, Any]] = {}
        for m in m_list:
            if isinstance(m, dict) and str(m.get("symbol", "")).startswith(prefix):
                marks_map[m["symbol"]] = m

        all_symbols = sorted(set(tickers_map.keys()) | set(marks_map.keys()))
        merged_options = []
        for sym in all_symbols:
            t_info = tickers_map.get(sym, {})
            m_info = marks_map.get(sym, {})
            merged_options.append({
                "symbol": sym,
                **t_info,
                "markOptions": m_info,
            })

        logger.info(f"Retrieved {len(merged_options)} active option contracts for {clean} from Binance.")
        return merged_options

    def get_klines(
        self,
        currency: str,
        interval: str = "1d",
        limit: int = 30
    ) -> List[Dict[str, Any]]:
        """Fetch historical Klines OHLCV candlesticks from Binance."""
        clean = self._clean_currency(currency)
        pair = f"{clean}USDT"
        candles = []
        raw = None

        for url, use_proxy in (
            (f"{self.DATA_VISION_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}", False),
            (f"{self.SPOT_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}", True),
            (f"{self.FUTURES_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}", True),
            (f"{self.US_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}", False),
        ):
            data, status, _ = self._request_json(url, timeout=8, use_proxy=use_proxy)
            if status == 200 and isinstance(data, list) and len(data) > 0:
                raw = data
                break

        if raw and isinstance(raw, list):
            for c in raw:
                if isinstance(c, list) and len(c) >= 6:
                    dt = datetime.fromtimestamp(c[0] / 1000.0, tz=timezone.utc)
                    close_p = float(c[4])
                    dec = 2 if close_p >= 10 else 4
                    candles.append({
                        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": round(float(c[1]), dec),
                        "high": round(float(c[2]), dec),
                        "low": round(float(c[3]), dec),
                        "close": round(close_p, dec),
                        "volume": round(float(c[5]), 2),
                    })

        return candles
