"""A blocked attempt must not sit out the selector deadline it can never meet.

Measured on yandex_search (timeout_ms=45000), six sequential attempts through
the real preset: five were blocked, and on every one of them `page.url` was
already https://yandex.ru/showcaptcha the moment `page.goto` returned — yet the
selector wait still ran to its full 45.0s before anything looked at the verdict.
The one unblocked attempt spent 0.0s there. So the verdict is available before
the wait, and the wait is pure loss: ~45s per blocked attempt, up to three
attempts per request.

The guard is deliberately narrow. See `redirected_to_block`.
"""
from __future__ import annotations

import pytest

from src.browser.runner import looks_like_captcha_or_block, redirected_to_block


class TestRedirectedToBlock:
    """Only a redirect INTO a block endpoint counts."""

    def test_a_redirect_to_yandex_smartcaptcha_counts(self):
        assert redirected_to_block(
            "https://yandex.ru/search/?text=x",
            "https://yandex.ru/showcaptcha?cc=1&form-fb-hint=1.1",
        )

    def test_a_redirect_to_googles_sorry_counts(self):
        assert redirected_to_block(
            "https://www.google.com/search?q=x",
            "https://www.google.com/sorry/index?continue=https://www.google.com/",
        )

    def test_the_page_we_asked_for_never_counts(self):
        """The false positive that matters.

        `/sorry/` is a perfectly ordinary path — an apology page, a returns
        policy. A caller who navigated there ON PURPOSE has not been blocked,
        and skipping their selector wait would hand back a page that had not
        finished rendering. Only a URL we did not ask for can be a block.
        """
        assert not redirected_to_block(
            "https://shop.example/sorry/we-are-closed",
            "https://shop.example/sorry/we-are-closed",
        )

    def test_an_ordinary_redirect_does_not_count(self):
        assert not redirected_to_block(
            "https://yandex.ru/search/?text=x", "https://yandex.ru/search/?text=x&lr=213"
        )

    def test_no_current_url_does_not_count(self):
        assert not redirected_to_block("https://yandex.ru/search/?text=x", None)
        assert not redirected_to_block("https://yandex.ru/search/?text=x", "")

    def test_the_body_cannot_be_consulted_at_all(self):
        """Content phrases are the detector's other arm and must stay out.

        Right after `goto` the DOM is still settling, so a half-rendered page
        carrying a trigger phrase would fire — and the size ceiling that
        normally protects real content from exactly that has not been reached
        yet. The URL is the one signal that is true the instant navigation
        commits.

        Asserted on the signature rather than the source text: a function that
        takes no body cannot read one, whatever its comments happen to say.
        """
        import inspect

        params = list(inspect.signature(redirected_to_block).parameters)

        assert params == ["requested_url", "current_url"]


@pytest.mark.asyncio
async def test_camoufox_skips_the_selector_wait_once_the_url_says_blocked(monkeypatch):
    """The 45s this change exists to stop spending."""
    from tests.browser.test_camoufox_runner import _mock_page
    from src.browser.camoufox_runner import CamoufoxRunner

    page = _mock_page(monkeypatch)
    page.url = "https://yandex.ru/showcaptcha?cc=1"

    res = await CamoufoxRunner(timeout_ms=45000).fetch(
        url="https://yandex.ru/search/?text=x", device="desktop", proxy=None,
        headers=None, wait_until="load", wait_for_selector="li.serp-item",
        timeout_ms=45000, screenshot=False,
    )

    page.wait_for_selector.assert_not_awaited()
    # The verdict is unchanged — the fast path declines to WAIT, it does not
    # decide. The classifier still runs on the full page afterwards.
    assert res.ok is False
    assert res.blocked is True


@pytest.mark.asyncio
async def test_camoufox_still_waits_on_an_ordinary_page(monkeypatch):
    """The control: nothing about the normal path may change."""
    from tests.browser.test_camoufox_runner import _mock_page
    from src.browser.camoufox_runner import CamoufoxRunner

    page = _mock_page(monkeypatch)
    page.url = "https://yandex.ru/search/?text=x"

    await CamoufoxRunner(timeout_ms=45000).fetch(
        url="https://yandex.ru/search/?text=x", device="desktop", proxy=None,
        headers=None, wait_until="load", wait_for_selector="li.serp-item",
        timeout_ms=45000, screenshot=False,
    )

    page.wait_for_selector.assert_awaited_once()


