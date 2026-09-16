"""What the browser process is allowed to inherit.

Measured on a real headful Chrome in the service image, launching
`https://example.com/` with three canaries in the service environment:
without the narrowing, all three sat in `/proc/<chrome>/environ` (25-27
variables across the browser and both crashpad handlers); with it, 5-7
variables and none of the canaries, page still fetched. Camoufox, measured
the same way: 6 processes, no canary, CAMOU_CONFIG chunk intact, forced
window and spoofed UA unchanged.
"""
from __future__ import annotations

import pytest

from src.browser.launch_env import BROWSER_ENV_KEYS, browser_env


class TestNothingSecretReachesTheBrowser:
    def test_a_variable_nobody_allowed_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The whole point: the browser renders caller-supplied URLs and can
        read its own environ, so a name that was never vetted must not be
        there -- whatever it is called."""
        monkeypatch.setenv("SERVICE_TOKEN", "s3cret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("PROXY_PASSWORD", "hunter2")
        monkeypatch.setenv("SOMETHING_INVENTED_NEXT_QUARTER", "also-secret")

        env = browser_env()

        assert "SERVICE_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert "PROXY_PASSWORD" not in env
        assert "SOMETHING_INVENTED_NEXT_QUARTER" not in env

    def test_the_display_the_headful_launch_needs_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A narrowing that drops DISPLAY turns every headful request into
        Playwright's opaque 'Missing X server'."""
        monkeypatch.setenv("DISPLAY", ":99")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/root")

        env = browser_env()

        assert env["DISPLAY"] == ":99"
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/root"

    def test_an_allowed_name_that_is_unset_does_not_appear_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An empty TZ is not the same as an absent one: the browser reads ""
        as a timezone rather than falling back to its default. Asserted
        against a set value too, so the test cannot pass by filtering
        nothing."""
        monkeypatch.setenv("TZ", "Europe/Moscow")
        assert browser_env()["TZ"] == "Europe/Moscow"

        monkeypatch.delenv("TZ")
        assert "TZ" not in browser_env()

    def test_fontconfig_is_not_inherited(self, monkeypatch: pytest.MonkeyPatch):
        """Camoufox generates a fontconfig per launch and merges it UNDER the
        env we pass. Inheriting ours would replace the font set its
        fingerprint claims -- silently, and on every Camoufox request."""
        monkeypatch.setenv("FONTCONFIG_FILE", "/etc/fonts/fonts.conf")
        monkeypatch.setenv("FONTCONFIG_PATH", "/etc/fonts")

        env = browser_env()

        assert "FONTCONFIG_FILE" not in env
        assert "FONTCONFIG_PATH" not in env

    def test_the_list_is_positive_not_a_denylist(self):
        """A denylist of secret-looking names fails open on the next name
        somebody adds -- this codebase has already paid for that once, with a
        header-name denylist that let `apikey` through."""
        assert BROWSER_ENV_KEYS
        assert all(isinstance(key, str) for key in BROWSER_ENV_KEYS)
        assert not any(
            "SECRET" in key or "TOKEN" in key or "KEY" in key
            for key in BROWSER_ENV_KEYS
        )

    def test_it_reads_the_live_environment(self, monkeypatch: pytest.MonkeyPatch):
        """Not a snapshot taken at import: the entrypoint exports DISPLAY
        before the app starts, but a test (and an operator) can change it
        after."""
        monkeypatch.setenv("DISPLAY", ":1234")
        assert browser_env()["DISPLAY"] == ":1234"
        monkeypatch.setenv("DISPLAY", ":5678")
        assert browser_env()["DISPLAY"] == ":5678"

    def test_every_allowed_name_is_one_the_environment_can_actually_hold(self):
        """A name that cannot appear in an environment is silently never
        matched, so nothing fails -- the variable just goes missing."""
        for key in BROWSER_ENV_KEYS:
            assert key == key.strip(), key
            assert " " not in key and "=" not in key and "\0" not in key, key
            assert key == key.upper(), key
