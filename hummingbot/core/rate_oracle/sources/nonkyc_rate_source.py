from decimal import Decimal
from typing import TYPE_CHECKING, Dict, Optional

from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.rate_oracle.sources.rate_source_base import RateSourceBase
from hummingbot.core.utils import async_ttl_cache

if TYPE_CHECKING:
    from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange


class NonkycRateSource(RateSourceBase):
    def __init__(self):
        super().__init__()
        self._exchange: Optional["NonkycExchange"] = None

    @property
    def name(self) -> str:
        return "nonkyc"

    @async_ttl_cache(ttl=30, maxsize=1)
    async def get_prices(self, quote_token: Optional[str] = None) -> Dict[str, Decimal]:
        """
        Fetch all trading pair prices from NonKYC /tickers endpoint.

        Uses bid/ask midpoint as the price. Pairs with zero or missing
        bid/ask are skipped.

        :param quote_token: If specified, only return pairs with this quote currency
        :return: Dict mapping "BASE-QUOTE" -> mid price as Decimal
        """
        self._ensure_exchange()
        results = {}
        try:
            tickers = await self._exchange.get_all_pairs_prices()
            for ticker in tickers:
                try:
                    base = ticker.get("base_currency", "")
                    quote = ticker.get("target_currency", "")
                    if not base or not quote:
                        continue

                    # Optional quote token filter
                    if quote_token is not None and quote != quote_token:
                        continue

                    bid_str = str(ticker.get("bid", "0"))
                    ask_str = str(ticker.get("ask", "0"))

                    if not bid_str or not ask_str or bid_str == "" or ask_str == "":
                        continue

                    bid = Decimal(bid_str)
                    ask = Decimal(ask_str)

                    if bid <= 0 or ask <= 0:
                        continue

                    if bid > ask:
                        continue  # Crossed book, skip

                    trading_pair = combine_to_hb_trading_pair(base=base, quote=quote)
                    results[trading_pair] = (bid + ask) / Decimal("2")

                except (ValueError, ArithmeticError, TypeError):
                    continue  # Skip pairs with unparseable data

        except Exception:
            self.logger().exception(
                msg="Unexpected error while retrieving rates from NonKYC. "
                    "Check the log file for more info.",
            )
        return results

    def _ensure_exchange(self):
        if self._exchange is None:
            self._exchange = self._build_nonkyc_connector_without_private_keys()

    @staticmethod
    def _build_nonkyc_connector_without_private_keys() -> "NonkycExchange":
        from hummingbot.connector.exchange.nonkyc.nonkyc_exchange import NonkycExchange

        return NonkycExchange(
            nonkyc_api_key="",
            nonkyc_api_secret="",
            trading_pairs=[],
            trading_required=False,
        )
