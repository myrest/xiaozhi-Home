"""Bootstrap interoperability checks; run in the endpoint image for upstream AES."""

import base64
import configparser
from contextlib import ExitStack, redirect_stdout
from importlib import import_module
from io import StringIO
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from endpoint import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        try:
            self.aes = import_module("src.utils.aes_utils")
        except ModuleNotFoundError as error:
            if error.name in {"src", "src.utils", "src.utils.aes_utils"}:
                self.skipTest("Run in the endpoint image to use its genuine upstream AES")
            raise

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.base = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(bootstrap, "CONFIG_FILE", self.base / "endpoint.cfg"))
        stack.enter_context(patch.object(bootstrap, "URL_DIR", self.base / "urls"))
        stack.enter_context(patch.dict(os.environ, {
            "XIAOZHI_AGENT_ID": "171abaf0ad664538a51969b7e8f29eba",
            "XIAOZHI_ENDPOINT_HOST": "bedtime-mcp-endpoint",
        }))
        self.output = stack.enter_context(redirect_stdout(StringIO()))

    def test_token_matches_manager_identity_and_reinitialization_preserves_key(self):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        # Public test fixture; genuine upstream AES encrypts both initializations.
        key = "0123456789abcdef0123456789abcdef"
        with patch.object(bootstrap.secrets, "token_hex", return_value=key) as generate_key:
            bootstrap.bootstrap()
            first_config = bootstrap.CONFIG_FILE.read_text(encoding="utf-8")
            first_urls = {
                name: (bootstrap.URL_DIR / name).read_text(encoding="utf-8")
                for name in ("tool_endpoint", "xiaozhi_endpoint", "health_url")
            }
            bootstrap.bootstrap()
            generate_key.assert_called_once_with(16)

        self.assertEqual(bootstrap.CONFIG_FILE.read_text(encoding="utf-8"), first_config)
        cfg = configparser.ConfigParser()
        cfg.read(bootstrap.CONFIG_FILE)
        self.assertEqual(cfg.get("server", "key"), key)
        for name, original in first_urls.items():
            self.assertEqual((bootstrap.URL_DIR / name).read_text(encoding="utf-8"), original)

        tokens = []
        for name, hostname in (("tool_endpoint", "endpoint"),
                               ("xiaozhi_endpoint", "bedtime-mcp-endpoint")):
            url = urlsplit(first_urls[name].strip())
            self.assertEqual((url.scheme, url.hostname, url.port, url.path),
                             ("ws", hostname, 8004, "/mcp_endpoint/mcp/"))
            token = parse_qs(url.query, strict_parsing=True)["token"][0]
            tokens.append(token)
            # Decode independently of upstream decrypt, using Manager's wire format.
            ciphertext = base64.b64decode(token, validate=True)
            plaintext = unpad(AES.new(key.encode("utf-8"), AES.MODE_ECB).decrypt(ciphertext),
                              AES.block_size)
            self.assertEqual(json.loads(plaintext), {
                "agentId": "edab4fb479ac015afe33bf4c0f6629e5",
            })
            self.assertNotIn(token, self.output.getvalue())
        self.assertEqual(tokens[0], tokens[1])
        self.assertNotIn(key, self.output.getvalue())
        for url in first_urls.values():
            self.assertNotIn(url.strip(), self.output.getvalue())

    def test_base64_query_characters_are_percent_encoded(self):
        # Isolate URL encoding; the interoperability test above uses real encryption.
        with patch.object(self.aes, "encrypt", return_value="encoding+/="):
            bootstrap.bootstrap()

        for name in ("tool_endpoint", "xiaozhi_endpoint"):
            url = urlsplit((bootstrap.URL_DIR / name).read_text(encoding="utf-8").strip())
            self.assertEqual(url.query, "token=encoding%2B%2F%3D")
            self.assertEqual(parse_qs(url.query), {"token": ["encoding+/="]})
