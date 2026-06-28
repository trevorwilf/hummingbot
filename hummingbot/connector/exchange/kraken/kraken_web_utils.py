import time
from typing import Optional

import hummingbot.connector.exchange.kraken.kraken_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def private_rest_url(*args, **kwargs) -> str:
    return rest_url(*args, **kwargs)


def public_rest_url(*args, **kwargs) -> str:
    return rest_url(*args, **kwargs)


def rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN):
    # Kraken exposes a single REST base; the domain argument is accepted for interface parity with other
    # connectors and resolved through a mapping so it is honoured rather than silently ignored.
    base_url = CONSTANTS.DOMAIN_TO_BASE_URL.get(domain, CONSTANTS.BASE_URL)
    return base_url + path_url


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        auth: Optional[AuthBase] = None, ) -> WebAssistantsFactory:
    throttler = throttler
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth
    )
    return api_factory


def is_exchange_information_valid(trading_pair_details) -> bool:
    """
    Verifies if a trading pair is enabled to operate with based on its exchange information

    :param exchange_info: the exchange information for a trading pair

    :return: True if the trading pair is enabled, False otherwise
    Want to filter out dark pool trading pairs from the list of trading pairs
    For more info, please check
    https://support.kraken.com/hc/en-us/articles/360001391906-Introducing-the-Kraken-Dark-Pool
    """
    # Exclude pairs that are not fully tradable. Kraken's AssetPairs entries carry a 'status' field
    # ('online' / 'cancel_only' / 'post_only' / ...); only 'online' pairs accept normal order flow.
    # Entries without a status field (older API shape / test fixtures) are treated as valid.
    status = trading_pair_details.get('status')
    if status is not None and status != 'online':
        return False
    if trading_pair_details.get('altname'):
        return not trading_pair_details.get('altname').endswith('.d')
    return True


async def get_current_server_time(
        throttler,
        domain
) -> float:
    # Kraken authenticates with a monotonically increasing nonce rather than a synchronized timestamp
    # (see KrakenAuth.get_tracking_nonce), so the connector does not depend on the exchange clock and
    # intentionally returns local time here instead of issuing a /0/public/Time request on startup.
    return time.time()
