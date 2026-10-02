"""Restart-frozen browser profiles + X-Super-Properties builder.

Source of truth: https://docs.discord.food/ (client properties page).

Rules enforced here:
1. Everything in a profile is frozen at ``Client()`` construction.
   Only protocol-mandated ephemeral IDs (launch id, heartbeat session id,
   gateway session/seq/resume url) change mid-run -- and they live in
   ``EphemeralIds``, not in the profile.
2. ``browser_user_agent`` property MUST equal the ``User-Agent`` header,
   except mobile (android/ios) where the property is ``""`` and the real
   string travels in the header only.
3. Fingerprint extras (screen resolution, cpu cores, device memory) are
   NEVER serialized into X-Super-Properties. They live in
   ``BrowserProfile.fingerprint`` for a future separate sender.
4. ``impersonate`` is always a browser preset -- never bare curl.
   Console/VR targets have no preset in curl_cffi, so they map to the
   closest Chromium stack and carry an ``approximation`` note.
5. Versions are dynamic: ``build_profile`` takes a ``ResolvedVersions``
   (see versions.py, fetched at boot). Nothing version-like is
   hardcoded here except unfetchable spoof claims (Windows build
   strings, iOS CFNetwork/Darwin triplet, console app UAs) and the
   offline fallbacks in versions.py.
6. Client-Hint / Fetch-Metadata / Origin / Referer headers are only sent by
   presets that plausibly send them. Claiming WebKit TLS (safari_ios)
   while also advertising ``Sec-CH-UA: Chromium`` is a self-inflicted
   contradiction, so iOS sends no client hints at all.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from enum import Enum

from .versions import (
    FALLBACK_BUILD_NUMBER,
    ResolvedVersions,
    system_cpu_count,
    system_locale,
    system_timezone,
)

# Unfetchable spoof claims (no public source publishes these; observed values).
IOS_CFNETWORK = "1496.0.7"
IOS_DARWIN = "23.5.0"
IOS_OS_VERSION = "17.5"
XBOX_UA = "Discord-Xbox/1.0"
PLAYSTATION_UA = "Discord-Playstation/1.0"
VR_PACKAGE_VERSION = "1.0.0"
VR_OS_VERSION = "10.0.22631"
EMBEDDED_BUILD_NUMBER = 4440  # observed on console embedded clients

PROFILE_NAMES = (
    "web",
    "desktop",
    "android",
    "ios",
    "xbox",
    "playstation",
    "vr",
)


class ClientKind(str, Enum):
    """Which client surface a profile claims to be.

    Replaces scattered ``profile.browser in (...)`` string checks: renaming a
    browser string used to silently break header generation.
    """

    WEB = "web"
    DESKTOP = "desktop"
    ANDROID = "android"
    IOS = "ios"
    XBOX = "xbox"
    PLAYSTATION = "playstation"
    VR = "vr"

    @property
    def is_browser_page(self) -> bool:
        """Does this surface actually load a web page?

        Only ``web`` navigates to a document, so only it carries Origin,
        Referer and Sec-Fetch-*. Every other surface is a native client
        talking to the API over its own HTTP stack.
        """
        return self is ClientKind.WEB

    @property
    def sends_client_hints(self) -> bool:
        """Sec-CH-UA and friends.

        Chromium-based surfaces send these; WebKit (iOS) never does. Console
        clients keep them on purpose: their TLS fingerprint is borrowed from
        Chromium, so dropping the matching CH headers would be the *bigger*
        inconsistency.
        """
        return self is not ClientKind.IOS

# impersonate preset per profile. Consoles/VR have no native preset in
# curl_cffi (browser presets only), so they map to the closest stack:
# Xbox OS browser = Edge/Chromium -> chrome; Quest = Chromium Android
# (this vr preset is the Windows/PCVR variant -> chrome); PS5's real
# stack is a Sony WebKit fork, chrome is the least-bad approximation.
IMPERSONATE_MAP = {
    "web": "chrome",
    "desktop": "chrome",
    "android": "chrome_android",
    "ios": "safari_ios",
    "xbox": "chrome",  # approximation: no console preset in curl_cffi
    "playstation": "chrome",  # approximation: no console preset in curl_cffi
    "vr": "chrome",  # approximation: PCVR variant; Quest standalone ~ chrome_android
}


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """Everything frozen at Client() construction."""

    kind: ClientKind
    user_agent: str
    os: str
    browser: str
    browser_version: str
    os_version: str
    release_channel: str = "stable"
    client_build_number: int = FALLBACK_BUILD_NUMBER
    design_id: int = 0
    locale: str = "en-US"
    timezone: str = "America/New_York"
    impersonate: str = "chrome"
    # Real Chromium major behind the UA (desktop: from bundled Chrome;
    # else the fetched stable major). Drives Sec-CH-UA. Never serialized.
    chromium_major: str = ""
    # NEVER serialized into X-Super-Properties (see module docstring).
    fingerprint: dict = field(default_factory=dict, compare=False)


def build_profile(
    preset: str,
    versions: ResolvedVersions | None = None,
    *,
    locale: str | None = None,
    timezone: str | None = None,
    screen_resolution: str = "1920x1080",
    hardware_concurrency: int | None = None,
    device_memory: int = 8,
) -> BrowserProfile:
    """Pure factory: no network, no clock.

    ``versions`` comes from ``versions.resolve_all`` at boot; ``None``
    means offline boot (fallbacks). locale/timezone/cpu default from OS.
    """
    if preset not in PROFILE_NAMES:
        raise ValueError(f"unknown preset {preset!r}, expected one of {PROFILE_NAMES}")
    v = versions or ResolvedVersions()
    locale = locale or system_locale()
    timezone = timezone or system_timezone()
    hardware_concurrency = (
        hardware_concurrency if hardware_concurrency is not None else system_cpu_count()
    )
    impersonate = IMPERSONATE_MAP[preset]
    fp = {
        "screen_resolution": screen_resolution,
        "hardware_concurrency": hardware_concurrency,
        "device_memory": device_memory,
    }

    if preset == "web":
        return BrowserProfile(
            kind=ClientKind.WEB,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{v.chrome_major}.0.0.0 Safari/537.36"
            ),
            os="Windows",
            browser="Chrome",
            browser_version=v.chrome_major,
            os_version="10.0.26100",
            client_build_number=v.build_number,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.chrome_major,
            fingerprint=fp,
        )
    if preset == "desktop":
        return BrowserProfile(
            kind=ClientKind.DESKTOP,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"discord/{v.desktop_app} Chrome/{v.desktop_chrome} "
                f"Electron/{v.desktop_electron} Safari/537.36"
            ),
            os="Windows",
            browser="Discord Client",
            browser_version=v.desktop_electron,
            os_version="10.0.20348",
            client_build_number=v.build_number,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.desktop_chrome.split(".")[0],
            fingerprint=fp,
        )
    if preset == "android":
        return BrowserProfile(
            kind=ClientKind.ANDROID,
            user_agent=f"Discord-Android/{v.android_header};RNA",
            os="Android",
            browser="Discord Android",
            browser_version="",  # discord.food: mobile property is ""
            os_version="14",
            client_build_number=v.build_number,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.chrome_major,
            fingerprint={**fp, "screen_resolution": "1080x2400", "device_memory": 8},
        )
    if preset == "ios":
        return BrowserProfile(
            kind=ClientKind.IOS,
            user_agent=(
                f"Discord/{v.ios_app} CFNetwork/{IOS_CFNETWORK} Darwin/{IOS_DARWIN}"
            ),
            os="iOS",
            browser="Discord iOS",
            browser_version="",  # discord.food: mobile property is ""
            os_version=IOS_OS_VERSION,
            client_build_number=v.build_number,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.chrome_major,
            fingerprint={**fp, "screen_resolution": "1179x2556", "device_memory": 6},
        )
    if preset == "xbox":
        return BrowserProfile(
            kind=ClientKind.XBOX,
            user_agent=XBOX_UA,
            os="Xbox",
            browser="Discord Embedded",
            browser_version="1.0",
            os_version="10.0.22621",
            release_channel="unknown",
            client_build_number=EMBEDDED_BUILD_NUMBER,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.chrome_major,
            fingerprint={**fp, "device_memory": 16},
        )
    if preset == "playstation":
        return BrowserProfile(
            kind=ClientKind.PLAYSTATION,
            user_agent=PLAYSTATION_UA,
            os="Playstation",
            browser="Discord Embedded",
            browser_version="1.0",
            os_version="10.0",
            release_channel="unknown",
            client_build_number=EMBEDDED_BUILD_NUMBER,
            locale=locale,
            timezone=timezone,
            impersonate=impersonate,
            chromium_major=v.chrome_major,
            fingerprint={**fp, "device_memory": 16},
        )
    # vr (PCVR / Windows variant)
    return BrowserProfile(
        kind=ClientKind.VR,
        user_agent=f"DiscordVR/{VR_PACKAGE_VERSION} (Windows {VR_OS_VERSION})",
        os="Windows",
        browser="Discord VR",
        browser_version=VR_PACKAGE_VERSION,
        os_version=VR_OS_VERSION,
        client_build_number=v.build_number,
        locale=locale,
        timezone=timezone,
        impersonate=impersonate,
        chromium_major=v.chrome_major,
        fingerprint=fp,
    )


@dataclass(slots=True)
class EphemeralIds:
    """Protocol-mandated IDs allowed to change mid-run. Nothing else may."""

    client_launch_id: str  # uuid, set once per process boot
    client_heartbeat_session_id: str = ""  # rotated every ~30min with op 41


def super_properties_dict(
    profile: BrowserProfile, eph: EphemeralIds, *, for_gateway: bool = False
) -> dict:
    """Build the properties payload.

    ``for_gateway=False`` -> stable subset for the X-Super-Properties header.
    ``for_gateway=True``  -> full Identify properties (stable + ephemeral +
    gateway-only fields).
    """
    # Mobile: property UA is "" per discord.food; header carries the string.
    prop_ua = (
        ""
        if profile.browser in ("Discord Android", "Discord iOS")
        else profile.user_agent
    )
    props: dict = {
        "os": profile.os,
        "browser": profile.browser,
        "browser_user_agent": prop_ua,
        "browser_version": profile.browser_version,
        "client_build_number": profile.client_build_number,
        "release_channel": profile.release_channel,
        "system_locale": profile.locale,
        "os_version": profile.os_version,
        "client_launch_id": eph.client_launch_id,
        "client_app_state": "focused",
        "client_event_source": None,
        "design_id": profile.design_id,
    }
    if eph.client_heartbeat_session_id:
        props["client_heartbeat_session_id"] = eph.client_heartbeat_session_id
    if for_gateway:
        props["is_fast_connect"] = False
    return props


def encode_super_properties(props: dict) -> str:
    raw = json.dumps(props, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def sec_ch_ua(profile: BrowserProfile) -> str:
    """Dynamic Sec-CH-UA derived from the frozen profile (no static brands).

    GREASE brand names and their ordering are Chromium-version dependent and
    rotate on a schedule, so treat the exact strings as an approximation --
    the structural rules (iOS sends none, mobile orders brands differently)
    are what matter and are enforced here.
    """
    chromium = profile.chromium_major or "124"
    if profile.kind is ClientKind.DESKTOP:
        return (
            f'"Not-A.Brand";v="99", "Chromium";v="{chromium}", '
            f'"Electron";v="{profile.browser_version}"'
        )
    if profile.kind is ClientKind.ANDROID:
        # Chrome on Android orders the GREASE brand first, unlike desktop.
        return (
            f'"Not-A.Brand";v="99", "Chromium";v="{chromium}", '
            f'"Google Chrome";v="{chromium}"'
        )
    return (
        f'"Chromium";v="{chromium}", "Google Chrome";v="{chromium}", '
        '"Not-A.Brand";v="99"'
    )


def sec_ch_ua_mobile(profile: BrowserProfile) -> str:
    return "?1" if profile.kind is ClientKind.ANDROID else "?0"


def accept_language(locale: str) -> str:
    """``en-US`` -> ``en-US,en;q=0.9``.

    A bare tag with no q-value list is not something a browser emits; the
    base language is always offered as a lower-priority fallback.
    """
    base = locale.split("-", 1)[0].split("_", 1)[0]
    if not base or base == locale:
        return locale
    return f"{locale},{base};q=0.9"


def sec_fetch_headers(profile: BrowserProfile) -> dict:
    """Fetch Metadata. Only a real page load sends these.

    A native client (desktop, mobile, console) hits the API from its own HTTP
    stack and sends no ``Sec-Fetch-*`` at all, so adding them there would
    advertise a browser that the User-Agent just denied being one.
    """
    if not profile.kind.is_browser_page:
        return {}
    # XHR/fetch from the SPA, same-origin.
    return {
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }


def base_headers(
    profile: BrowserProfile, eph: EphemeralIds, *, authorized: bool = False
) -> dict:
    """Headers shared by every request from the single HTTP session."""
    headers = {
        "User-Agent": profile.user_agent,
        "X-Super-Properties": encode_super_properties(
            super_properties_dict(profile, eph)
        ),
        "X-Discord-Locale": profile.locale,
        "X-Discord-Timezone": profile.timezone,
        "Accept-Language": accept_language(profile.locale),
    }
    if profile.kind.is_browser_page:
        # Only the web preset is a page load. The Referer mirrors the SPA
        # route rather than the bare origin, which is what a same-origin
        # fetch actually reports.
        headers["Origin"] = "https://discord.com"
        headers["Referer"] = "https://discord.com/channels/@me"
        headers.update(sec_fetch_headers(profile))
    if profile.kind.sends_client_hints:
        headers["Sec-CH-UA"] = sec_ch_ua(profile)
        headers["Sec-CH-UA-Mobile"] = sec_ch_ua_mobile(profile)
        headers["Sec-CH-UA-Platform"] = f'"{profile.os}"'
    if not authorized:
        headers["X-Fingerprint"] = ""  # filled from experiment assignments
    return headers
