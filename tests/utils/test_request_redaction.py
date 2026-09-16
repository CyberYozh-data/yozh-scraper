"""`redact_request_secrets`: the job's request echo without the caller's secrets (audit H-09)."""
from __future__ import annotations

from src.schemas import Cookie, JobResultsResponse, ScrapeRequest
from src.utils.redaction import REDACTED, redact_request_secrets


def test_every_header_value_is_masked_and_every_name_kept():
    # Names only: a list of secret header names failed open on `apikey`,
    # `Private-Token`, `X-Amz-Security-Token` (review, 2026-09-05).
    page = ScrapeRequest(
        url="https://example.com",
        headers={"Authorization": "Bearer S3CRET", "apikey": "LEAK1", "Private-Token": "LEAK2",
                 "Accept-Language": "de-DE"},
        cookies=[Cookie(name="sid", value="COOKIE-S3CRET", domain="example.com")],
    )
    out = redact_request_secrets(page)
    assert list(out.headers) == ["Authorization", "apikey", "Private-Token", "Accept-Language"]
    assert set(out.headers.values()) == {REDACTED}
    assert [(c.name, c.value, c.domain) for c in out.cookies] == [("sid", REDACTED, "example.com")]
    # The input is untouched: the store's copy is what the worker reads.
    assert page.headers["Authorization"] == "Bearer S3CRET"
    assert page.cookies[0].value == "COOKIE-S3CRET"


def test_the_session_id_is_masked_and_a_sticky_proxy_label_dropped():
    page = ScrapeRequest(url="https://example.com", proxy_type="prem_res_rotating",
                         prem_proxy_options={"session_type": "sticky", "sticky_id": "ridemyexit"})
    page = page.model_copy(update={"session_id": "sess_0123456789abcdef"})
    out = redact_request_secrets(page)
    assert out.session_id == REDACTED
    assert out.prem_proxy_options.sticky_id is None, "the validator admits alphanumerics only"


def test_the_redacted_echo_still_validates_as_a_response():
    """A client re-parsing the echo with our own models must not choke on the
    marker (codex, 2026-09-05: `<redacted>` in `sticky_id` did)."""
    page = ScrapeRequest(url="https://example.com", proxy_type="prem_res_rotating",
                         headers={"Authorization": "Bearer S3CRET"},
                         cookies=[Cookie(name="sid", value="S3CRET")],
                         prem_proxy_options={"session_type": "sticky", "sticky_id": "ridemyexit"})
    page = page.model_copy(update={"session_id": "sess_0123456789abcdef"})
    resp = JobResultsResponse(job_id="req_x", status="done", pages=[redact_request_secrets(page)],
                              total=1, done=1, results=[None])
    assert JobResultsResponse.model_validate_json(resp.model_dump_json()).pages[0].session_id == REDACTED


def test_a_page_without_secrets_is_unchanged():
    page = ScrapeRequest(url="https://example.com", device="mobile")
    assert redact_request_secrets(page) == page
