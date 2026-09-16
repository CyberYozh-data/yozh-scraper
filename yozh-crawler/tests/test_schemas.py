from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas import CrawlRequest, CrawlScope


def test_crawl_request_accepts_wait_until_load():
    req = CrawlRequest(seed_url="https://example.com", scrape_options={"wait_until": "load"})
    assert req.scrape_options.wait_until == "load"


def test_a_malformed_scope_regex_is_rejected_at_the_boundary():
    """Audit H-15: a bad pattern is a 422, not a `re.error` inside the worker."""
    with pytest.raises(ValidationError, match="invalid regex"):
        CrawlScope(include_patterns=["("])
    with pytest.raises(ValidationError, match="invalid regex"):
        CrawlScope(exclude_patterns=["[unclosed"])
    assert CrawlScope(include_patterns=[r"^/blog/.*$"]).include_patterns == [r"^/blog/.*$"]
