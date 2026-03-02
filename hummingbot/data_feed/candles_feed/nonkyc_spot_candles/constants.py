from bidict import bidict

from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit

# NonKYC REST API
REST_URL = "https://api.nonkyc.io/api/v2"
HEALTH_CHECK_ENDPOINT = "/time"
CANDLES_ENDPOINT = "/market/candles"

# NonKYC WebSocket (public streaming endpoint from WS API docs)
WSS_URL = "wss://ws.nonkyc.io"

# NonKYC candle intervals are specified in MINUTES.
# The bidict maps hummingbot's standard interval strings to NonKYC's minute values.
# NonKYC supports: 5, 15, 30, 60, 180, 240, 480, 720, 1440
# It does NOT support 1m, 3m, 1s, 2h, 6h, 3d, 1w, 1M — those are excluded.
INTERVALS = bidict({
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "3h": "180",
    "4h": "240",
    "8h": "480",
    "12h": "720",
    "1d": "1440",
})

# Max candles per REST request (NonKYC uses the countBack parameter)
MAX_RESULTS_PER_CANDLESTICK_REST_REQUEST = 500

# Rate limit identifiers
REQUEST_WEIGHT = "REQUEST_WEIGHT"

RATE_LIMITS = [
    RateLimit(REQUEST_WEIGHT, limit=6000, time_interval=60),
    RateLimit(CANDLES_ENDPOINT, limit=1200, time_interval=60,
             linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1)]),
    RateLimit(HEALTH_CHECK_ENDPOINT, limit=1200, time_interval=60,
             linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1)]),
]
