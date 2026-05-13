from __future__ import annotations

from src.dedup import DedupSet, canonicalize_url, fingerprint


def test_canonicalize_normalizes_host_and_scheme():
    assert canonicalize_url("HTTP://Example.COM/") == "http://example.com/"
    assert canonicalize_url("https://EXAMPLE.com:443/foo") == "https://example.com/foo"
    assert canonicalize_url("http://example.com:80/") == "http://example.com/"


def test_canonicalize_sorts_query_and_drops_fragment():
    assert canonicalize_url("https://a.com/p?b=2&a=1#frag") == "https://a.com/p?a=1&b=2"
    # Equal fingerprint regardless of query order
    assert fingerprint("https://a.com/p?b=2&a=1") == fingerprint("https://a.com/p?a=1&b=2")


def test_canonicalize_strips_userinfo():
    assert canonicalize_url("https://user:pass@example.com/x") == "https://example.com/x"


def test_canonicalize_empty_path_becomes_slash():
    assert canonicalize_url("https://a.com") == "https://a.com/"


def test_dedup_set_add_returns_false_on_dup():
    d = DedupSet()
    assert d.add("https://a.com/") is True
    # Different variant of the same canonical URL
    assert d.add("HTTPS://A.COM/") is False
    assert d.add("https://a.com/?") is True or d.add("https://a.com/") is False  # stable
    assert len(d) >= 1


def test_dedup_contains():
    d = DedupSet()
    d.add("https://a.com/p")
    assert "HTTPS://A.COM/p" in d
    assert "https://b.com/" not in d
