from hummingbot.core.api_throttler.data_types import LinkedLimitWeightPair, RateLimit
from hummingbot.core.data_type.in_flight_order import OrderState

# NonKYC has a single domain. This constant is retained for compatibility with
# the ExchangePyBase domain parameter interface but is not used in URL construction.
DEFAULT_DOMAIN = "nonkyc"
EXCHANGE_NAME = "nonkyc"

# Base urls
REST_URL = "https://api.nonkyc.io/api/"
WS_URL = "wss://api.nonkyc.io"

API_VERSION = "v2"

BROKER_ID = "hummingbot"
HBOT_ORDER_ID_PREFIX = "HBOT-"
MAX_ORDER_ID_LEN = 32

# Public Nonkyc API endpoints — current
SERVER_TIME_PATH_URL = "/time"
TICKER_INFO_PATH_URL = "/ticker"
TICKER_BOOK_PATH_URL = "/tickers"
MARKETS_INFO_PATH_URL = "/market/getlist"
MARKET_ORDERBOOK_PATH_URL = "/market/orderbook"
PING_PATH_URL = "/info"
SUPPORTED_SYMBOL_PATH_URL = "/asset/getlist"

# Deprecated public endpoints (old API, still functional but superseded)
SERVER_TIME_API_URL = "https://nonkyc.io/api/v2/getservertime"  # Deprecated: use SERVER_TIME_PATH_URL (/time)
ORDERBOOK_SNAPSHOT_PATH_URL = "/orderbook"  # Deprecated: use MARKET_ORDERBOOK_PATH_URL (/market/orderbook)

# Private Nonkyc API endpoints — current
USER_BALANCES_PATH_URL = "/balances"
ACCOUNT_TRADES_PATH_URL = "/account/trades"
CREATE_ORDER_PATH_URL = "/createorder"
CANCEL_ORDER_PATH_URL = "/cancelorder"
ACCOUNT_ORDERS_PATH_URL = "/account/orders"
ORDER_INFO_PATH_URL = "/getorder"
CANCEL_ALL_ORDERS_PATH_URL = "/cancelallorders"

# Deprecated private endpoints
USER_TRADES_PATH_URL = "/gettrades"  # Deprecated: use ACCOUNT_TRADES_PATH_URL
USER_TRADES_SINCE_A_TIMESTAMP_PATH_URL = "/gettradessince"  # Deprecated: use ACCOUNT_TRADES_PATH_URL with since param

# Ws public methods
WS_METHOD_SUBSCRIBE_ORDERBOOK = "subscribeOrderbook"
WS_METHOD_SUBSCRIBE_TRADES = "subscribeTrades"
WS_METHOD_UNSUBSCRIBE_ORDERBOOK = "unsubscribeOrderbook"
WS_METHOD_UNSUBSCRIBE_TRADES = "unsubscribeTrades"

# Ws private methods
WS_METHOD_SUBSCRIBE_USER_ORDERS = "subscribeReports"
WS_METHOD_SUBSCRIBE_USER_BALANCE = "subscribeBalances"

# Websocket event types
DIFF_EVENT_TYPE = "updateOrderbook"
SNAPSHOT_EVENT_TYPE = "snapshotOrderbook"
TRADE_EVENT_TYPE = "updateTrades"
SNAPSHOT_TRADES_EVENT_TYPE = "snapshotTrades"

WS_HEARTBEAT_TIME_INTERVAL = 30

# Rate Limit time intervals in seconds
ONE_MINUTE = 60
ONE_SECOND = 1
ONE_DAY = 86400

# Order params
SIDE_BUY = "buy"
SIDE_SELL = "sell"

