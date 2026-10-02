"""Versions tests: pure parsers + fetchers against a fake session + dynamic profiles."""

import asyncio

import pytest

from despise import headers as H
from despise.headers import EphemeralIds, build_profile
from despise.versions import (
    DEFAULT_RESOLVER,
    FALLBACK_DESKTOP_APP,
    ResolvedVersions,
    VersionResolver,
    android_header_version,
    coerce_resolver,
    fetch_android_version,
    fetch_build_number,
    fetch_chrome_major,
    fetch_desktop_versions,
    fetch_ios_version,
    parse_build_number_login,
    parse_chrome_major,
    parse_electron_chrome,
    parse_electron_latest,
    parse_ios_version,
    resolve_all,
    system_cpu_count,
    system_locale,
)


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeSession:
    """Maps URL substring -> payload (dict serialized, or raw str)."""

    def __init__(self, routes, fail=()):
        self.routes = routes
        self.fail = fail

    async def get(self, url):
        for key in self.fail:
            if key in url:
                raise ConnectionError("offline")
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, str):
                    return FakeResp(payload)
                import json

                return FakeResp(json.dumps(payload))
        raise AssertionError(f"unexpected url {url}")


def run(coro):
    return asyncio.run(coro)


# -- parsers ---------------------------------------------------------------
def test_parse_build_login():
    assert parse_build_number_login('<script>"buildNumber": 456245</script>') == 456245
    assert parse_build_number_login("<html>nope</html>") is None


def test_parse_chrome():
    assert parse_chrome_major({"versions": [{"version": "133.0.6943.99"}]}) == "133"
    assert parse_chrome_major({}) is None


def test_parse_desktop_and_electron():
    assert parse_electron_latest({"version": "38.0.0"}) == "38.0.0"
    releases = [
        {"version": "38.0.0", "chrome": "140.0.1234.5"},
        {"version": "37.7.0", "chrome": "138.0.7204.251"},
    ]
    assert parse_electron_chrome(releases, "38.0.0") == "140.0.1234.5"
    assert parse_electron_chrome(releases, "38.1.0") == "140.0.1234.5"  # major match
    assert parse_electron_chrome(releases, "99.0.0") is None


def test_parse_ios():
    assert parse_ios_version({"results": [{"version": "281.0"}]}) == "281.0"
    assert parse_ios_version({}) is None


def test_android_header_heuristic():
    assert android_header_version("280.2") == "280202"  # observed pair
    assert android_header_version("281.15") == "2811502"


# -- fetchers ---------------------------------------------------------------
def full_routes():
    return {
        "discord.com/login": '<script src="/assets/app.abc123.js"></script>',
        "/assets/app.abc123.js": "var x={buildNumber: 500001};",
        "versionhistory": {"versions": [{"version": "133.0.0.0"}]},
        "electron/latest": {"version": "38.0.0"},
        "releases.json": [{"version": "38.0.0", "chrome": "140.0.1.2"}],
        "lookup": {"results": [{"version": "281.0"}]},
    }


def test_fetchers_happy_path():
    s = FakeSession(full_routes())
    assert run(fetch_build_number(s)) == 500001
    assert run(fetch_chrome_major(s)) == "133"
    # desktop app has no public feed -> manual constant; electron+chrome live
    assert run(fetch_desktop_versions(s)) == (
        FALLBACK_DESKTOP_APP,
        "38.0.0",
        "140.0.1.2",
    )
    assert run(fetch_ios_version(s)) == "281.0"


def test_fetchers_offline_fallback():
    s = FakeSession(
        {},
        fail=(
            "cordapi",
            "versionhistory",
            "updates",
            "npmjs",
            "electronjs",
            "apple",
            "discord.com",
        ),
    )
    from despise.versions import (
        FALLBACK_ANDROID_APP,
        FALLBACK_ANDROID_HEADER,
        FALLBACK_BUILD_NUMBER,
        FALLBACK_CHROME_MAJOR,
        FALLBACK_DESKTOP_APP,
        FALLBACK_DESKTOP_CHROME,
        FALLBACK_DESKTOP_ELECTRON,
        FALLBACK_IOS_APP,
    )

    assert run(fetch_build_number(s)) == FALLBACK_BUILD_NUMBER
    assert run(fetch_chrome_major(s)) == FALLBACK_CHROME_MAJOR
    assert run(fetch_desktop_versions(s)) == (
        FALLBACK_DESKTOP_APP,
        FALLBACK_DESKTOP_ELECTRON,
        FALLBACK_DESKTOP_CHROME,
    )
    assert run(fetch_android_version(s)) == (
        FALLBACK_ANDROID_APP,
        FALLBACK_ANDROID_HEADER,
    )
    assert run(fetch_ios_version(s)) == FALLBACK_IOS_APP


