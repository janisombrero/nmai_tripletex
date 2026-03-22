import json as _json
import logging
import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class TripletexClient:
    def __init__(self, base_url: str, session_token: str, consumer_token: str = ""):
        self.base_url = base_url.rstrip("/")
        # Standardize headers: move Content-Type/Accept into methods to avoid 403 on GET
        self.auth = HTTPBasicAuth("0", session_token)
        last4 = session_token[-4:] if len(session_token) >= 4 else "????"
        logger.info("TripletexClient init: base_url=%s auth=Basic 0:***%s token_len=%s",
                    self.base_url, last4, len(session_token))

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}{endpoint}"

    def get(self, endpoint: str, params: dict = None):
        url = self._url(endpoint)
        headers = {"Accept": "application/json"}
        try:
            resp = requests.get(url, params=params, auth=self.auth, headers=headers, timeout=30)
            logger.info("GET %s -> %d", endpoint, resp.status_code)
            return resp.status_code, self._parse(resp)
        except Exception as e:
            logger.error("GET %s error: %s", endpoint, e)
            return 0, {"error": str(e)}

    @staticmethod
    def _strip_voucher_postings(endpoint: str, body: dict) -> dict:
        """Remove system-generated fields from voucher postings before sending to Tripletex.

        Tripletex rejects POST/PUT /ledger/voucher with 422 if postings contain
        fields like 'row' or 'guiRow' (system-generated values it manages internally).
        Stripping at the HTTP layer is a backstop that works regardless of which
        handler built the postings.
        """
        logger.info("STRIP called for %s", endpoint)
        if "/ledger/voucher" in endpoint and isinstance(body, dict) and "postings" in body:
            strip = {"row", "guiRow"}
            for posting in body.get("postings", []):
                if isinstance(posting, dict):
                    before = list(posting.keys())
                    for key in strip:
                        posting.pop(key, None)
                    after = list(posting.keys())
                    if before != after:
                        logger.info("STRIP_DEBUG: posting before=%s after=%s", before, after)
        return body

    def post(self, endpoint: str, json: dict = None, params: dict = None):
        if json is not None:
            json = self._strip_voucher_postings(endpoint, json)
        if "/ledger/voucher" in endpoint and not any(
            x in endpoint for x in ["/:sendToLedger", "/:sendToInbox", "/attachment", "/pdf"]
        ):
            logger.info("VOUCHER_DEBUG payload: %s", _json.dumps(json, default=str)[:800])
        url = self._url(endpoint)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = requests.post(url, json=json, params=params, auth=self.auth, headers=headers, timeout=30)
            logger.info("POST %s -> %d", endpoint, resp.status_code)
            parsed = self._parse(resp)
            if resp.status_code >= 400:
                self._log_error(endpoint, resp.status_code, parsed)
            return resp.status_code, parsed
        except Exception as e:
            logger.error("POST %s error: %s", endpoint, e)
            return 0, {"error": str(e)}

    def put(self, endpoint: str, json: dict = None, params: dict = None):
        if json is not None:
            json = self._strip_voucher_postings(endpoint, json)
        url = self._url(endpoint)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            resp = requests.put(url, json=json, params=params, auth=self.auth, headers=headers, timeout=30)
            logger.info("PUT %s -> %d", endpoint, resp.status_code)
            parsed = self._parse(resp)
            if resp.status_code >= 400:
                self._log_error(endpoint, resp.status_code, parsed)
            return resp.status_code, parsed
        except Exception as e:
            logger.error("PUT %s error: %s", endpoint, e)
            return 0, {"error": str(e)}

    def post_multipart(self, endpoint: str, file_bytes: bytes, filename: str,
                       mime_type: str = "application/octet-stream", params: dict = None):
        url = self._url(endpoint)
        headers = {"Accept": "application/json"}
        try:
            resp = requests.post(
                url,
                files={"file": (filename, file_bytes, mime_type)},
                params=params,
                auth=self.auth,
                headers=headers,
                timeout=60,
            )
            logger.info("POST (multipart) %s -> %d", endpoint, resp.status_code)
            parsed = self._parse(resp)
            if resp.status_code >= 400:
                self._log_error(endpoint, resp.status_code, parsed)
            return resp.status_code, parsed
        except Exception as e:
            logger.error("POST multipart %s error: %s", endpoint, e)
            return 0, {"error": str(e)}

    def delete(self, endpoint: str):
        url = self._url(endpoint)
        headers = {"Accept": "application/json"}
        try:
            resp = requests.delete(url, auth=self.auth, headers=headers, timeout=30)
            logger.info("DELETE %s -> %d", endpoint, resp.status_code)
            return resp.status_code, self._parse(resp)
        except Exception as e:
            logger.error("DELETE %s error: %s", endpoint, e)
            return 0, {"error": str(e)}

    def _log_error(self, endpoint: str, status: int, parsed: dict):
        messages = parsed.get("validationMessages") or parsed.get("message") or parsed.get("raw", "")
        logger.error("%s %s -> %d | validationMessages: %s", endpoint, status, status, messages)

    def _parse(self, resp: requests.Response):
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}
