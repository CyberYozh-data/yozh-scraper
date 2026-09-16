from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.proxy.cyberyozh.provider import (
    CyberYozhProxyProvider,
    normalize_proxy_raw_type,
    get_category_proxy,
    _server,
)
from src.proxy.cyberyozh.client import OrderedProxy
from src.proxy.base import ProxyFailure


class TestUtilityFunctions:
    def test_normalize_mobile_shared(self):
        """Normalization mobile_shared"""
        assert normalize_proxy_raw_type("mobile_shared") == "mobile"

    def test_normalize_passthrough(self):
        """Other types without changes"""
        assert normalize_proxy_raw_type("mobile") == "mobile"
        assert normalize_proxy_raw_type("res_rotating") == "res_rotating"
        assert normalize_proxy_raw_type("res_static") == "res_static"
        assert normalize_proxy_raw_type("dc_static") == "dc_static"

    def test_category_mobile(self):
        """Category for mobile"""
        assert get_category_proxy("mobile") == "lte"
        assert get_category_proxy("lte") == "lte"

    def test_category_res_rotating(self):
        """Category for residential_rotating"""
        assert get_category_proxy("res_rotating") == "residential_rotating"
        assert get_category_proxy("residential_rotating") == "residential_rotating"
        assert get_category_proxy("rotating") == "residential_rotating"

    def test_category_res_static(self):
        """Category for residential_static"""
        assert get_category_proxy("res_static") == "residential_static"
        assert get_category_proxy("residential") == "residential_static"
        assert get_category_proxy("residential_static") == "residential_static"

    def test_category_dc_static(self):
        """Category for datacenter"""
        assert get_category_proxy("dc_static") == "datacenter"
        assert get_category_proxy("datacenter") == "datacenter"

    def test_category_default(self):
        """Category by default"""
        assert get_category_proxy("unknown") == "residential_static"

    def test_server_parsing_http(self):
        """Parsing HTTP URL"""
        url = "http://proxy.example.com:8080"
        result = _server(url)
        assert result == "http://proxy.example.com:8080"

    def test_server_parsing_socks5(self):
        """Parsing SOCKS5 URL"""
        url = "socks5://proxy.example.com:1080"
        result = _server(url)
        assert result == "socks5://proxy.example.com:1080"

    def test_server_parsing_socks5_http(self):
        """Parsing socks5_http:// -> socks5://"""
        url = "socks5_http://proxy.example.com:1080"
        result = _server(url)
        assert result == "socks5://proxy.example.com:1080"

    def test_server_parsing_invalid_no_host(self):
        """Parsing error: not found host"""
        with pytest.raises(ValueError, match="Invalid proxy URL"):
            _server("http://:8080")

    def test_server_parsing_invalid_no_port(self):
        """Parsing error: not found port"""
        with pytest.raises(ValueError, match="Invalid proxy URL"):
            _server("http://proxy.com")


class TestMaxAttempts:
    @pytest.fixture
    def provider(self):
        client = Mock()
        return CyberYozhProxyProvider(client=client)

    def test_max_attempts_rotating(self, provider):
        """max_attempts uses settings.max_retries for all types"""
        with patch("src.proxy.cyberyozh.provider.settings.max_retries", 5):
            assert provider.max_attempts("res_rotating") == 5
            assert provider.max_attempts("residential_rotating") == 5
            assert provider.max_attempts("rotating") == 5

    def test_max_attempts_mobile(self, provider):
        """max_attempts uses settings.max_retries for all types"""
        with patch("src.proxy.cyberyozh.provider.settings.max_retries", 5):
            assert provider.max_attempts("mobile") == 5
            assert provider.max_attempts("mobile_shared") == 5
            assert provider.max_attempts("lte") == 5

    def test_max_attempts_static(self, provider):
        """max_attempts uses settings.max_retries for all types"""
        with patch("src.proxy.cyberyozh.provider.settings.max_retries", 3):
            assert provider.max_attempts("res_static") == 3
            assert provider.max_attempts("dc_static") == 3
            assert provider.max_attempts("datacenter") == 3

    def test_max_attempts_minimum_one(self, provider):
        """max_attempts never returns less than 1"""
        with patch("src.proxy.cyberyozh.provider.settings.max_retries", 0):
            assert provider.max_attempts("res_static") == 1


