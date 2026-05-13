from __future__ import annotations

from src.schemas import CrawlScope
from src.scope import CompositeScope


SEED = "https://example.com/"


def _scope(**overrides) -> CompositeScope:
    return CompositeScope(CrawlScope(**overrides), SEED)


def test_same_domain_accepts_seed_host():
    s = _scope(mode="same-domain", max_depth=3)
    assert s.allows("https://example.com/about", depth=1)
    assert not s.allows("https://other.com/", depth=1)


def test_same_domain_rejects_subdomain():
    s = _scope(mode="same-domain", max_depth=3)
    assert not s.allows("https://blog.example.com/", depth=1)
    assert s.reason("https://blog.example.com/", 1) == "mode"


def test_subdomains_accepts_subdomain():
    s = _scope(mode="subdomains", max_depth=3)
    assert s.allows("https://blog.example.com/", depth=1)
    assert s.allows("https://example.com/x", depth=1)
    assert not s.allows("https://other.com/", depth=1)


def test_all_accepts_any_http_host():
    s = _scope(mode="all", max_depth=3)
    assert s.allows("https://other.com/", depth=1)
    assert not s.allows("ftp://example.com/", depth=1)


def test_max_depth_enforced():
    s = _scope(mode="same-domain", max_depth=2)
    assert s.allows("https://example.com/x", depth=2)
    assert s.reason("https://example.com/x", 3) == "max_depth"


def test_exclude_wins_over_include():
    s = _scope(
        mode="all",
        max_depth=3,
        include_patterns=[r"/blog/"],
        exclude_patterns=[r"/blog/private/"],
    )
    assert s.allows("https://x.com/blog/post", depth=1)
    assert s.reason("https://x.com/blog/private/post", 1) == "exclude"


def test_regex_mode_requires_include_match():
    s = _scope(
        mode="regex",
        max_depth=3,
        include_patterns=[r"^https://example\.com/blog/"],
    )
    assert s.allows("https://example.com/blog/1", depth=1)
    assert s.reason("https://example.com/about", 1) == "include"
