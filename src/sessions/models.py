from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.schemas import Device, ProxyGeo, ScrapeProxyType, Viewport
from src.security.egress import EgressBlocked, assert_http_scheme

SessionStatus = Literal["created", "logging_in", "ready", "expired", "failed"]

LoginOp = Literal[
    "goto",
    "fill",
    "click",
    "wait_for_selector",
    "wait_for_timeout",
    "press_key",
    "type_text",
    "hover",
]


class LoginStep(BaseModel):
    op: LoginOp
    selector: str | None = None
    url: str | None = None
    value: str | None = Field(
        default=None,
        description="Supports $creds_<key> substitution from the login request's `creds` dict.",
    )
    key: str | None = None
    timeout_ms: int | None = Field(default=None, ge=0, le=120_000)
    ms: int | None = Field(default=None, ge=0, le=120_000)

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str | None) -> str | None:
        """`ScrapeRequest.url` is an `HttpUrl`; this was a bare `str`.

        A `file://` or `chrome://` step opens no TCP connection, so neither the
        address predicate nor the route guard can see it — the schema is the
        only layer that can. Note this validates the TEMPLATE: `_dispatch`
        re-checks after `$creds_` substitution.
        """
        if value is None:
            return None
        try:
            assert_http_scheme(value)
        except EgressBlocked as exc:
            raise ValueError("login step url must be http:// or https://") from exc
        return value


class LoginScript(BaseModel):
    steps: list[LoginStep]
    success_selector: str | None = None
    success_url_regex: str | None = None


class SessionCreateRequest(BaseModel):
    device: Device = "desktop"
    viewport: Viewport | None = Field(
        default=None,
        description=(
            "Browser viewport (and matching window.screen) pinned for this "
            "session's login and every scrape on it. Defaults to the device "
            "preset size when unset. Pinning it keeps the screen size identical "
            "across the whole session — a size that changed mid-session would be "
            "a fingerprint tell."
        ),
    )
    proxy_type: ScrapeProxyType = "none"
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None
    ttl_seconds: int = Field(default=86400, ge=300, le=7 * 86400)

    @model_validator(mode="after")
    def _refuse_a_device_the_login_cannot_use(self) -> "SessionCreateRequest":
        # The device pins the LOGIN as well as every scrape on the session, and
        # a login runs JavaScript by definition -- forms, challenges, the lot.
        # `legacy_wap` claims a phone that predates it, so the credentials would
        # go out under an identity the page can prove is impossible. The scrape
        # side answers the same pairing with `render=false`; a login has no such
        # escape, so the device is refused outright here.
        if self.device == "legacy_wap":
            raise ValueError(
                "device='legacy_wap' cannot be pinned to a session: the login "
                "runs JavaScript, which that identity claims not to have. Use "
                "desktop or mobile for the session, and legacy_wap on the "
                "individual scrape that needs the no-JS layout."
            )
        return self


class SessionLoginRequest(BaseModel):
    script: LoginScript
    creds: dict[str, str] = Field(
        ...,
        description=(
            "Free-form credential bag. Keys become $creds_<key> placeholders in "
            "LoginStep.value/url. Lives in RAM for the duration of this request "
            "only; never persisted."
        ),
    )


class SessionLoginResult(BaseModel):
    ok: bool
    failed_step_index: int | None = None
    failed_step: LoginStep | None = None
    error: str | None = None
    screenshot_b64: str | None = None
    took_ms: int = 0


class SessionRecord(BaseModel):
    session_id: str
    status: SessionStatus
    created_at: float
    expires_at: float
    last_used_at: float

    device: Device
    viewport: Viewport | None = None
    proxy_type: ScrapeProxyType
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None

    storage_state: dict[str, Any] | None = None
    storage_state_bytes: int = 0
    login_script: LoginScript | None = None
    last_error: str | None = None


class SessionPublic(BaseModel):
    """`SessionRecord` minus `storage_state` — what `GET /sessions/{id}` returns."""

    session_id: str
    status: SessionStatus
    created_at: float
    expires_at: float
    last_used_at: float
    device: Device
    viewport: Viewport | None = None
    proxy_type: ScrapeProxyType
    proxy_pool_id: str | None = None
    proxy_geo: ProxyGeo | None = None
    storage_state_bytes: int = 0
    last_error: str | None = None


class SessionList(BaseModel):
    items: list[SessionPublic]


class SessionCookieInjection(BaseModel):
    cookies: list[dict[str, Any]]


class SessionCreated(BaseModel):
    session_id: str
    expires_at: float