class TestAcquire:
    @pytest.mark.asyncio
    async def test_acquire_delegates_to_rotate(self, mocker):
        """acquire delegate to rotate_next"""
        client = AsyncMock()
        provider = CyberYozhProxyProvider(client=client)

        mock_lease = Mock()
        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(return_value=mock_lease),
        )

        result = await provider.acquire(
            proxy_type_raw="mobile",
            proxy_pool_id="pool_1",
        )

        assert result == mock_lease
        provider.rotate_next.assert_called_once_with(
            proxy_type_raw="mobile",
            proxy_pool_id="pool_1",
            exclude_source_ids=set(),
        )


class TestRotateNext:
    @pytest.mark.asyncio
    async def test_rotate_next_basic(self, mocker):
        """Base rotate"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            exclude_source_ids=set(),
        )

        assert lease.config.server == "http://proxy1.com:8080"
        assert lease.config.username == "user1"
        assert lease.config.password == "pass1"
        assert lease.source_id == "1"

    @pytest.mark.asyncio
    async def test_rotate_next_with_exclusions(self, mocker):
        """Rotate with excludes"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
            OrderedProxy(
                id="2",
                url="http://proxy2.com:8080",
                login="user2",
                password="pass2",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy2.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            exclude_source_ids={"1"},
        )

        # Need return proxy2, because proxy1 exclude
        assert lease.source_id == "2"
        assert lease.config.username == "user2"

    @pytest.mark.asyncio
    async def test_rotate_next_with_pool_id_found(self, mocker):
        """Rotate with pool_id (founded)"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
            OrderedProxy(
                id="pool_target",
                url="http://proxy2.com:8080",
                login="user2",
                password="pass2",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy2.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id="pool_target",
            exclude_source_ids=set(),
        )

        # Need return proxy with correct pool_id
        assert lease.source_id == "pool_target"

    @pytest.mark.asyncio
    async def test_rotate_next_with_pool_id_fallback(self, mocker):
        """Rotate with pool_id (not found, fallback)"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id="not_found",
            exclude_source_ids=set(),
        )

        # Need return first available proxy
        assert lease.source_id == "1"

    @pytest.mark.asyncio
    async def test_rotate_next_no_proxies(self, mocker):
        """Error: there is no available proxy"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[])

        provider = CyberYozhProxyProvider(client=client)

        with pytest.raises(RuntimeError, match="no_more_proxies"):
            await provider.rotate_next(
                proxy_type_raw="res_static",
                proxy_pool_id=None,
                exclude_source_ids=set(),
            )

    @pytest.mark.asyncio
    async def test_rotate_next_all_excluded(self, mocker):
        """Error: all proxy excluded"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        with pytest.raises(RuntimeError, match="no_more_proxies"):
            await provider.rotate_next(
                proxy_type_raw="res_static",
                proxy_pool_id=None,
                exclude_source_ids={"1"},
            )

    @pytest.mark.asyncio
    async def test_rotate_next_filters_inactive(self, mocker):
        """Filtering inactive proxy"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="inactive",
                expired=False,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
            OrderedProxy(
                id="2",
                url="http://proxy2.com:8080",
                login="user2",
                password="pass2",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy2.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            exclude_source_ids=set(),
        )

        # Need return only active proxy
        assert lease.source_id == "2"

    @pytest.mark.asyncio
    async def test_rotate_next_filters_expired(self, mocker):
        """Filtering expired proxy"""
        client = AsyncMock()
        client.proxy_history = AsyncMock(return_value=[
            OrderedProxy(
                id="1",
                url="http://proxy1.com:8080",
                login="user1",
                password="pass1",
                status="active",
                expired=True,
                change_ip_links=[],
                connection_host="proxy1.com",
                connection_port=8080,
            ),
            OrderedProxy(
                id="2",
                url="http://proxy2.com:8080",
                login="user2",
                password="pass2",
                status="active",
                expired=False,
                change_ip_links=[],
                connection_host="proxy2.com",
                connection_port=8080,
            ),
        ])

        provider = CyberYozhProxyProvider(client=client)

        lease = await provider.rotate_next(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            exclude_source_ids=set(),
        )

        # Need return only not expired proxy
        assert lease.source_id == "2"


class TestRecover:
    @pytest.mark.asyncio
    async def test_recover_rotating_proxy(self, mocker):
        """Recover for rotating: new credentials"""
        client = AsyncMock()
        provider = CyberYozhProxyProvider(client=client)

        mock_lease = Mock()
        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(return_value=mock_lease),
        )

        failure = ProxyFailure(status_code=403, error="Forbidden")

        new_lease, should_retry = await provider.recover(
            proxy_type_raw="res_rotating",
            proxy_pool_id=None,
            lease=None,
            exclude_source_ids=set(),
            failure=failure,
        )

        assert new_lease == mock_lease
        assert should_retry is True
        provider.rotate_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_recover_mobile_change_ip_success(self, mocker):
        """Recover for mobile: successful change_ip"""
        client = AsyncMock()
        client.call_change_ip_link = AsyncMock()

        provider = CyberYozhProxyProvider(client=client)

        lease = Mock()
        lease.source_id = "mobile_1"
        lease.change_ip_links = ["https://api.com/change-ip/1"]

        failure = ProxyFailure(status_code=403, error="Blocked")

        new_lease, should_retry = await provider.recover(
            proxy_type_raw="mobile",
            proxy_pool_id=None,
            lease=lease,
            exclude_source_ids=set(),
            failure=failure,
        )

        assert new_lease == lease
        assert should_retry is True
        client.call_change_ip_link.assert_called_once_with(
            "https://api.com/change-ip/1"
        )

    @pytest.mark.asyncio
    async def test_recover_mobile_change_ip_failure(self, mocker):
        """Recover for mobile: change_ip failed, fallback on rotate"""
        client = AsyncMock()
        client.call_change_ip_link = AsyncMock(side_effect=Exception("Change IP failed"))

        provider = CyberYozhProxyProvider(client=client)

        lease = Mock()
        lease.source_id = "mobile_1"
        lease.change_ip_links = ["https://api.com/change-ip/1"]

        mock_new_lease = Mock()
        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(return_value=mock_new_lease),
        )

        failure = ProxyFailure(status_code=403, error="Blocked")

        new_lease, should_retry = await provider.recover(
            proxy_type_raw="mobile",
            proxy_pool_id=None,
            lease=lease,
            exclude_source_ids=set(),
            failure=failure,
        )

        assert new_lease == mock_new_lease
        assert should_retry is True

    @pytest.mark.asyncio
    async def test_recover_static_exclude_and_rotate(self, mocker):
        """Recover for static: exclude + rotate"""
        client = AsyncMock()
        provider = CyberYozhProxyProvider(client=client)

        lease = Mock()
        lease.source_id = "static_1"
        lease.change_ip_links = None

        exclude_ids = set()

        mock_new_lease = Mock()
        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(return_value=mock_new_lease),
        )

        failure = ProxyFailure(status_code=403, error="Forbidden")

        new_lease, should_retry = await provider.recover(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            lease=lease,
            exclude_source_ids=exclude_ids,
            failure=failure,
        )

        # Check, source_id added to exclude
        assert "static_1" in exclude_ids
        assert new_lease == mock_new_lease
        assert should_retry is True

    @pytest.mark.asyncio
    async def test_recover_no_more_proxies(self, mocker):
        """Recover: there is no available proxy"""
        client = AsyncMock()
        provider = CyberYozhProxyProvider(client=client)

        lease = Mock()
        lease.source_id = "1"

        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(side_effect=RuntimeError("no_more_proxies")),
        )

        failure = ProxyFailure(status_code=403, error="Forbidden")

        new_lease, should_retry = await provider.recover(
            proxy_type_raw="res_static",
            proxy_pool_id=None,
            lease=lease,
            exclude_source_ids=set(),
            failure=failure,
        )

        assert new_lease == lease
        assert should_retry is False

    @pytest.mark.asyncio
    async def test_recover_adds_to_exclusions(self, mocker):
        """Recover add source_id ot exclusions"""
        client = AsyncMock()
        provider = CyberYozhProxyProvider(client=client)

        lease = Mock()
        lease.source_id = "proxy_123"
        lease.change_ip_links = None

        exclude_ids = {"proxy_1", "proxy_2"}

        mock_new_lease = Mock()
        mocker.patch.object(
            provider,
            "rotate_next",
            AsyncMock(return_value=mock_new_lease),
        )

        failure = ProxyFailure(status_code=500, error="Error")

        await provider.recover(
            proxy_type_raw="dc_static",
            proxy_pool_id=None,
            lease=lease,
            exclude_source_ids=exclude_ids,
            failure=failure,
        )

        # Check, source_id added
        assert "proxy_123" in exclude_ids
        assert len(exclude_ids) == 3


def _rotating_proxy() -> OrderedProxy:
    return OrderedProxy(
        id="1", url="http://proxy.com:8080", login="login", password="password", status="active",
        expired=False, change_ip_links=[], connection_host="proxy.com", connection_port=8080,
    )


class TestToLease:
    @pytest.mark.asyncio
    async def test_to_lease_rotating_basic(self, mocker):
        """Converting in lease for rotatingwithoutgeo"""
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(
            return_value=["user:pass@proxy.com:8080"]
        )

        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="login",
            password="password",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="proxy.com",
            connection_port=8080,
        )

        lease = await provider._to_lease("res_rotating", proxy, geo=None)

        assert lease.config.server == "http://proxy.com:8080"
        assert lease.config.username == "user"
        assert lease.config.password == "pass"
        assert lease.source_id == "1"

    @pytest.mark.asyncio
    async def test_to_lease_rotating_with_geo(self, mocker):
        """Converting in lease for rotatingwithgeo"""
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(
            return_value=["user:pass@proxy.com:8080"]
        )

        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="login",
            password="password",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="proxy.com",
            connection_port=8080,
        )

        geo = {
            "country_code": "US",
            "region": "California",
            "city": "Los Angeles",
        }

        lease = await provider._to_lease("res_rotating", proxy, geo=geo)

        # Check, rotating_credentials was called with geo
        call_args = client.rotating_credentials.call_args
        payload = call_args[0][0]
        assert payload["country_code"] == "US"
        assert payload["region"] == "California"
        assert payload["city"] == "Los Angeles"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_to_lease_rotating_geo_refusal_fails_the_lease(self):
        """Audit 2026-09-03, H-12: a second call without geo used to hand back
        an untargeted exit as if it were the requested one -- `retries=0`,
        `warnings=[]`, fingerprint still GB. A country-scoped caller gets the
        market or an error, nothing in between."""
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(side_effect=[Exception("geo not available"), Exception("still not")])
        provider = CyberYozhProxyProvider(client=client)
        proxy = _rotating_proxy()

        with pytest.raises(RuntimeError, match="proxy exit for geo country_code=US, city=New York unavailable"):
            await provider._to_lease("res_rotating", proxy, geo={"country_code": "US", "city": "New York"})
        # Both calls carried the geo; none dropped it.
        assert [c.kwargs or c.args for c in client.rotating_credentials.call_args_list]
        for call in client.rotating_credentials.call_args_list:
            assert call.args[0]["country_code"] == "US"

    @pytest.mark.asyncio
    async def test_to_lease_rotating_geo_transient_error_is_retried_once(self):
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(side_effect=[Exception("429"), ["user:pass@proxy.com:8080"]])
        provider = CyberYozhProxyProvider(client=client)

        lease = await provider._to_lease("res_rotating", _rotating_proxy(), geo={"country_code": "US"})

        assert lease.config.username == "user"
        assert client.rotating_credentials.call_count == 2
        assert all(c.args[0]["country_code"] == "US" for c in client.rotating_credentials.call_args_list)

    @pytest.mark.asyncio
    async def test_to_lease_rotating_parse_credentials(self, mocker):
        """Parsing credentials for rotating"""
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(
            return_value=["myuser:mypassword@host.com:9000"]
        )

        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://host.com:9000",
            login="login",
            password="password",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="host.com",
            connection_port=9000,
        )

        lease = await provider._to_lease("rotating", proxy, geo=None)

        assert lease.config.server == "http://host.com:9000"
        assert lease.config.username == "myuser"
        assert lease.config.password == "mypassword"

    @pytest.mark.asyncio
    async def test_to_lease_rotating_invalid_format(self, mocker):
        """Error: credentials have not valid format"""
        client = AsyncMock()
        client.rotating_credentials = AsyncMock(
            return_value=["invalid_format"]
        )

        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://host.com:9000",
            login="login",
            password="password",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="host.com",
            connection_port=9000,
        )

        with pytest.raises(RuntimeError, match="Invalid credential format"):
            await provider._to_lease("rotating", proxy, geo=None)

    @pytest.mark.asyncio
    async def test_to_lease_rotating_missing_host(self, mocker):
        """Error: there is no host/port"""
        client = AsyncMock()

        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="invalid_url",
            login="login",
            password="password",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host=None,
            connection_port=None,
        )

        with pytest.raises(RuntimeError, match="missing host/port"):
            await provider._to_lease("rotating", proxy, geo=None)

    @pytest.mark.asyncio
    async def test_to_lease_static_from_connection(self, mocker):
        """Static lease from connection_host/port"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="proxy.com",
            connection_port=8080,
        )

        lease = await provider._to_lease("res_static", proxy, geo=None)

        assert lease.config.server == "http://proxy.com:8080"
        assert lease.config.username == "user"
        assert lease.config.password == "pass"
        assert lease.source_id == "1"

    @pytest.mark.asyncio
    async def test_to_lease_static_from_url(self, mocker):
        """Static lease from url (if there is no connection_host/port)"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host=None,
            connection_port=None,
        )

        lease = await provider._to_lease("res_static", proxy, geo=None)

        assert lease.config.server == "http://proxy.com:8080"
        assert lease.config.username == "user"
        assert lease.config.password == "pass"

    @pytest.mark.asyncio
    async def test_to_lease_static_socks5(self, mocker):
        """Static socks5 lease"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="socks5://proxy.com:1080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="proxy.com",
            connection_port=1080,
        )

        lease = await provider._to_lease("dc_static", proxy, geo=None)

        assert lease.config.server == "socks5://proxy.com:1080"

    @pytest.mark.asyncio
    async def test_to_lease_mobile_with_change_ip(self, mocker):
        """Mobile lease with change_ip_links"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=["https://api.com/change-ip/1"],
            connection_host="proxy.com",
            connection_port=8080,
        )

        lease = await provider._to_lease("mobile", proxy, geo=None)

        assert lease.change_ip_links == ["https://api.com/change-ip/1"]
        assert lease.source_id == "1"

    @pytest.mark.asyncio
    async def test_to_lease_mobile_without_change_ip(self, mocker):
        """Mobile lease without change_ip_links"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
            connection_host="proxy.com",
            connection_port=8080,
        )

        lease = await provider._to_lease("mobile", proxy, geo=None)

        assert lease.change_ip_links == []


class TestOkMethod:
    def test_ok_method_active(self):
        """_ok: active and not expired"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
        )

        assert provider._ok(proxy) is True

    def test_ok_method_inactive(self):
        """_ok: inactive -> False"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="inactive",
            expired=False,
            change_ip_links=[],
        )

        assert provider._ok(proxy) is False

    def test_ok_method_expired(self):
        """_ok: expired -> False"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="http://proxy.com:8080",
            login="user",
            password="pass",
            status="active",
            expired=True,
            change_ip_links=[],
        )

        assert provider._ok(proxy) is False

    def test_ok_method_no_url(self):
        """_ok: no URL -> False"""
        client = Mock()
        provider = CyberYozhProxyProvider(client=client)

        proxy = OrderedProxy(
            id="1",
            url="",
            login="user",
            password="pass",
            status="active",
            expired=False,
            change_ip_links=[],
        )

        assert provider._ok(proxy) is False
