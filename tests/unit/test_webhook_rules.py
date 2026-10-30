"""Webhook rules that need no engines: where hooks may point, and how requests are signed."""

import hashlib
import hmac

import pytest

from swsdb_server.stores import StoreError
from swsdb_server.webhooks import backoff, check_url, signature


@pytest.mark.parametrize("url", ["http://127.0.0.1:6333/collections", "http://localhost:6379/", "http://[::1]/x",
                                 "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "ftp://example.com/",
                                 "file:///etc/passwd", "http://user:pass@10.0.0.5/hook"])
def test_hooks_cannot_reach_this_machine_or_metadata_endpoints(url):
    with pytest.raises(StoreError):
        check_url(url)


def test_private_networks_are_allowed():
    assert check_url("http://10.1.2.3:8080/hook") == "http://10.1.2.3:8080/hook"
    assert check_url("https://192.168.1.20/swsdb")


def test_signature_is_hmac_sha256_of_the_body():
    body = b'{"event":"document.ready"}'
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert signature("s3cret", body) == f"sha256={expected}"


def test_backoff_grows_and_is_capped():
    assert backoff(1) < backoff(2) < backoff(5) and backoff(30) == 1800
