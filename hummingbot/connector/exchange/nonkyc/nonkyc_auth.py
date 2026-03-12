import hashlib
import hmac
import json
import random
import string
from typing import Dict
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSRequest


class NonkycAuth(AuthBase):
    def __init__(self, api_key: str, secret_key: str, time_provider: TimeSynchronizer):
        self.api_key = api_key
        self.secret_key = secret_key
        self.time_provider = time_provider

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        """
        Adds the server time and the signature to the request, required for authenticated interactions. It also adds
        the required parameter in the request header.
        :param request: the request to be configured for authenticated interaction
        """

        headers = {}
        if request.headers is not None:
            headers.update(request.headers)

        if request.method == RESTMethod.GET:
            # Build the full URL with query params baked in, then sign it.
            #
            # CRITICAL: Do NOT use urllib.parse.urlencode() here.
            # urlencode() encodes '/' as '%2F', but aiohttp (via yarl) does NOT
            # encode '/' in query parameter values. The NonKYC server verifies the
            # HMAC against the URL it receives (the yarl version), so the signed
            # URL must be byte-identical to what aiohttp sends.
            #
            # By baking params into request.url and clearing request.params, we
            # ensure the auth signs EXACTLY the URL the HTTP client transmits.
            if request.params:
                qs_parts = [f"{k}={v}" for k, v in request.params.items()]
                request.url = f"{request.url}?{'&'.join(qs_parts)}"
                request.params = None  # Prevent aiohttp from re-encoding
            headers.update(self.header_for_authentication(data=request.url))

        elif request.method == RESTMethod.POST:
            json_str = json.dumps(json.loads(request.data), separators=(',', ':'))
            to_sign = f"{request.url}{json_str}"
            headers.update(self.header_for_authentication(to_sign))
            request.data = json_str  # Body must match what was signed (per NonKYC API spec)

        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        """
        This method is intended to configure a websocket request to be authenticated. Nonkyc does not use this
        functionality
        """
        return request  # pass-through

    def generate_ws_authentication_message(self, request: WSRequest = None) -> Dict[str, any]:
        random_str = "".join(random.choices(string.ascii_letters + string.digits, k=14))
        payload = {
            "method": "login",
            "params": {
                "algo": "HS256",
                "pKey": self.api_key,
                "nonce": random_str,
                "signature": self._generate_signature(random_str)
            },
            "id": 99  # Fixed id for auth; response correlation handled by result check
        }
        return payload

    def header_for_authentication(self, data: str) -> Dict[str, str]:
        timestamp = int(self.time_provider.time() * 1e3)
        message_to_sign = f"{self.api_key}{data}{timestamp}"
        signature = self._generate_signature(message_to_sign)
        return {"X-API-KEY": self.api_key,
                "X-API-NONCE": str(timestamp),
                "X-API-SIGN": signature}

    def _generate_signature(self, data: str) -> str:
        digest = hmac.new(self.secret_key.encode("utf8"), data.encode("utf8"), hashlib.sha256).hexdigest()
        return digest
