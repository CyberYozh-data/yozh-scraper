from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from src.proxy.base import ProxyFailure, ProxyLease
from src.proxy.cyberyozh.client import CyberYozhClient, OrderedProxy
from src.proxy.models import ProxyConfig
from src.settings import settings
from src.utils.redaction import redact_url

log = logging.getLogger(__name__)


def _geo_label(geo: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in geo.items())


def _split_rotating_credentials(line: str) -> tuple[str, str, str]:
    """`user:pass@host:port` -> (server, username, password), logged safely.

    The username was logged in the clear on every parse, four lines under a
    comment saying credentials must never reach logs or 502 bodies. On this
    provider the username is not a mere identifier either — it encodes the
    session and geo targeting, so it is the half worth stealing. Lengths keep
    what the line was actually used for (spotting an empty or truncated half)
    without carrying the value.
    """
    if "@" not in line:
        raise RuntimeError("Invalid credential format (no @)")
    auth_part, location_part = line.rsplit("@", 1)
    if ":" not in auth_part:
        raise RuntimeError("Invalid auth format (no :)")
    username, password = auth_part.split(":", 1)
    server = f"http://{location_part}"
    log.info(
        "parsed rotating credentials: server=%s, username_len=%d, password_len=%d",
        server, len(username), len(password),
    )
    return server, username, password


def normalize_proxy_raw_type(proxy_type_raw: str) -> str:
    if proxy_type_raw == "mobile_shared":
        return "mobile"
    return proxy_type_raw


def get_category_proxy(proxy_type_norm: str) -> str:
    if proxy_type_norm in ("mobile", "lte"):
        return "lte"
    if proxy_type_norm in ("res_rotating", "residential_rotating", "rotating"):
        return "residential_rotating"
    if proxy_type_norm in ("res_static", "residential", "residential_static"):
        return "residential_static"
    if proxy_type_norm in ("dc_static", "datacenter"):
        return "datacenter"
    return "residential_static"


def _server(url: str) -> str:
    """Parse proxy URL and return in format scheme://host:port"""
    clean_url = url.replace("socks5_http://", "socks5://")
    parsed_url = urlparse(clean_url)
    scheme = parsed_url.scheme or "http"
    host = parsed_url.hostname or ""
    port = int(parsed_url.port or 0)

    if not host or not port:
        raise ValueError(f"Invalid proxy URL: {url}")

    return f"{scheme}://{host}:{port}"


