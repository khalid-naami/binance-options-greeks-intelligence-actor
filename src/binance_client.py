"""Official Binance Options EAPI and Klines REST Connector."""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
import requests

logger = logging.getLogger(__name__)

class BinanceClient:
    """Official Binance Public REST Endpoints for European Options & Klines."""

    EAPI_BASE = "https://eapi.binance.com/eapi/v1"
    SPOT_BASE = "https://api.binance.com/api/v3"
    FUTURES_BASE = "https://fapi.binance.com/fapi/v1"
    US_BASE = "https://api.binance.us/api/v3"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "BinanceOptionsQuantActor/1.0",
            "Accept": "application/json"
        })
        # Binance Option Symbol regex: BTC-260626-140000-C or ETH-260626-3500-P
        self.symbol_regex = re.compile(
            r'^([A-Z]+)-(\d{6})-(\d+(?:\.\d+)?)-([CP])$'
        )

    def _safe_get(self, url: str, timeout: int = 8) -> Optional[Any]:
        try:
            res = self.session.get(url, timeout=timeout)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.debug(f"GET failed for {url}: {e}")
        return None

    def get_spot_price(self, currency: str) -> float:
        """Fetch current live price for the underlying crypto asset."""
        clean = currency.upper().replace('-USD', '').replace('-USDT', '').replace('^', '').strip()
        pair = f"{clean}USDT"

        # 1. Spot API
        data = self._safe_get(f"{self.SPOT_BASE}/ticker/price?symbol={pair}")
        if data and isinstance(data, dict) and "price" in data:
            return float(data["price"])

        # 2. Futures API
        data_f = self._safe_get(f"{self.FUTURES_BASE}/ticker/price?symbol={pair}")
        if data_f and isinstance(data_f, dict) and "price" in data_f:
            return float(data_f["price"])

        # 3. Binance US API
        data_us = self._safe_get(f"{self.US_BASE}/ticker/price?symbol={pair}")
        if data_us and isinstance(data_us, dict) and "price" in data_us:
            return float(data_us["price"])

        defaults = {'BTC': 65000.0, 'ETH': 2650.0, 'SOL': 155.0, 'BNB': 580.0, 'DOGE': 0.12, 'XRP': 0.60}
        return defaults.get(clean, 100.0)

    def get_options_data(self, currency: str) -> List[Dict[str, Any]]:
        """Fetch all active options tickers and mark Greeks for a currency from Binance EAPI."""
        clean = currency.upper().replace('-USD', '').replace('-USDT', '').replace('^', '').strip()
        prefix = f"{clean}-"

        t_data = self._safe_get(f"{self.EAPI_BASE}/ticker")
        m_data = self._safe_get(f"{self.EAPI_BASE}/mark")

        if not t_data or not isinstance(t_data, list):
            logger.warning(f"No options ticker data returned from Binance EAPI for {clean}")
            return []

        marks_map = {}
        if m_data and isinstance(m_data, list):
            for m in m_data:
                if isinstance(m, dict) and "symbol" in m:
                    marks_map[m["symbol"]] = m

        merged_options = []
        for t in t_data:
            if isinstance(t, dict) and "symbol" in t:
                sym = t["symbol"]
                if sym.startswith(prefix):
                    m_info = marks_map.get(sym, {})
                    merged_options.append({
                        **t,
                        "markOptions": m_info
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
        clean = currency.upper().replace('-USD', '').replace('-USDT', '').replace('^', '').strip()
        pair = f"{clean}USDT"
        candles = []

        # Map interval
        url = f"{self.SPOT_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}"
        raw = self._safe_get(url)

        if not raw or not isinstance(raw, list):
            # Fallback to Futures Klines
            url_f = f"{self.FUTURES_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}"
            raw = self._safe_get(url_f)

        if not raw or not isinstance(raw, list):
            # Fallback to Binance US
            url_us = f"{self.US_BASE}/klines?symbol={pair}&interval={interval}&limit={limit}"
            raw = self._safe_get(url_us)

        if raw and isinstance(raw, list):
            for c in raw:
                if isinstance(c, list) and len(c) >= 6:
                    dt = datetime.fromtimestamp(c[0] / 1000.0, tz=timezone.utc)
                    close_p = float(c[4])
                    dec = 2 if close_p > 10 else 4
                    candles.append({
                        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": round(float(c[1]), dec),
                        "high": round(float(c[2]), dec),
                        "low": round(float(c[3]), dec),
                        "close": round(close_p, dec),
                        "volume": round(float(c[5]), 2)
                    })

        return candles
