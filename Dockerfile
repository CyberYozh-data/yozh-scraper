FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

ENV PATH=/app/.venv/bin:$PATH \
    \
    # Python
    PYTHONPATH=/app:$PYTHONPATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    \
    # Pip
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    \
    # Ruff
    RUFF_CACHE_DIR=/tmp

WORKDIR /app

COPY pyproject.toml requirements.txt requirements-dev.txt ./

RUN pip install -r requirements.txt

# Fetch the Camoufox browser binary + GeoIP data into the image at build time.
# Must run after camoufox is installed (above) and before COPY src so that a
# source-only change does not invalidate this ~1.3 GB download layer.
#
# PINNED, and deliberately so: a bare `camoufox fetch` takes whatever the
# upstream channel points at on build day, so the browser drifts with no commit
# recording it (measured 2026-09-02: the image ran beta.28, released 07-19,
# while beta.29 had been the stable release since 08-21). Bump this line to
# move the browser, and rebuild.
#
# The `&& python -c ...` is not belt-and-braces, it is the whole guarantee.
# It leans on two properties of `sys.exit`: `None` exits 0, and a string is
# written to stderr and exits 1. `assert` would read better and is strictly
# weaker — `-O` / PYTHONOPTIMIZE would delete a build-critical check.
# MEASURED: `camoufox fetch 999.0.4-beta.99` prints "not found in cache" and
# **exits 0 having installed nothing** — click's `return` is a success exit. So
# a typo here does not degrade to "latest"; it builds a ~1.3 GB-lighter image
# with NO browser, and the first Camoufox scrape then tries to download one
# inside the request, under the request timeout, on the worker. The assertion
# turns all three failure modes (wrong version resolved, nothing installed,
# camoufox unable to read its own install) into a red build.
#
# Google Chrome below is deliberately NOT pinned: `playwright install chrome`
# tracks Google stable, which is what we want it to do — there, current IS the
# goal, and the security patches ride along.
#
# The ARG sits HERE, above Chrome and the dev deps, and that placement costs
# ~870 MB of rebuild on every pin bump: changing an ARG's value invalidates the
# RUN that interpolates it AND every RUN below it, cached or not. Keeping it is
# the deliberate choice, because that invalidation is the ONLY thing that ever
# refreshes the Chrome layer — unpinned is not the same as current, and a layer
# nothing busts is how Camoufox got stale in the first place. Moving this block
# below `pip install -r requirements-dev.txt` would save the rebuild and leave
# Chrome frozen until someone edits requirements.txt. Do not move it without
# giving Chrome another way to advance.
#
# The trailing `-1bea4b55` is the asset sha8 and is NOT decoration. Camoufox
# resolves a bare `<version>-<build>` spec to the NEWEST asset for that build
# (`_resolve_spec` in its __main__.py: the sha-suffixed form matches an exact
# artifact, the bare form falls through to `latest_per_build`). So if upstream
# ever republishes an asset under this same tag, a bare pin would silently
# install different bytes under an identical version string — and
# `installed_verstr()` omits the sha, so nothing downstream could tell. The
# suffixed form is immutable; compare the install DIRECTORY, which carries it.
#
# SHELF LIFE, so a future build failure is read correctly: camoufox fetches
# `api.github.com/repos/<repo>/releases` with no pagination, i.e. the newest 30
# releases only. Once 30 newer Camoufox releases exist, this still-valid pin
# stops resolving and every clean build fails here. That is the safe direction
# — loud, not silent — and the fix is to bump the pin, which is the workflow
# anyway. "Not found in cache" on an untouched pin means it aged out, not that
# it is wrong.
ARG CAMOUFOX_VERSION=152.0.4-beta.29-1bea4b55
RUN python -m camoufox fetch "${CAMOUFOX_VERSION}" \
 && python -c "import sys,os;from camoufox.multiversion import get_active_path;pinned=sys.argv[1];active=get_active_path();installed=os.path.basename(str(active));sys.exit(None if installed==pinned else f'camoufox pin did not apply: installed {installed!r}, pinned {pinned!r}')" "${CAMOUFOX_VERSION}"

# Install a real Google Chrome (stable) alongside the bundled Chromium so the
# scraper can drive it via channel="chrome" (CHROME_CHANNEL=chrome) for better
# anti-bot fidelity — real branding/codecs, a populated navigator.plugins, and a
# newer engine than the pinned Chromium. No-op at runtime unless CHROME_CHANNEL
# is set. Kept before COPY src so a source change doesn't re-pull it.
RUN playwright install chrome

# Xvfb (virtual X display) so the browser can run *headful* on a headless
# server. Launch mode is per-request, so the entrypoint starts Xvfb
# unconditionally — one idle process per container, independent of HEADLESS.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

# Test/dev deps (pytest, fakeredis, ...). The production CMD never imports them;
# they live in the image so `docker compose run --rm web-scraper pytest` works on
# hosts without a local Python 3.12 toolchain (the K12 dev convention). Drop this
# layer behind a build target if a slim production image is ever needed.
RUN pip install -r requirements-dev.txt

COPY src /app/src
COPY scripts /app/scripts
RUN chmod +x /app/scripts/docker-entrypoint.sh

ENV HOST=0.0.0.0
ENV PORT=8000

# Entrypoint starts Xvfb (headful support is per-request) then execs the CMD / compose command.
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