ORDER_STATE = {
    "New": OrderState.OPEN,
    "new": OrderState.OPEN,
    "Active": OrderState.OPEN,
    "active": OrderState.OPEN,
    "Partly Filled": OrderState.PARTIALLY_FILLED,
    "partly filled": OrderState.PARTIALLY_FILLED,
    "partlyFilled": OrderState.PARTIALLY_FILLED,
    "Filled": OrderState.FILLED,
    "filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "Canceled": OrderState.CANCELED,
    "canceled": OrderState.CANCELED,
    "Expired": OrderState.CANCELED,
    "expired": OrderState.CANCELED,
    "Rejected": OrderState.FAILED,
    "rejected": OrderState.FAILED,
    "Suspended": OrderState.PENDING_CREATE,
    "suspended": OrderState.PENDING_CREATE,
}

# Rate Limit Type
REQUEST_WEIGHT = "REQUEST_WEIGHT"
ORDERS = "ORDERS"
ORDERS_24HR = "ORDERS_24HR"
RAW_REQUESTS = "RAW_REQUESTS"

# ============================================================================
# RATE LIMITS -- ESTIMATED VALUES
# ============================================================================
# NonKYC does not publish rate limit documentation. These values are
# conservative estimates based on observed behavior during development.
#
# If you encounter HTTP 429 ("Too Many Requests") errors in hummingbot logs:
#   1. Reduce MAX_REQUEST (e.g., from 5000 to 2000)
#   2. Increase REQUEST_WEIGHT time_interval (e.g., from 60s to 120s)
#
# If bot performance feels sluggish (slow order placement, delayed updates):
#   1. Increase MAX_REQUEST cautiously
#   2. Reduce weight values on frequently-used endpoints
#
# Monitor logs for "Rate limit" warnings from the AsyncThrottler.
# ============================================================================

# Estimated -- NonKYC doesn't publish rate limit docs
MAX_REQUEST = 5000

RATE_LIMITS = [
    # Pools (estimated values — NonKYC does not publish rate limit documentation)
    RateLimit(limit_id=REQUEST_WEIGHT, limit=6000, time_interval=ONE_MINUTE),
    RateLimit(limit_id=ORDERS, limit=50, time_interval=10 * ONE_SECOND),
    RateLimit(limit_id=ORDERS_24HR, limit=160000, time_interval=ONE_DAY),
    RateLimit(limit_id=RAW_REQUESTS, limit=61000, time_interval=5 * ONE_MINUTE),
    # Weighted Limits
    RateLimit(limit_id=TICKER_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 2),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=TICKER_BOOK_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MARKETS_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=MARKET_ORDERBOOK_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 100),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ORDERBOOK_SNAPSHOT_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 100),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SERVER_TIME_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=USER_BALANCES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=SERVER_TIME_API_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=PING_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=USER_TRADES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=USER_TRADES_SINCE_A_TIMESTAMP_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),
    RateLimit(limit_id=ACCOUNT_TRADES_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),

    RateLimit(limit_id=CREATE_ORDER_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(ORDERS_24HR, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),

    RateLimit(limit_id=CANCEL_ORDER_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(ORDERS_24HR, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),

    RateLimit(limit_id=ACCOUNT_ORDERS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 20),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),

    RateLimit(limit_id=CANCEL_ALL_ORDERS_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 10),
                             LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(ORDERS_24HR, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)]),

    RateLimit(limit_id=ORDER_INFO_PATH_URL, limit=MAX_REQUEST, time_interval=ONE_MINUTE,
              linked_limits=[LinkedLimitWeightPair(REQUEST_WEIGHT, 4),
                             LinkedLimitWeightPair(ORDERS, 1),
                             LinkedLimitWeightPair(ORDERS_24HR, 1),
                             LinkedLimitWeightPair(RAW_REQUESTS, 1)])
]

ORDER_NOT_EXIST_ERROR_CODE = 20002
ORDER_NOT_EXIST_MESSAGE = "Order not found"

UNKNOWN_ORDER_ERROR_CODE = 20002
UNKNOWN_ORDER_MESSAGE = "Active order not found for cancellation"
