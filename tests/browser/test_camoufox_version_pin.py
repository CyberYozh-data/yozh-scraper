"""The installed Camoufox browser must be the artifact the tree pins.

The Dockerfile is the primary guard — it asserts installed == pinned inside the
same `RUN` as the fetch, so a pin that does not resolve can never produce an
image. Read the `ARG CAMOUFOX_VERSION` comment there for why that is necessary,
and for why the pin carries the asset sha8; neither is repeated here.

This module catches the state the build cannot see, because it was fine at
build time: an image that is STALE relative to the tree — built before the
current pin, or with a `--build-arg` that was never committed.

## Why the skip keys on /ms-playwright

Two earlier versions of this file got the gate wrong in the same way, and the
shape of the mistake is worth keeping:

  1. It asked camoufox for the installed version and skipped when that raised.
     But "camoufox installed, browser missing" is exactly what a broken pin
     leaves behind — the failure was reported as a green skip.
  2. It then keyed the skip on `CAMOUFOX_VERSION`, an env var this same commit
     introduced. An image built BEFORE this patch does not carry it — so the
     module skipped green on precisely the first stale image it advertises
     catching. A marker minted by the thing under test cannot test it.

`/ms-playwright` is the discriminator because it predates all of this: it comes
from the `mcr.microsoft.com/playwright/python` base image, so it is present in
every image this Dockerfile has ever produced, old and new, and absent from a
bare checkout and from the CI test job (ubuntu-latest + pip, no Docker).
Being a directory rather than an env var, no compose `environment:` or `.env`
entry can conjure or clear it.

Where a browser IS expected, every way of having the wrong one — a camoufox too
old to answer, no install at all, an install that disagrees with the tree — is
a failure that names the stale image. None of them is a skip.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"

# Set by the base image; see the module docstring. Checked as a path, not an
# env var, so host configuration cannot fake it either way.
_IMAGE_MARKER = Path("/ms-playwright")

# Matches the build arg the fetch step interpolates. Deliberately strict: a
# reformat that this stops matching fails the test loudly ("no pin found")
# instead of quietly asserting nothing.
_PIN_RE = re.compile(r"^ARG\s+CAMOUFOX_VERSION=(\S+)\s*$", re.MULTILINE)


def declared_pin() -> str:
    """The artifact committed to the tree, read from the Dockerfile itself."""
    match = _PIN_RE.search(DOCKERFILE.read_text(encoding="utf-8"))
    assert match, (
        f"no `ARG CAMOUFOX_VERSION=<version>` line in {DOCKERFILE}. The browser "
        "binary must be pinned; see that file's comment."
    )
    return match.group(1)


def test_the_dockerfile_still_pins_a_camoufox_browser_artifact():
    """The one check that runs everywhere, including CI, where no browser exists.

    Deleting the `ARG` line reds this even on a bare checkout, which matters
    because every other guard here and in the Dockerfile is downstream of it.
    """
    declared_pin()


def test_the_installed_browser_is_the_artifact_the_tree_pins():
    """Compares the install directory, which carries the sha8 the pin names.

    `installed_verstr()` is deliberately NOT used: it reports `152.0.4-beta.29`
    with the asset sha stripped, so it cannot tell two different artifacts
    published under one version apart — the exact drift the sha8 pin exists to
    stop.
    """
    if not _IMAGE_MARKER.is_dir():
        pytest.skip(
            f"{_IMAGE_MARKER} is absent, so this is not a built image and no "
            "Camoufox browser is expected (bare checkout / CI test job)."
        )

    declared = declared_pin()
    stale = (
        f"the tree pins {declared!r}, but this image %s. Either it predates the "
        "current pin (rebuild it) or it was built with a --build-arg override "
        "that is not committed."
    )

    try:
        # `multiversion` arrived in camoufox 0.5. An image old enough to lack it
        # is by definition older than this pin, so report THAT rather than
        # letting a bare ImportError read as a broken test.
        from camoufox.multiversion import get_active_path
    except ImportError as exc:
        pytest.fail(stale % f"runs a camoufox too old to have {exc.name!r}")

    # Returns None rather than raising when nothing is installed — which is what
    # a fetch that resolved nothing leaves behind.
    active = get_active_path()
    if active is None:
        pytest.fail(stale % "has no Camoufox browser installed at all")

    installed = os.path.basename(str(active))
    assert installed == declared, stale % f"has {installed!r}"