class TestTheInterstitialThatIsNotABlock:
    """`/showcaptchafast` contains `/showcaptcha` and is NOT a block.

    It is Yandex's transparent browser check: it self-resolves in a few seconds
    and redirects to the real SERP. `yandex_search` ships
    `wait_for_selector: li.serp-item` precisely so the wait outlives it — and an
    earlier draft of the fast path skipped exactly that wait on a substring
    match, turning an 18-result SERP into a hard `blocked` and burning three
    exits chasing it. Reproduced against a real browser before it was narrowed.
    """

    REQUESTED = "https://yandex.ru/search/?text=x&lr=225"

    def test_the_self_resolving_interstitial_is_left_alone(self):
        assert not redirected_to_block(
            self.REQUESTED, "https://yandex.ru/showcaptchafast?cc=1&mt=ABC"
        )

    def test_the_real_smartcaptcha_still_counts(self):
        assert redirected_to_block(
            self.REQUESTED, "https://yandex.ru/showcaptcha?cc=1&mt=ABC"
        )

    def test_a_trailing_slash_does_not_hide_the_real_one(self):
        assert redirected_to_block(self.REQUESTED, "https://yandex.ru/showcaptcha/")

    def test_a_marker_in_the_query_string_is_not_a_path(self):
        """Google's own block URL embeds the page it blocked —
        /sorry/index?continue=<original> — so a whole-URL substring test would
        fire on whatever that original happened to contain."""
        assert not redirected_to_block(
            self.REQUESTED, "https://yandex.ru/search/?text=x&next=/showcaptcha"
        )
        assert redirected_to_block(
            "https://www.google.com/search?q=x",
            "https://www.google.com/sorry/index?continue=https://www.google.com/search",
        )


@pytest.mark.asyncio
async def test_playwright_runner_skips_the_wait_once_the_url_says_blocked():
    """The Chromium path had the identical waste, so it gets the identical gate."""
    from tests.browser.test_selector_timeout_classification import (
        CAPTCHA_HTML, CAPTCHA_URL, _page, _playwright_fetch,
    )

    page = _page(content=CAPTCHA_HTML, url=CAPTCHA_URL)

    res = await _playwright_fetch(page)

    page.wait_for_selector.assert_not_awaited()
    assert res.blocked is True
    assert res.ok is False


@pytest.mark.asyncio
async def test_playwright_runner_still_waits_on_an_ordinary_page():
    from tests.browser.test_selector_timeout_classification import (
        SERP_URL, _page, _playwright_fetch,
    )

    page = _page(content="<html>results</html>", url=SERP_URL, selector_times_out=False)

    await _playwright_fetch(page)

    page.wait_for_selector.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_self_resolving_interstitial_still_gets_its_wait():
    """The regression the narrowed matcher exists to prevent, end to end.

    `/showcaptchafast` resolves into a real SERP if the wait is allowed to
    outlive it; skipping the wait there was measured to turn 18 organic results
    into a hard `blocked` and burn three exits.
    """
    from tests.browser.test_selector_timeout_classification import _page, _playwright_fetch

    page = _page(
        content="<html>verification</html>",
        url="https://yandex.ru/showcaptchafast?cc=1&mt=ABC",
        selector_times_out=False,
    )

    await _playwright_fetch(page)

    page.wait_for_selector.assert_awaited_once()


