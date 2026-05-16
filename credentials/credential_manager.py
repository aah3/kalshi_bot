"""
credentials/credential_manager.py

Handles all authentication concerns:
  - Loads API key ID and RSA private key from environment variables only
  - Signs HTTP requests using RSA-PSS (required by Kalshi)
  - Never reads from disk or hardcodes secrets
"""

import base64
import os
import time
from typing import Dict

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.backends import default_backend

import config
from credentials.env_credentials import CredentialError, normalize_env, resolve_credentials


class CredentialManager:
    """
    Loads credentials from environment variables and produces signed
    request headers for the Kalshi REST API.

    Store both demo and prod keys in .env; set ``KALSHI_ENV`` to select which pair
    is used (see ``credentials/env_credentials.py``).
    """

    def __init__(self, env: str | None = None) -> None:
        self._env = normalize_env(env or config.ENV)
        api_key_id, key_b64, source = resolve_credentials(self._env)
        self._credential_source = source
        self._api_key_id = api_key_id
        self._private_key = self._load_private_key(key_b64)

    # ── Public interface ────────────────────────────────────────────────────

    @property
    def env(self) -> str:
        return self._env

    @property
    def api_key_id(self) -> str:
        return self._api_key_id

    @property
    def credential_source(self) -> str:
        """Which env vars supplied the active key (for logs)."""
        return self._credential_source

    def sign_request(
        self,
        method: str,
        path: str,
        body: str = "",
        timestamp_ms: int | None = None,
    ) -> Dict[str, str]:
        """
        Return headers required by Kalshi's RSA-PSS authentication scheme.

        Args:
            method:       HTTP verb (GET, POST, DELETE …), uppercase.
            path:         URL path including query string, e.g. '/trade-api/v2/markets'.
            body:         Raw request body string (empty string for GET).
            timestamp_ms: Unix epoch milliseconds; defaults to now.

        Returns:
            Dict of headers to merge into the outgoing request.
        """
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        message = self._build_message(method, path, ts)
        signature = self._sign(message)

        return {
            "KALSHI-ACCESS-KEY":       self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type":            "application/json",
        }

    # ── Private helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise CredentialError(
                f"Required environment variable '{name}' is not set. "
                "Set it before starting the bot."
            )
        return value

    def _load_private_key(self, raw: str):
        """
        Load an RSA private key from a base64 or PEM string.

        Accepts:
          - Base64 of a PEM file (decodes to ASCII starting with ``-----BEGIN``).
          - Base64 of DER PKCS#1 / PKCS#8 (decodes to binary; common when the key was
            exported as ``.der`` or base64-wrapped without PEM headers).
          - Raw PEM text in ``.env`` (e.g. a multiline value in double quotes).

        Whitespace in base64 strings is ignored.
        """
        stripped = raw.strip()
        if stripped.startswith("-----BEGIN") and "PRIVATE KEY" in stripped:
            key_bytes = raw.encode("utf-8")
        else:
            b64_compact = "".join(raw.split())
            try:
                key_bytes = base64.b64decode(b64_compact, validate=False)
            except Exception as exc:
                raise CredentialError(
                    "Private key is not valid base64 (check for truncated lines "
                    "or stray characters). Encode PEM with: "
                    "python -c \"import base64, pathlib; "
                    "print(base64.b64encode(pathlib.Path('key.pem').read_bytes()).decode())\""
                ) from exc

        private_key = self._deserialize_private_key(key_bytes)

        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise CredentialError(
                "Kalshi requires an RSA private key; this material is another key type."
            )
        return private_key

    def _deserialize_private_key(self, key_bytes: bytes):
        """Try PEM then DER; raise CredentialError on failure."""
        if key_bytes.lstrip().startswith(b"-----BEGIN"):
            loaders = (
                ("PEM", lambda b: serialization.load_pem_private_key(b, password=None, backend=default_backend())),
            )
        else:
            loaders = (
                ("DER", lambda b: serialization.load_der_private_key(b, password=None, backend=default_backend())),
                ("PEM", lambda b: serialization.load_pem_private_key(b, password=None, backend=default_backend())),
            )

        last_exc: Exception | None = None
        for _label, load in loaders:
            try:
                return load(key_bytes)
            except ValueError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "password" in msg or "encrypted" in msg:
                    raise CredentialError(
                        "The private key appears to be password-protected. "
                        "Export an unencrypted key for the bot (or decrypt it first)."
                    ) from exc
            except Exception as exc:
                last_exc = exc

        assert last_exc is not None
        raise CredentialError(
            "Could not read private key material. Use base64 of your Kalshi RSA private "
            "key as PEM or DER, or paste the PEM block in double quotes in .env."
        ) from last_exc

    @staticmethod
    def _build_message(method: str, path: str, timestamp_ms: int) -> bytes:
        """
        Kalshi signing payload (see docs.kalshi.com quick start):
            <timestamp_ms><METHOD><path>

        Path is the full API path from root (e.g. /trade-api/v2/portfolio/orders),
        without query string. Request body is NOT included in the signature.
        """
        path_clean = path.split("?")[0]
        raw = f"{timestamp_ms}{method.upper()}{path_clean}"
        return raw.encode()

    def _sign(self, message: bytes) -> str:
        """Sign message bytes with RSA-PSS and return base64url-encoded signature."""
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()
