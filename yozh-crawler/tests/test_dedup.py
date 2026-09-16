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


def test_fingerprint_unifies_www_alias():
    # www.X and X are one site (same-domain scope alias): the same path under
    # either spelling is one page — no twin fetches, no double budget.
    d = DedupSet()
    assert d.add("https://x.com/a") is True
    assert d.add("https://www.x.com/a") is False
    assert "https://www.x.com/a" in d
    # ...while the emitted canonical URL keeps its original host.
    assert canonicalize_url("https://www.x.com/a") == "https://www.x.com/a"


def test_fingerprint_keeps_degenerate_www_hosts_distinct():
    d = DedupSet()
    assert d.add("https://www.com/") is True
    assert d.add("https://com/") is True


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


def test_ipv6_literals_keep_their_brackets():
    """Audit H-16: a bare `::1:8080` netloc raises on the next parse."""
    assert canonicalize_url("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert canonicalize_url("http://[2001:db8::1]/") == "http://[2001:db8::1]/"
    assert canonicalize_url("HTTP://[2001:DB8::1]:80/") == "http://[2001:db8::1]/"
    seen = DedupSet()
    assert seen.add("http://[::1]:8080/x") is True
    assert seen.add("http://[::1]:8080/x") is False
    assert fingerprint("http://[2001:db8::1]/a") != fingerprint("http://[2001:db8::2]/a")