class TestTheWarmupHopIsClassifiedToo:
    """The warmup was the one navigation with a pre-flight and no block check.

    `run_warmup` asserted the landing was PUBLIC — an egress question — and
    then declared success for anything that did not raise. A homepage warmup
    landing on `/sorry/` is the exit being turned away before the request we
    care about, and it reported `applied={...}` with no error: the refusal
    reached the caller only as an empty result page, one navigation later.
    """

    @staticmethod
    def _page(landed_on: str):
        from unittest.mock import AsyncMock, Mock

        page = AsyncMock()
        page.goto = AsyncMock(return_value=Mock(status=200))
        page.url = landed_on
        page.wait_for_timeout = AsyncMock()
        return page

    @pytest.mark.asyncio
    @pytest.mark.parametrize("landed", [
        "https://www.google.com/sorry/index?continue=x",
        "https://yandex.ru/showcaptcha?cc=1",
    ])
    async def test_a_warmup_that_lands_on_a_block_says_so(self, landed, monkeypatch):
        from unittest.mock import AsyncMock

        from src.browser import runner as mod

        monkeypatch.setattr(mod, "assert_navigable", AsyncMock())
        monkeypatch.setattr(mod, "assert_landing_public", AsyncMock())
        out = await mod.run_warmup(
            self._page(landed), "https://www.google.com/search?q=x",
            {"type": "homepage"}, timeout_ms=5000, default_dwell_ms=0,
        )
        assert out.blocked is True
        assert out.error is None, "nothing raised; it is not a failure"
        assert out.applied is not None, "it did run, and applied must say what"
        # The CARRIER, not just the field: `applied` is what every FetchResult
        # return in both runners passes on, and a mutation that left this False
        # while `out.blocked` stayed True survived the whole suite once.
        assert out.applied["blocked"] is True

    @pytest.mark.asyncio
    async def test_an_ordinary_warmup_is_not_flagged(self, monkeypatch):
        from unittest.mock import AsyncMock

        from src.browser import runner as mod

        monkeypatch.setattr(mod, "assert_navigable", AsyncMock())
        monkeypatch.setattr(mod, "assert_landing_public", AsyncMock())
        out = await mod.run_warmup(
            self._page("https://www.google.com/"), "https://www.google.com/search?q=x",
            {"type": "homepage"}, timeout_ms=5000, default_dwell_ms=0,
        )
        assert out.blocked is False
        assert out.applied == {
            "type": "homepage", "url": "https://www.google.com/", "dwell_ms": 0,
            "blocked": False,
        }

    @pytest.mark.asyncio
    async def test_a_warmup_nobody_configured_is_still_nothing(self, monkeypatch):
        from src.browser import runner as mod

        out = await mod.run_warmup(
            self._page("https://x.example/"), "https://x.example/a",
            None, timeout_ms=5000, default_dwell_ms=0,
        )
        assert (out.applied, out.error, out.blocked) == (None, None, False)


class TestAkamaiBehaviouralInterstitial:
    """Akamai answers at HTTP 200 from the requested URL, with no redirect.

    Measured 2026-09-11 on `suchen.mobile.de/fahrzeuge/details.html`: 6 of 6
    runs of the shipped `mobile_de_ad_*` presets landed on a 2.5-2.7 KB page
    with no `<title>`, zero `data-testid` nodes and an obfuscated sensor
    script -- and were reported as `selector_not_found`, i.e. as a broken
    recipe, when the exit had simply been refused. Neither the status code nor
    the final URL carries the news, so the body is the only place to read it.
    """

    SHELL = (
        '<!DOCTYPE html><html><head></head><body>'
        '<script type="text/javascript" src="/FrDk19/-2RwM/Ns8bx/GrCt/4N9V6k7f'
        '/GFM3AQ/Si5/pQxdGEBQb?v=f6ffd94c&amp;t=839117731"></script>'
        '<div id="sec-if-cpt-container" role="main" style="display: none">'
        '<div class="behavioral-content"><div id="sec-bc-text-container"></div>'
        '<div class="scf-akamai-logo-sec-abc"><div class="scf-akamai-logo-msg">'
        '<p class="scf-akamai-protected-by">Powered and protected by</p>'
        '</div></div></div></div></body></html>'
    )

    def test_the_interstitial_is_a_block(self):
        assert looks_like_captcha_or_block(self.SHELL) is True

    def test_it_is_a_block_even_though_the_url_never_changed(self):
        """The signal Google and Yandex give us -- a redirect to /sorry/ or
        /showcaptcha -- is absent here."""
        url = "https://suchen.mobile.de/fahrzeuge/details.html?id=391420794"
        assert looks_like_captcha_or_block(self.SHELL, final_url=url) is True

    def test_a_real_page_mentioning_akamai_is_not(self):
        """The needles are Akamai's own element id and class, not the visible
        text, which is localised -- and an article about Akamai must not read
        as a block."""
        page = (
            "<html><body><h1>Akamai Bot Manager review</h1>"
            "<p>Powered and protected by clever marketing, apparently. "
            "We tested the behavioral content challenge.</p>"
            + "<p>filler</p>" * 200
            + "</body></html>"
        )
        assert looks_like_captcha_or_block(page) is False
