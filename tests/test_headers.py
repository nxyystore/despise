"""Golden tests: X-Super-Properties shape + header/UA consistency."""

import base64
import json

import pytest

from despise.headers import (
    PROFILE_NAMES,
    ClientKind,
    EphemeralIds,
    base_headers,
    build_profile,
    encode_super_properties,
    super_properties_dict,
)

EPH = EphemeralIds(client_launch_id="00000000-0000-0000-0000-000000000000")

BANNED_KEYS = {"screen_resolution", "hardware_concurrency", "device_memory"}


@pytest.mark.parametrize("preset", PROFILE_NAMES)
def test_super_properties_roundtrip(preset):
    profile = build_profile(preset)
    props = super_properties_dict(profile, EPH)
    decoded = json.loads(base64.b64decode(encode_super_properties(props)))
    assert decoded == props
    assert BANNED_KEYS.isdisjoint(decoded), f"fingerprint leaked: {decoded}"
    for key in (
        "os",
        "browser",
        "browser_user_agent",
        "browser_version",
        "client_build_number",
        "release_channel",
        "system_locale",
    ):
        assert key in decoded, (preset, key)


@pytest.mark.parametrize("preset", PROFILE_NAMES)
def test_ua_header_property_consistency(preset):
    profile = build_profile(preset)
    headers = base_headers(profile, EPH)
    props = super_properties_dict(profile, EPH)
    assert headers["User-Agent"] == profile.user_agent
    if profile.browser in ("Discord Android", "Discord iOS"):
        assert props["browser_user_agent"] == ""
        assert props["browser_version"] == ""
    else:
        assert props["browser_user_agent"] == profile.user_agent


@pytest.mark.parametrize("preset", PROFILE_NAMES)
def test_mobile_flag_matches_the_surface(preset):
    """iOS is WebKit: it sends no client hints at all, so there is no ?1."""
    profile = build_profile(preset)
    headers = base_headers(profile, EPH)
    if profile.kind.sends_client_hints:
        expected = "?1" if profile.kind is ClientKind.ANDROID else "?0"
        assert headers["Sec-CH-UA-Mobile"] == expected
    else:
        assert "Sec-CH-UA-Mobile" not in headers
        assert "Sec-CH-UA" not in headers
        assert "Sec-CH-UA-Platform" not in headers


def test_ios_sends_no_chromium_client_hints():
    """Regression: iOS claimed WebKit TLS while advertising Chromium."""
    headers = base_headers(build_profile("ios"), EPH)
    for key in ("Sec-CH-UA", "Sec-CH-UA-Mobile", "Sec-CH-UA-Platform"):
        assert key not in headers
    assert headers["User-Agent"].startswith("Discord/")
    assert headers["User-Agent"].count("Chromium") == 0


def test_only_web_sends_fetch_metadata():
    """A native client does not claim to be a page load."""
    web = base_headers(build_profile("web"), EPH)
    assert web["Sec-Fetch-Dest"] == "empty"
    assert web["Sec-Fetch-Mode"] == "cors"
    assert web["Sec-Fetch-Site"] == "same-origin"
    assert web["Referer"].startswith("https://discord.com/")
    assert web["Origin"] == "https://discord.com"

    for preset in ("desktop", "android", "ios", "xbox", "playstation", "vr"):
        headers = base_headers(build_profile(preset), EPH)
        assert "Sec-Fetch-Dest" not in headers, preset
        assert "Referer" not in headers, preset
        assert "Origin" not in headers, preset


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("en-US", "en-US,en;q=0.9"),
        ("fr-FR", "fr-FR,fr;q=0.9"),
        ("zh-TW", "zh-TW,zh;q=0.9"),
        ("pt", "pt"),
    ],
)
def test_accept_language_has_q_values(locale, expected):
    assert base_headers(build_profile("web", locale=locale), EPH)[
        "Accept-Language"
    ] == expected


def test_android_orders_grease_brand_first():
    headers = base_headers(build_profile("android"), EPH)
    assert headers["Sec-CH-UA"].startswith('"Not-A.Brand"')
    assert '"Google Chrome"' in headers["Sec-CH-UA"]


def test_mobile_header_shapes():
    android = build_profile("android")
    assert android.user_agent == "Discord-Android/280202;RNA"
    ios = build_profile("ios")
    assert "CFNetwork" in ios.user_agent and "Darwin" in ios.user_agent


def test_console_uas():
    assert build_profile("xbox").user_agent == "Discord-Xbox/1.0"
    assert build_profile("playstation").user_agent == "Discord-Playstation/1.0"
    vr = build_profile("vr")
    assert vr.user_agent.startswith("DiscordVR/") and vr.browser == "Discord VR"


def test_impersonate_never_bare():
    for preset in PROFILE_NAMES:
        assert build_profile(preset).impersonate  # must always be set


def test_gateway_props_superset():
    profile = build_profile("web")
    header_props = super_properties_dict(profile, EPH)
    gw_props = super_properties_dict(profile, EPH, for_gateway=True)
    assert set(gw_props) >= set(header_props)
    assert "is_fast_connect" in gw_props