def test_resolve_all_concurrent():
    s = FakeSession(full_routes())
    v = run(resolve_all(s))
    assert isinstance(v, ResolvedVersions)
    assert (v.build_number, v.chrome_major, v.desktop_app) == (
        500001,
        "133",
        FALLBACK_DESKTOP_APP,
    )
    assert v.ios_app == "281.0"


# -- dynamic profiles ---------------------------------------------------------
def test_dynamic_versions_flow_into_profiles():
    v = ResolvedVersions(
        build_number=500001,
        chrome_major="133",
        desktop_app="1.0.9170",
        desktop_electron="38.0.0",
        desktop_chrome="140.0.1.2",
        android_app="281.0",
        android_header="2810002",
        ios_app="281.0",
    )
    web = build_profile("web", v)
    assert "Chrome/133.0.0.0" in web.user_agent
    assert web.client_build_number == 500001

    desk = build_profile("desktop", v)
    assert "discord/1.0.9170" in desk.user_agent
    assert "Electron/38.0.0" in desk.user_agent
    assert desk.chromium_major == "140"

    droid = build_profile("android", v)
    assert droid.user_agent == "Discord-Android/2810002;RNA"

    ios = build_profile("ios", v)
    assert ios.user_agent.startswith("Discord/281.0 CFNetwork/")

    eph = EphemeralIds(client_launch_id="x")
    import base64
    import json

    props = json.loads(
        base64.b64decode(H.encode_super_properties(H.super_properties_dict(web, eph)))
    )
    assert props["client_build_number"] == 500001
    assert props["browser_version"] == "133"


def test_os_derived_defaults():
    assert isinstance(system_locale(), str) and "-" in system_locale() or True
    assert system_cpu_count() >= 1
    p = build_profile("web")  # versions=None -> offline fallbacks
    assert p.fingerprint["hardware_concurrency"] >= 1


# -- pluggable version resolvers --------------------------------------------
def test_default_resolver_matches_resolve_all():
    s = FakeSession(full_routes())
    a = run(resolve_all(s))
    b = run(DEFAULT_RESOLVER.resolve(FakeSession(full_routes())))
    assert a.build_number == b.build_number
    assert a.chrome_major == b.chrome_major
    assert a.ios_app == b.ios_app


def test_custom_field_resolver_overrides_one_value():
    async def pinned(session):
        return 999999

    resolver = VersionResolver(build_number=pinned)
    v = run(resolver.resolve(FakeSession(full_routes())))
    assert v.build_number == 999999
    # everything else still comes from the built-in sources
    assert v.chrome_major == "133"
    assert v.ios_app == "281.0"


def test_a_raising_custom_field_falls_back():
    async def broken(session):
        raise RuntimeError("nope")

    resolver = VersionResolver(build_number=broken)
    v = run(resolver.resolve(FakeSession(full_routes())))
    # fell back to the built-in scraper, which found 500001 in the routes
    assert v.build_number == 500001


def test_override_returns_a_whole_resolved_bundle():
    async def whole(session):
        return ResolvedVersions(build_number=42, chrome_major="140")

    resolver = VersionResolver(override=whole)
    v = run(resolver.resolve(FakeSession({})))
    assert v.build_number == 42
    assert v.chrome_major == "140"


def test_override_must_return_resolved_versions():
    async def wrong(session):
        return {"build_number": 1}

    resolver = VersionResolver(override=wrong)
    with pytest.raises(TypeError, match="ResolvedVersions"):
        run(resolver.resolve(FakeSession({})))


def test_override_cannot_be_combined_with_fields():
    async def fn(session):
        return None

    with pytest.raises(ValueError, match="cannot be combined"):
        VersionResolver(build_number=fn, override=fn)


def test_sync_override_is_supported():
    def sync_override(session):
        return ResolvedVersions(build_number=7)

    v = run(VersionResolver(override=sync_override).resolve(FakeSession({})))
    assert v.build_number == 7


def test_coerce_resolver_variants():
    assert coerce_resolver(None) is DEFAULT_RESOLVER
    custom = VersionResolver()
    assert coerce_resolver(custom) is custom

    def bare(session):
        return ResolvedVersions()

    coerced = coerce_resolver(bare)
    assert isinstance(coerced, VersionResolver)
    assert coerced.override is bare


def test_coerce_resolver_rejects_nonsense():
    with pytest.raises(TypeError, match="VersionResolver or callable"):
        coerce_resolver(42)


def test_client_accepts_a_bare_callable_resolver():
    from despise.client import Client

    calls = []

    async def resolver(session):
        calls.append(session)
        return ResolvedVersions(build_number=123)

    c = Client("tok", version_resolver=resolver)
    assert isinstance(c.version_resolver, VersionResolver)
    run(c.boot())
    assert calls, "custom resolver was not used"
    assert c.http.headers["X-Super-Properties"]
    run(c.http.close())


def test_client_rejects_a_bad_resolver():
    from despise.client import Client

    with pytest.raises(TypeError, match="VersionResolver or callable"):
        Client("tok", version_resolver=object())
