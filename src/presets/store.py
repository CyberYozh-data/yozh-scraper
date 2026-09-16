"""Persistence layer for presets.

Two backends:
  - `BuiltInRegistry` reads JSONs bundled at `src/presets/builtin/*.json`.
    Read-only.
  - `FilePresetStore` reads/writes JSONs under a mounted volume
    (`data/presets/*.json` in production, `tmp_path` in tests).

`PresetStore` is the public facade. `get()` resolves a name against built-ins
first, then user-defined presets; mutating calls (`create`/`update`/`delete`)
go to the user store and refuse to touch built-ins. User-created presets must
have a `user_` prefix to keep the namespace separate from future built-ins.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from src.presets.models import Preset, PresetKind

log = logging.getLogger(__name__)

USER_PREFIX = "user_"
# How long a writer waits for another writer before giving up. Bounded rather
# than blocking because `flock` is per open file description: a re-entrant
# update -- one running inside another one's `os.replace` -- would wait on
# itself forever, and a wedged holder would take every later writer with it.
# Real contention here is a couple of milliseconds; reaching this deadline
# means something is wrong, which is why it raises instead of writing anyway.
PRESET_LOCK_TIMEOUT_S = 10.0
DEFAULT_BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
DEFAULT_USER_DIR = Path("data/presets")


class PresetNotFound(KeyError):
    """No preset with this name in either store."""


class PresetAlreadyExists(ValueError):
    """A preset with this name already exists (built-in or user)."""


class PresetReadOnly(ValueError):
    """Attempted to mutate a built-in preset, or to create a user preset
    without the required `user_` prefix."""


class PresetNameInvalid(ValueError):
    """User preset name does not satisfy naming rules (e.g. missing prefix)."""


class PresetChangedSinceRead(ValueError):
    """The stored preset is no longer the one this write was computed from.

    Raised only for a write that asked to be conditional (`if_stamp`). The
    self-heal writer asks; a human PUT does not, so a person still overwrites
    whatever is there. A `ValueError` like the other store refusals, so the
    day an `If-Match` header reaches `update_preset` it can become a 409
    beside its siblings rather than a 500.
    """


class PresetLockUnavailable(RuntimeError):
    """Another writer held this preset's lock past the deadline.

    An operational failure, not a caller mistake: nothing here is safe to
    write without the lock, because the compare and the replace stop being
    one step and the write this guard exists to protect gets lost anyway.
    """


def _safe_name(name: str) -> str:
    """Defence-in-depth filename guard. The Pydantic validator already
    enforces snake_case, but we still want to refuse path-traversal attempts
    that might land here from a buggy caller.
    """
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"unsafe preset name: {name!r}")
    return name


def _stamp(raw: bytes) -> str:
    """Fingerprint of the exact bytes a preset was parsed from.

    Content, not `version`: a client PUTs the whole preset including its
    `version` field, so nothing forces a human edit to bump it, and a
    version-based check would miss the very edit it exists to protect.
    """
    return hashlib.sha256(raw).hexdigest()


def _parse_preset(raw: bytes, path: Path) -> Preset | None:
    try:
        data = json.loads(raw)
        return Preset.model_validate(data)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        log.warning("preset parse failed: %s (%s)", path, exc)
        return None


def _read_preset_file(path: Path) -> Preset | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("preset read failed: %s (%s)", path, exc)
        return None
    return _parse_preset(raw, path)


def _lock_path(path: Path) -> Path:
    """Sidecar lock file for a preset.

    Not `<name>.json.lock`: `create` accepts a name whose `<name>.json`
    already fills NAME_MAX, and five more characters make the lock
    unopenable -- i.e. the longest legal preset would be the one that cannot
    be written. A capped stem keeps it readable, the digest of the full name
    keeps it unique, and `.lock` keeps it out of the `*.json` glob.
    """
    digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
    return path.with_name(f"{path.stem[:32]}.{digest}.lock")


@contextlib.contextmanager
def _write_lock(path: Path, timeout_s: float | None = None) -> Iterator[None]:
    """Serialize writers of one preset across processes AND containers.

    `os.replace` swaps the inode, so a lock held on the preset file itself
    would not exclude a writer that opened it a moment later -- hence a
    sidecar file (see `_lock_path`), which is never replaced. It stays out of
    the `*.json` glob readers use, and is deliberately left behind: unlinking
    it reintroduces exactly the inode race it exists to close.

    Every write takes it, conditional or not. A write that proceeds without
    it is not merely unserialized: the compare and the replace stop being one
    step, so a human PUT landing in between is read as unchanged and
    overwritten -- the very loss the compare was added to prevent.

    `timeout_s` is how long to wait, defaulting to PRESET_LOCK_TIMEOUT_S --
    read here rather than bound as a default argument, which would freeze it
    at import and make the constant unchangeable at runtime. A person's PUT
    can afford that wait; the self-heal writer passes 0 and gives up
    instead, because it runs inside a scrape's budget and its write is
    best-effort by definition.

    The mode is group-writable because a second writer may not be the uid
    that created the file (audit H-06 step 3 moves these containers off root
    one service at a time), and it is set explicitly rather than left to the
    creation mode, which the umask narrows.
    """
    fd = os.open(_lock_path(path), os.O_CREAT | os.O_RDWR, 0o660)
    # The creation mode is masked -- under the usual umask 022 the sidecar
    # lands 0640, and the next writer, once these containers stop sharing a
    # uid, cannot open it at all. Unlike the preset itself the lock inode is
    # never replaced, so that first creation decides who may write the preset
    # from then on. Set the mode explicitly; suppressed because a lock we did
    # not create belongs to somebody else, and holding it is what matters.
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o660)
    try:
        budget = PRESET_LOCK_TIMEOUT_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    # Not contention: a filesystem without working locks
                    # (NFS with no lockd, some FUSE mounts) answers instantly
                    # and forever. Spinning out the deadline and then blaming
                    # "another writer" would send the next person hunting a
                    # writer that does not exist.
                    raise
                # The acquisition loop is kept clear of the `yield`: with the
                # yield inside this `except OSError`, an OSError from the
                # caller's body -- the failed write this lock exists to make
                # atomic -- is thrown back in here and swallowed as a failed
                # acquisition.
                if time.monotonic() >= deadline:
                    raise PresetLockUnavailable(
                        f"{path.name} is held by another writer"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the flock


class BuiltInRegistry:
    """Read-only loader for bundled presets."""

    def __init__(self, base_path: Path | None = None) -> None:
        self.base_path = base_path or DEFAULT_BUILTIN_DIR

    def list(self, *, source: str | None = None) -> list[Preset]:
        if not self.base_path.exists():
            return []
        items: list[Preset] = []
        for path in sorted(self.base_path.glob("*.json")):
            preset = _read_preset_file(path)
            if preset is None:
                continue
            if source is not None and preset.source != source:
                continue
            items.append(preset)
        return items

    def get(self, name: str) -> Preset:
        _safe_name(name)
        path = self.base_path / f"{name}.json"
        if not path.exists():
            raise PresetNotFound(name)
        preset = _read_preset_file(path)
        if preset is None:
            raise PresetNotFound(name)
        return preset

    def exists(self, name: str) -> bool:
        # cheap existence check that avoids parsing the JSON
        try:
            _safe_name(name)
        except ValueError:
            return False
        return (self.base_path / f"{name}.json").is_file()


class FilePresetStore:
    """Read-write JSON-on-disk store for user-defined presets."""

    def __init__(self, base_path: Path | None = None) -> None:
        self.base_path = base_path or DEFAULT_USER_DIR

    def _path(self, name: str) -> Path:
        _safe_name(name)
        return self.base_path / f"{name}.json"

    def _ensure_dir(self) -> None:
        self.base_path.mkdir(parents=True, exist_ok=True)

    def warn_if_not_writable(self) -> None:
        """Startup probe: log once when persisting to the store would fail.

        With every capability dropped (docker-compose.yml) root inside the
        container obeys file modes, so a bind-mounted store owned by the host
        user is read-only to us and each persist would fail one request at a
        time. Probes the directory or, before the first create, its nearest
        existing parent. Directory-level only: a preset file the host created
        as 0600 is still unreadable to us and surfaces per request. Never
        raises: the store is optional, the service is not.
        """
        detail = ""
        try:
            target = self.base_path
            while not target.exists() and target.parent != target:
                target = target.parent
            usable = os.access(target, os.R_OK | os.W_OK | os.X_OK)
        except OSError as exc:  # an unsearchable ancestor makes exists() itself raise
            target, usable, detail = self.base_path, False, f" ({exc})"
        if not usable:
            log.warning(
                "preset store %s is not readable, writable and searchable by uid %s%s: "
                "persists will fail (with every capability dropped, root obeys file "
                "modes; chmod o+rwx the directory on the host)",
                target, os.getuid(), detail,
            )

    def list(self, *, source: str | None = None) -> list[Preset]:
        if not self.base_path.exists():
            return []
        items: list[Preset] = []
        for path in sorted(self.base_path.glob("*.json")):
            preset = _read_preset_file(path)
            if preset is None:
                continue
            if source is not None and preset.source != source:
                continue
            items.append(preset)
        return items

    def get(self, name: str) -> Preset:
        path = self._path(name)
        if not path.exists():
            raise PresetNotFound(name)
        preset = _read_preset_file(path)
        if preset is None:
            raise PresetNotFound(name)
        return preset

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def get_stamped(self, name: str) -> tuple[Preset, str]:
        """The preset plus the fingerprint of the bytes it came from.

        One read, so the fingerprint cannot describe a different revision
        than the object returned -- which two calls (`get` then a stat/hash)
        could, in the direction that loses the newer file.
        """
        path = self._path(name)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            # `get` logs this through `_read_preset_file`; without the same
            # line here an unreadable file 404s with nothing to go on, on a
            # store whose permissions have bitten this project before.
            log.warning("preset read failed: %s (%s)", path, exc)
            raise PresetNotFound(name) from exc
        preset = _parse_preset(raw, path)
        if preset is None:
            raise PresetNotFound(name)
        return preset, _stamp(raw)

    def create(self, preset: Preset) -> Preset:
        path = self._path(preset.name)
        self._ensure_dir()
        payload = json.dumps(
            preset.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        # `x` mode = exclusive create; raises FileExistsError if another writer
        # got here first. Closes the TOCTOU window between `exists()` + write.
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(payload)
        except FileExistsError as exc:
            raise PresetAlreadyExists(preset.name) from exc
        return preset

    def update(
        self,
        name: str,
        preset: Preset,
        *,
        if_stamp: str | None = None,
        lock_timeout_s: float | None = None,
    ) -> Preset:
        """Replace a stored preset.

        `if_stamp` makes the write conditional on the file still being the
        one that fingerprint came from (see `get_stamped`), and raises
        `PresetChangedSinceRead` when it is not. Without it the write is
        unconditional, which is what a human PUT wants.
        """
        path = self._path(name)
        # Cheap early exit BEFORE the lock, which the authoritative check
        # inside it does not replace. Taking the lock creates a sidecar that is
        # deliberately never unlinked (see `_write_lock`), so doing it for a
        # preset that is not there writes a file nothing may clean up, under a
        # name the caller chose -- and these endpoints carry no token. It also
        # kept a fresh deploy from answering 404: with the directory not yet
        # created, the lock's own `os.open` raised FileNotFoundError first.
        if not path.exists():
            raise PresetNotFound(name)
        payload = json.dumps(
            preset.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        # Everything the write depends on is decided inside the lock. The
        # existence check used to sit outside it, so a DELETE that ran to
        # completion in between was undone by the replace below and the
        # preset came back after its owner had removed it. The stamp compare
        # is here for the same reason: read outside, two writers see the same
        # stamp, both pass, and the second still eats the first.
        with _write_lock(path, lock_timeout_s):
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError as exc:
                raise PresetNotFound(name) from exc
            if if_stamp is not None:
                try:
                    current = _stamp(path.read_bytes())
                except OSError as exc:
                    raise PresetNotFound(name) from exc
                if current != if_stamp:
                    raise PresetChangedSinceRead(name)
            # Write to a temp file in the same dir then os.replace — atomic on
            # the same filesystem, so a concurrent reader never sees a partial
            # file. mkstemp, not `<name>.<pid>.tmp`: worker containers share
            # this directory but not a pid namespace, so a pid-derived name
            # collided across containers.
            # The prefix is capped so the temp name never outgrows NAME_MAX
            # where the preset's own `<name>.json` still fits. Readers glob
            # `*.json`, so a temp orphaned by a SIGKILL is invisible and safe
            # to delete.
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=f"{path.stem[:32]}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    os.fchmod(fd, mode)  # mkstemp creates 0600; keep the mode
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):  # surface the write's error
                    Path(tmp).unlink(missing_ok=True)
                raise
        return preset

    def delete(self, name: str) -> None:
        path = self._path(name)
        # Cheap early exit BEFORE the lock, which the authoritative check
        # inside it does not replace. Taking the lock creates a sidecar that is
        # deliberately never unlinked (see `_write_lock`), so doing it for a
        # preset that is not there writes a file nothing may clean up, under a
        # name the caller chose -- and these endpoints carry no token. It also
        # kept a fresh deploy from answering 404: with the directory not yet
        # created, the lock's own `os.open` raised FileNotFoundError first.
        if not path.exists():
            raise PresetNotFound(name)
        # Under the write lock, so a write cannot land between the check and
        # the unlink -- and, with `update` checking existence inside the same
        # lock, cannot resurrect the preset after the unlink either.
        with _write_lock(path):
            try:
                path.unlink()
            except FileNotFoundError as exc:
                raise PresetNotFound(name) from exc


class PresetStore:
    """Facade over `BuiltInRegistry` + `FilePresetStore`.

    Read paths consult built-ins first (they win on name clashes), then user
    store. Write paths only touch the user store and refuse:
      - to overwrite a built-in name (PresetAlreadyExists),
      - to mutate a built-in (PresetReadOnly),
      - to create a user preset without the `user_` prefix (PresetReadOnly).
    """

    def __init__(
        self,
        *,
        builtin: BuiltInRegistry | None = None,
        user: FilePresetStore | None = None,
    ) -> None:
        self.builtin = builtin or BuiltInRegistry()
        self.user = user or FilePresetStore()

    def get(self, name: str) -> Preset:
        try:
            return self.builtin.get(name)
        except PresetNotFound:
            pass
        return self.user.get(name)

    def list(
        self,
        *,
        kind: PresetKind | None = None,
        source: str | None = None,
    ) -> list[Preset]:
        result: list[Preset] = []
        if kind in (None, "builtin"):
            result.extend(self.builtin.list(source=source))
        if kind in (None, "user"):
            result.extend(self.user.list(source=source))
        result.sort(key=lambda preset: preset.name)
        return result

    def create(self, preset: Preset) -> Preset:
        if self.builtin.exists(preset.name):
            raise PresetAlreadyExists(preset.name)
        if not preset.name.startswith(USER_PREFIX):
            raise PresetNameInvalid(
                f"user-defined preset name must start with {USER_PREFIX!r}: "
                f"got {preset.name!r}"
            )
        # Force kind="user" regardless of what the caller passed.
        normalized = preset.model_copy(update={"kind": "user"})
        return self.user.create(normalized)

    def get_stamped(self, name: str) -> tuple[Preset, str | None]:
        """Read a preset together with the stamp a later conditional `update`
        compares against.

        The stamp is None for a built-in, which resolves the same way `get`
        does: built-ins are read-only, so no writer can lose anything under
        one, and the caller carrying the stamp does not have to know which
        kind it asked for.
        """
        if self.builtin.exists(name):
            return self.builtin.get(name), None
        return self.user.get_stamped(name)

    def update(
        self,
        name: str,
        preset: Preset,
        *,
        if_stamp: str | None = None,
        lock_timeout_s: float | None = None,
    ) -> Preset:
        if self.builtin.exists(name):
            raise PresetReadOnly(f"cannot update built-in preset {name!r}")
        normalized = preset.model_copy(update={"kind": "user"})
        return self.user.update(
            name, normalized, if_stamp=if_stamp, lock_timeout_s=lock_timeout_s
        )

    def delete(self, name: str) -> None:
        if self.builtin.exists(name):
            raise PresetReadOnly(f"cannot delete built-in preset {name!r}")
        self.user.delete(name)
