from decimal import Decimal
from typing import Dict

from hummingbot.connector.utils import combine_to_hb_trading_pair, split_hb_trading_pair
from hummingbot.core.gateway.utils import unwrap_token_symbol


def find_rate(prices: Dict[str, Decimal], pair: str) -> Decimal:
    '''
    Finds exchange rate for a given trading pair from a dictionary of prices.
    Returns Decimal("0") if the rate cannot be determined (missing or zero denominator).
    '''
    if pair in prices:
        return prices[pair]
    base, quote = split_hb_trading_pair(trading_pair=pair)
    base = unwrap_token_symbol(base)
    quote = unwrap_token_symbol(quote)
    if base == quote:
        return Decimal("1")
    reverse_pair = combine_to_hb_trading_pair(base=quote, quote=base)
    if reverse_pair in prices:
        rate = prices[reverse_pair]
        if rate == Decimal("0"):
            return Decimal("0")
        return Decimal("1") / rate
    base_prices = {k: v for k, v in prices.items() if k.startswith(f"{base}-")}
    for base_pair, proxy_price in base_prices.items():
        link_quote = split_hb_trading_pair(base_pair)[1]
        link_pair = combine_to_hb_trading_pair(base=link_quote, quote=quote)
        if link_pair in prices:
            return proxy_price * prices[link_pair]
        common_denom_pair = combine_to_hb_trading_pair(base=quote, quote=link_quote)
        if common_denom_pair in prices:
            rate = prices[common_denom_pair]
            if rate == Decimal("0"):
                return Decimal("0")
            return proxy_price / rate
    return Decimal("0")
