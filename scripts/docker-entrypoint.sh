#!/bin/sh
# Container entrypoint. Headful browsers need an X display; on a headless server
# we provide a virtual one via Xvfb. Launch mode is per-request
# (ScrapeRequest.headless), so a container whose default is headless must still
# be able to serve a headful request — start Xvfb unconditionally. It is one
# idle ~40MB process per container and does not scale with browser count;
# headless Chromium simply ignores DISPLAY.
set -e

# Display number Xvfb serves. Overridable so tests can drive a throwaway
# display instead of the one this container serves requests on.
XVFB_DISPLAY="${XVFB_DISPLAY:-99}"

# DISPLAY already set means an operator pointed us at a real X server: use it
# and start nothing of our own.
if [ -z "${DISPLAY}" ]; then
    # -nolock skips X's lock-file arbitration, and we want it skipped. A
    # restarted container reuses its writable layer, so the previous run's
    # /tmp/.X<n>-lock survives into this one; X records the owning PID there and
    # refuses the display while that PID is alive, and a restart numbers PIDs
    # from scratch, so the recorded number is live again — often the very Xvfb
    # now starting. It then exits with "Server is already active" and every
    # headful request fails. Nothing else serves X in this container, so there
    # is nobody to arbitrate with.
    #
    # -displayfd makes Xvfb report its display number once it is ready to accept
    # connections. Waiting for the socket file to appear is not equivalent: a
    # socket left behind by the previous run satisfies that check instantly, so
    # the wait returns success and we advertise a display nothing serves.
    display_num=""
    # mktemp -d is atomic and 0700, so the fifo inside it cannot be pre-created
    # by anyone else. Guard both: a container that cannot write to /tmp must
    # still serve headless traffic rather than die in its entrypoint.
    fifo_dir="$(mktemp -d 2>/dev/null || true)"
    if [ -n "${fifo_dir}" ] && mkfifo "${fifo_dir}/display" 2>/dev/null; then
        # Hold both ends open for the container's lifetime, then unlink the
        # path: the inode outlives it, so nothing is left behind in /tmp, and
        # Xvfb never finds its reader gone if we stop waiting before it
        # reports — which would kill an otherwise healthy server.
        exec 4<>"${fifo_dir}/display"
        rm -rf "${fifo_dir}"

        # Xvfb's stderr goes to the container log on purpose: "Server is
        # already active", "Cannot establish any listening socket" and
        # "Cannot write display number to fd" mean very different things, and
        # the warning below can only say that something went wrong.
        Xvfb ":${XVFB_DISPLAY}" -nolock -displayfd 3 -screen 0 1920x1080x24 \
            -nolisten tcp 3>&4 >/dev/null &
        display_num="$(timeout 5 head -n 1 <&4 || true)"
    fi

    if [ -n "${display_num}" ]; then
        export DISPLAY=":${display_num}"
    else
        # Don't fail the container: headless requests (the common case) ignore
        # DISPLAY and must still be served. Leave DISPLAY unset so a headful
        # request fails fast with the runner's message naming Xvfb rather than
        # Playwright's opaque "Missing X server", and say so loudly here —
        # otherwise the only symptom is every headful request failing with no
        # clue that Xvfb is the cause.
        echo "WARNING: Xvfb display :${XVFB_DISPLAY} did not come up; headful requests will fail (headless is unaffected)" >&2
    fi
fi

# Camoufox re-downloads the MaxMind GeoIP database, ~45 MB, from inside the
# launch path when the file on disk is older than 30 days:
# `get_geolocation()` -> `if not mmdb_path.exists() or needs_update(): download_mmdb()`
# (camoufox/geolocation.py). That is not a startup task there — it happens
# during a scrape, on the request that happens to be first past the 30-day
# mark, and it goes straight to the internet: not through the request's proxy,
# and not past this service's egress guard, which only covers the browser.
# A month after any image build, one unlucky scrape pays a 45 MB download or
# fails on a network that does not allow it.
#
# The database is baked into the image, so it is exactly as fresh as the image
# and re-downloading buys nothing between rebuilds. Stamping it at start keeps
# `needs_update()` false for the container's life; a rebuild ships a new copy.
# Deliberately quiet about failure: an unwritable cache must not stop a
# container that serves Chromium requests perfectly well.
CAMOUFOX_GEOIP_DIR="${CAMOUFOX_GEOIP_DIR:-/root/.cache/camoufox/geoip/mmdb}"
if [ -d "${CAMOUFOX_GEOIP_DIR}" ]; then
    if find "${CAMOUFOX_GEOIP_DIR}" -name '*.mmdb' -exec touch {} + 2>/dev/null; then
        echo "camoufox geoip database stamped fresh (no mid-scrape re-download)"
    else
        echo "WARNING: could not stamp ${CAMOUFOX_GEOIP_DIR}; camoufox may re-download the GeoIP database mid-scrape once it is 30 days old" >&2
    fi
fi

exec "$@"
