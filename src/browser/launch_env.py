"""The environment a browser process is started with.

Playwright and Camoufox both hand the browser this service's own environment
unless told otherwise, and this service's environment is where the service
token, the LLM keys and the proxy passwords live. The browser needs none of
them: it renders a caller-supplied URL, under `--no-sandbox`, in a process
that reads its own `/proc/self/environ` freely. Anything that gets code
running there -- a renderer escape, a malicious extension, a bug in a codec
-- currently reads the lot.

What this does NOT do: the Playwright driver (node) still holds the full
environment and runs as the same uid, so an escape complete enough to read
another process's `/proc/<pid>/environ` still gets there. This closes the
browser and its crashpad handlers, which is where the untrusted page runs.

The list below is POSITIVE by construction. A denylist of secret-looking
names fails open on the next variable somebody adds: this codebase has been
bitten by exactly that (a header-name denylist that let `apikey` through).
Adding a name here is a deliberate act; forgetting to add one costs a launch
failure that is loud, not a leak that is silent.
"""
from __future__ import annotations

import os

# Vetted one at a time against a real headful launch of Chrome and Camoufox.
BROWSER_ENV_KEYS = frozenset({
    # Process basics. Without PATH the browser cannot find its own helper
    # binaries; without HOME it writes its profile wherever it lands.
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LD_LIBRARY_PATH",
    # The X display the entrypoint's Xvfb serves. A headful launch without it
    # dies with Playwright's opaque "Missing X server".
    "DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    # Locale and timezone. Per-request values are set on the context, but the
    # process defaults still show through in places a context cannot reach.
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_TIME",
    "TZ",
    # FONTCONFIG_* is deliberately absent: Camoufox generates its own
    # fontconfig per launch and merges it UNDER the env we pass, so passing
    # ours would silently replace the font set its fingerprint claims.
})


def browser_env() -> dict[str, str]:
    """This process's environment, narrowed to what a browser needs.

    Read live rather than snapshotted at import: the entrypoint exports
    DISPLAY before the app starts, but an operator can point a container at
    another X server without a rebuild.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in BROWSER_ENV_KEYS
    }
