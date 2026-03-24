import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from typing import Any, Dict
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class MexcAuth(AuthBase):
    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for
        authenticated interactions.  It also adds the required parameter in
        the request header.

        MEXC (Binance-compatible) expects ALL signed parameters — including
        timestamp and signature — in the **query string**, regardless of
        HTTP method.  The request body (if any) is NOT included in the
        signature computation for this exchange.
        """
        # Merge any existing params with auth fields
        params = dict(request.params or {})

        # For POST/PUT/DELETE with a body, the body content must also be
        # included in the params that get signed.  However, for MEXC the
        # standard pattern is: body params go into the query string for
        # signing, and the body itself is left empty.
        if request.data is not None:
            body = request.data
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except (ValueError, TypeError):
                    body = {}
            if isinstance(body, dict):
                params.update(body)
            # Clear the body — everything goes in query string
            request.data = None

        request.params = self.add_auth_to_params(params=params)

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)
        headers.update(self.header_for_authentication())
        # Content-Type rules (verified against live MEXC API 2026-03-24):
        # - Empty body (GET, or POST/PUT/DELETE with params in QS): omit Content-Type.
        #   MEXC rejects "application/x-www-form-urlencoded" on endpoints like
        #   userDataStream, but accepts omitted CT or "application/json".
        #   Safest: omit entirely when no body.
        # - Non-empty body: use application/json.
        if request.data is None:
            headers.pop("Content-Type", None)
        else:
            headers["Content-Type"] = "application/json"
        request.headers = headers

        logger.debug(
            f"MEXC auth: method={request.method.name}, "
            f"url={request.url}, "
            f"has_body={request.data is not None}, "
            f"has_params={bool(request.params)}, "
            f"content_type={request.headers.get('Content-Type', 'NONE')}"
        )

        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated. Mexc does not use this
        functionality
        """
        return request  # pass-through

    def add_auth_to_params(self,
                           params: Dict[str, Any]):
        timestamp = int(self.time_provider.time() * 1e3)

        request_params = OrderedDict(params or {})
        request_params["timestamp"] = timestamp

        signature = self._generate_signature(params=request_params)
        request_params["signature"] = signature

        return request_params

    def header_for_authentication(self) -> Dict[str, str]:
        return {"X-MEXC-APIKEY": self.api_key}

    def _generate_signature(self, params: Dict[str, Any]) -> str:

        encoded_params_str = urlencode(params)
        digest = hmac.new(self.secret_key.encode("utf8"), encoded_params_str.encode("utf8"), hashlib.sha256).hexdigest()
        return digest
