# Binance Crypto Options Greeks, Candlesticks & Volume Intelligence Actor

Institutional-grade quantitative crypto options data engine powered by **100% direct official Binance Options EAPI (`eapi.binance.com`)**. Delivers **Live Binance Klines OHLCV Candlestick Price Action**, **Strike-by-Strike Primary & Higher-Order Greeks Tables**, and **Open Interest & 24h Volume Distributions** across **BTC, ETH, SOL, BNB, DOGE, and XRP**.

---

## ⚡ Direct Official Binance Architecture

- **Zero Third-Party Scraping / Zero yfinance**: Connects directly to official Binance endpoints (`eapi/v1/ticker`, `eapi/v1/mark`, and `api/v3/klines`).
- **No API Key Required**: Fully utilizes unauthenticated public REST endpoints.
- **European Options Contract Standard**: Native Mark IV and cash-settled European options pricing model.

---

## 🚀 Supported Crypto Assets

| Asset | Underlying Symbol | Contract Base | Option Style |
|---|---|---|---|
| **Bitcoin** | `BTC` | `BTCUSDT` | European Cash-Settled |
| **Ethereum** | `ETH` | `ETHUSDT` | European Cash-Settled |
| **Solana** | `SOL` | `SOLUSDT` | European Cash-Settled |
| **Binance Coin** | `BNB` | `BNBUSDT` | European Cash-Settled |
| **Dogecoin** | `DOGE` | `DOGEUSDT` | European Cash-Settled |
| **Ripple** | `XRP` | `XRPUSDT` | European Cash-Settled |

---

## 📊 Key Data Tables & Fields

### 1. Strike-by-Strike Greeks Dataset
- **Primary Greeks:**
  - `callDelta`, `putDelta`: Direct directional delta sensitivity.
  - `gamma`: Rate of change of Delta per \$1 underlying move.
  - `vega`: Option price sensitivity to 1% shift in Mark IV.
  - `callTheta`, `putTheta`: Daily time decay value.
- **Higher-Order Quantitative Greeks:**
  - `vanna`: Cross-derivative $\frac{\partial \text{Delta}}{\partial \text{IV}}$.
  - `charm`: Delta decay over time $\frac{\partial \text{Delta}}{\partial T}$.
  - `speed`: Third-order derivative $\frac{\partial \text{Gamma}}{\partial S}$.
  - `vomma`: Volatility convexity $\frac{\partial \text{Vega}}{\partial \text{IV}}$.
  - `color`: Gamma decay over time $\frac{\partial \text{Gamma}}{\partial T}$.
- **Breakevens:** Expiration breakevens ($K + \text{Call Price}$ and $K - \text{Put Price}$).

### 2. Open Interest & 24h Volume Distribution
- `callOpenInterest`, `putOpenInterest`, `netOpenInterest`
- `callVolume24h`, `putVolume24h`, `netVolume24h`
- Total Open Interest, Total Volume, and `Put/Call Ratio` ($PCR_{\text{OI}}$ & $PCR_{\text{Vol}}$).

### 3. Binance Klines OHLCV Candlestick Price History
- Official Binance spot/futures candlestick series (`open`, `high`, `low`, `close`, `volume`).

---

## 📥 Input Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `currencies` | `Array` | `["BTC", "ETH", "SOL"]` | List of cryptocurrency keys to fetch from Binance Options. |
| `candlestickInterval` | `String` | `"1d"` | Klines resolution interval (`5m`, `15m`, `1h`, `4h`, `1d`). |
| `candlestickLimit` | `Integer` | `30` | Number of historical OHLCV candlestick bars. |
| `maxExpirationsPerCurrency` | `Integer` | `4` | Maximum expiration cycles to process per currency. |
| `includeCandlesticks` | `Boolean` | `true` | Include OHLCV series. |
| `includeOiVolumeTable` | `Boolean` | `true` | Include Open Interest and Volume columns. |
| `includeFullGreeksTable` | `Boolean` | `true` | Include primary & higher-order Greeks. |

---

## 📤 Output Structure

### 1. Default Dataset (Binance Strike Options Table)
```json
{
  "currency": "BTC",
  "underlyingPriceUsd": 65420.50,
  "expirationDate": "2026-10-30",
  "daysToExpiration": 30,
  "strike": 66000.00,
  "callLastPriceUsd": 3250.80,
  "putLastPriceUsd": 3820.10,
  "callMarkIvPct": 54.25,
  "putMarkIvPct": 55.10,
  "callDelta": 0.4852,
  "putDelta": -0.5148,
  "gamma": 0.0000185,
  "vega": 98.42,
  "callTheta": -54.20,
  "putTheta": -51.85,
  "vanna": 0.000425,
  "charmCall": -0.000120,
  "speed": -0.00000001,
  "vomma": 0.1850,
  "color": -0.00000005,
  "callOpenInterest": 1845.2,
  "putOpenInterest": 1210.5,
  "netOpenInterest": 634.7,
  "callVolume24h": 412.0,
  "putVolume24h": 285.5,
  "netVolume24h": 126.5,
  "callBreakevenUsd": 69250.80,
  "putBreakevenUsd": 62179.90
}
```

### 2. Key-Value Store (`OUTPUT`)
Comprehensive JSON report with currency candlestick arrays and expiration totals summary.