@dataclass
class CyberYozhProxyProvider:
    client: CyberYozhClient
    geo: dict[str, str] | None = None

    def max_attempts(self, proxy_type_raw: str, override: int | None = None) -> int:
        # All proxy types share the same cap now — configurable via MAX_RETRIES,
        # or per-request via ScrapeRequest.max_retries (override). Static types
        # used to be hard-capped at 2 attempts, but there is no fundamental
        # reason for that: the same pool rotation logic applies.
        base = settings.max_retries if override is None else override
        return max(1, int(base))

    async def acquire(self, proxy_type_raw: str, proxy_pool_id: str | None) -> ProxyLease:
        return await self.rotate_next(
            proxy_type_raw=proxy_type_raw,
            proxy_pool_id=proxy_pool_id,
            exclude_source_ids=set(),
        )

    async def rotate_next(
        self,
        *,
        proxy_type_raw: str,
        proxy_pool_id: str | None,
        exclude_source_ids: set[str],
    ) -> ProxyLease:
        proxy_type = normalize_proxy_raw_type(proxy_type_raw)
        category = get_category_proxy(proxy_type)

        items = await self.client.proxy_history(category=category, expired=False)

        log.info(
            "rotate_next: received %d proxies from API for category=%s, excluding %d",
            len(items),
            category,
            len(exclude_source_ids)
        )

        available = [proxy for proxy in items if self._ok(proxy) and str(proxy.id) not in exclude_source_ids]

        log.info(
            "rotate_next: %d proxies available after filtering",
            len(available)
        )

        if not available:
            log.error(
                "no_more_proxies: category=%s, total=%d, ok=%d, excluded=%d",
                category,
                len(items),
                sum(1 for proxy in items if self._ok(proxy)),
                len(exclude_source_ids)
            )
            raise RuntimeError(
                f"no_more_proxies: category={category}, "
                f"total={len(items)}, excluded={len(exclude_source_ids)}"
            )

        if proxy_pool_id:
            for proxy in available:
                if str(proxy.id) == str(proxy_pool_id):
                    log.info("found proxy_pool_id=%s", proxy_pool_id)
                    return await self._to_lease(proxy_type, proxy, geo=self.geo)
            log.warning("proxy_pool_id=%s not found, using any available", proxy_pool_id)

        selected = available[0]
        log.info("selected proxy id=%s", selected.id)
        return await self._to_lease(proxy_type, selected, geo=self.geo)

    async def recover(
        self,
        *,
        proxy_type_raw: str,
        proxy_pool_id: str | None,
        lease: ProxyLease | None,
        exclude_source_ids: set[str],
        failure: ProxyFailure,
    ) -> tuple[ProxyLease | None, bool]:
        proxy_type = normalize_proxy_raw_type(proxy_type_raw)

        log.info(
            "recover: proxy_type=%s, status=%s, error=%s",
            proxy_type,
            failure.status_code,
            failure.error[:100] if failure.error else None
        )

        # For rotating just get new credentials
        if proxy_type in ("res_rotating", "residential_rotating", "rotating"):
            log.info("rotating proxy: requesting new credentials")
            try:
                new_lease = await self.rotate_next(
                    proxy_type_raw=proxy_type_raw,
                    proxy_pool_id=proxy_pool_id,
                    exclude_source_ids=exclude_source_ids,
                )
                log.info("got new rotating credentials")
                return new_lease, True
            except Exception as e:
                log.error("failed to get new credentials: %s", e)
                return lease, False

        # For not-rotating exclude current proxy
        if lease and lease.source_id:
            log.info("excluding source_id=%s", lease.source_id)
            exclude_source_ids.add(str(lease.source_id))

        # Dedicated mobile: attempt change_ip on the modem. Shared mobile cannot
        # change IP because the proxy is shared across users — skip straight to
        # rotating to the next proxy.
        if (
            proxy_type_raw != "mobile_shared"
            and proxy_type in ("mobile", "lte")
            and lease
            and lease.change_ip_links
        ):
            try:
                log.info("attempting change_ip for mobile")
                await self.client.call_change_ip_link(lease.change_ip_links[0])
                log.info("change_ip successful")
                return lease, True
            except Exception as e:
                log.warning("change_ip failed: %s", e)

        # rotate to next
        try:
            new_lease = await self.rotate_next(
                proxy_type_raw=proxy_type_raw,
                proxy_pool_id=proxy_pool_id,
                exclude_source_ids=exclude_source_ids,
            )
            log.info("rotated to next proxy")
            return new_lease, True
        except Exception as e:
            log.error("failed to rotate: %s", e)
            return lease, False

    def _ok(self, proxy: OrderedProxy) -> bool:
        return bool(proxy.url) and proxy.status == "active" and not proxy.expired

    async def _to_lease(
        self,
        proxy_type_norm: str,
        proxy: OrderedProxy,
        geo: dict[str, str] | None = None
    ) -> ProxyLease:
        if proxy_type_norm in ("res_rotating", "residential_rotating", "rotating"):
            host = proxy.connection_host
            port = proxy.connection_port

            if not host or not port:
                log.warning("connection_host/port missing, parsing url=%s", redact_url(proxy.url))
                clean_url = proxy.url.replace("socks5_http://", "http://")
                parsed_url = urlparse(clean_url)
                host = parsed_url.hostname
                port = parsed_url.port

            log.debug("rotating proxy: host=%s, port=%s", host, port)

            if not host or not port:
                raise RuntimeError(f"missing host/port: url={redact_url(proxy.url)}")

            src_payload = {
                "connection_login": proxy.login,
                "connection_password": proxy.password,
                "connection_host": str(host),
                "connection_port": str(port),
                "session_type": "random",
            }

            payload_with_extra = src_payload.copy()
            # Add geo-params if provided
            if geo:
                if geo.get("country_code"):
                    payload_with_extra["country_code"] = geo["country_code"]
                if geo.get("region"):
                    payload_with_extra["region"] = geo["region"]
                if geo.get("city"):
                    payload_with_extra["city"] = geo["city"]

            log.info(
                "requesting rotating credentials: host=%s, port=%s, geo=%s",
                host,
                port,
                geo if geo else "any"
            )

            if geo:
                # No silent fallback: a lease without the requested geo ships
                # a page from an unknown country under a fingerprint derived
                # from the requested one, and a country-scoped caller files it
                # under its own market (audit 2026-09-03, H-12). One retry
                # absorbs a transient provider error; a second failure fails
                # the lease, which the runner and /proxies/resolve already
                # report as an error.
                creds = None
                for attempt in (1, 2):
                    try:
                        creds = await self.client.rotating_credentials(payload_with_extra)
                        break
                    except Exception as e:
                        if attempt == 2:
                            raise RuntimeError(f"proxy exit for geo {_geo_label(geo)} unavailable: {e}") from e
                        log.warning(
                            "credentials with geo=%s failed (%s); retrying once", geo, str(e)[:200]
                        )
                log.info("received credentials with geo=%s", geo)
            else:
                creds = await self.client.rotating_credentials(src_payload)

            if not creds:
                raise RuntimeError("rotating_credentials_empty")

            log.info("received %d credentials", len(creds))

            # Never embed the raw credential string in errors or logs — these
            # messages travel into HTTP 502 details and service logs.
            server, username, password = _split_rotating_credentials(creds[0])

            return ProxyLease(
                config=ProxyConfig(
                    server=server,
                    username=username,
                    password=password
                ),
                source_id=str(proxy.id),
            )

        # static/mobile - geo not supported
        if geo:
            log.warning(
                "geo parameters ignored for proxy_type=%s (not supported)",
                proxy_type_norm
            )

        if proxy.connection_host and proxy.connection_port:
            scheme = "http"
            if "socks" in proxy.url.lower():
                scheme = "socks5"
            elif proxy.url.startswith("https"):
                scheme = "https"
            server = f"{scheme}://{proxy.connection_host}:{proxy.connection_port}"
        else:
            server = _server(proxy.url)

        log.debug("static/mobile proxy: server=%s", server)

        return ProxyLease(
            config=ProxyConfig(server=server, username=proxy.login, password=proxy.password),
            source_id=str(proxy.id),
            change_ip_links=proxy.change_ip_links if proxy_type_norm in ("mobile", "lte") else None,
        )
