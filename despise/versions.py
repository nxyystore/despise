"""Dynamic version resolution. Called ONLY at restart (Client.boot).

Every fetcher is tolerant: any failure -> offline fallback constant.
Fallbacks exist solely so an offline boot still produces a coherent
profile; they are never refreshed mid-run.

Sources (all verified live 2026-09-24):
- client build number: discord.com/login HTML scrape -> fallback.
  (cordapi.dolfi.es is dolfies' user API; every /api/* path 404s.)
- Chrome major: Google Version History API (no key needed for listing).
- Desktop: Electron+Chrome resolve live (npm latest + electron releases
  map); the discord/1.0.x host has NO public feed and stays one manual
  constant (see FALLBACK_DESKTOP_APP).
- Android app version: no public source (Play has no stable endpoint;
  no scraping games) -> fallback constant only.
- iOS app version: iTunes lookup API (Discord app id 985746746).
- Console/VR app UAs (Discord-Xbox/1.0 etc.): NOT fetchable anywhere
  public; kept as observed constants on the profile, not here.
"""

from __future__ import annotations

import asyncio
import json
import locale
import logging
import os
import re
from collections.abc import Callable

log = logging.getLogger("despise.versions")

# Offline fallbacks. Used only when every fetch fails.
FALLBACK_BUILD_NUMBER = 456245
FALLBACK_CHROME_MAJOR = "131"
# Desktop host version: Discord publishes NO machine-readable feed for it
# (/api/updates returns the Squirrel installer version, e.g. 0.0.311, not
# the discord/1.0.x host in the UA; the download page carries none).
# Single constant, refresh manually on major desktop releases.
FALLBACK_DESKTOP_APP = "1.0.9160"
FALLBACK_DESKTOP_ELECTRON = "37.7.0"
FALLBACK_DESKTOP_CHROME = "134.0.6998.205"
FALLBACK_ANDROID_APP = "280.2"
FALLBACK_ANDROID_HEADER = "280202"
FALLBACK_IOS_APP = "346.0"  # observed 2026-09-24 via iTunes lookup

DISCORD_IOS_APP_ID = "985746746"


async def _get_json(session, url: str):
    r = await session.get(url)
    text = r.text if isinstance(r.text, str) else r.text.decode()
    return json.loads(text)


async def _get_text(session, url: str) -> str:
    r = await session.get(url)
    return r.text if isinstance(r.text, str) else r.text.decode()


# -- parsers (pure, unit-tested) ------------------------------------------
def parse_build_number_login(html: str) -> int | None:
    m = re.search(r'"?buildNumber"?\s*:\s*"?(\d+)"?', html)
    return int(m.group(1)) if m else None


def parse_chrome_major(data: object) -> str | None:
    # versionhistory API: {"versions": [{"version": "131.0.6778.87", ...}]}
    if isinstance(data, dict):
        versions = data.get("versions")
        if versions and isinstance(versions[0], dict):
            full = versions[0].get("version", "")
            if "." in full:
                return full.split(".")[0]
    return None


def parse_electron_latest(data: object) -> str | None:
    if isinstance(data, dict) and isinstance(data.get("version"), str):
        return data["version"]
    return None


def parse_electron_chrome(releases: object, electron: str) -> str | None:
    # releases.electronjs.org/releases.json: [{version, chrome, ...}]
    if not isinstance(releases, list):
        return None
    major = electron.split(".")[0]
    for entry in releases:
        if isinstance(entry, dict) and entry.get("version") == electron:
            return entry.get("chrome") or None
    for entry in releases:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("version"), str)
            and entry["version"].split(".")[0] == major
        ):
            return entry.get("chrome") or None
    return None


def parse_ios_version(data: object) -> str | None:
    if isinstance(data, dict):
        results = data.get("results")
        if results and isinstance(results[0], dict):
            v = results[0].get("version")
            return v if isinstance(v, str) else None
    return None


def android_header_version(display: str) -> str:
    """Heuristic: '280.2' -> '280202' (matches observed pair).

    Major + minor (unpadded) + trailing '02' seen on real headers. If
    the shape is unexpected, digits are concatenated so the header
    stays numeric.
    """
    parts = display.split(".")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return f"{parts[0]}{int(parts[1])}02"
    return "".join(re.findall(r"\d+", display)) or FALLBACK_ANDROID_HEADER


# -- fetchers (network, tolerant) ------------------------------------------
async def fetch_build_number(session) -> int:
    # cordapi.dolfi.es was probed 2026-09-24: it is dolfies' user API
    # with no public build endpoint (every /api/* path 404s), so the
    # live source is the login page's JS bundle (the HTML shell carries
    # no buildNumber itself; it lives in a hashed /assets/*.js chunk).
    try:
        html = await _get_text(session, "https://discord.com/login")
        srcs = re.findall(r'<script[^>]+src="(/assets/[^"]+\.js)"', html)
        if srcs:
            chunks = await asyncio.gather(
                *(_get_text(session, f"https://discord.com{s}") for s in srcs),
                return_exceptions=True,
            )
            for js in chunks:
                if (
                    isinstance(js, str)
                    and (v := parse_build_number_login(js)) is not None
                ):
                    return v
    except Exception:
        pass
    return FALLBACK_BUILD_NUMBER


async def fetch_chrome_major(session) -> str:
    try:
        data = await _get_json(
            session,
            "https://versionhistory.googleapis.com/v1/chrome/platforms/win/channels/stable/versions",
        )
        if (v := parse_chrome_major(data)) is not None:
            return v
    except Exception:
        pass
    return FALLBACK_CHROME_MAJOR


async def fetch_desktop_versions(session) -> tuple[str, str, str]:
    """Returns (app_version, electron_version, chrome_build).

    app_version has NO public feed (/api/updates returns the Squirrel
    installer version like 0.0.311, NOT the discord/1.0.x host in the
    UA), so it stays a manual constant. Electron + Chrome resolve live.
    """
    app = FALLBACK_DESKTOP_APP
    electron = FALLBACK_DESKTOP_ELECTRON
    try:
        data = await _get_json(session, "https://registry.npmjs.org/electron/latest")
        if (v := parse_electron_latest(data)) is not None:
            electron = v
    except Exception:
        pass
    chrome = FALLBACK_DESKTOP_CHROME
    try:
        releases = await _get_json(
            session, "https://releases.electronjs.org/releases.json"
        )
        if (v := parse_electron_chrome(releases, electron)) is not None:
            chrome = v
    except Exception:
        pass
    return app, electron, chrome


async def fetch_android_version(session) -> tuple[str, str]:
    """Returns (display_version, header_version).

    No public source (Play has no stable endpoint; no scraping games),
    so this is the fallback pair. Single place to update when observed.
    """
    return FALLBACK_ANDROID_APP, FALLBACK_ANDROID_HEADER


async def fetch_ios_version(session) -> str:
    try:
        data = await _get_json(
            session,
            f"https://itunes.apple.com/lookup?id={DISCORD_IOS_APP_ID}",
        )
        if (v := parse_ios_version(data)) is not None:
            return v
    except Exception:
        pass
    return FALLBACK_IOS_APP


class VersionResolver:
    """Pluggable source of version numbers.

    Each field is an optional async callable taking the shared session and
    returning that value. Anything omitted (or raising) falls back to its
    offline constant, so a custom resolver can pin exactly one field and
    inherit the rest::

        async def my_build(session):
            return 456245

        Client(token, version_resolver=VersionResolver(build_number=my_build))

    A bare callable is also accepted and treated as ``override=``, meaning
    it replaces the whole resolution and must return ``ResolvedVersions``.
    """

    def __init__(
        self,
        *,
        build_number: Callable | None = None,
        chrome_major: Callable | None = None,
        desktop: Callable | None = None,
        android: Callable | None = None,
        ios: Callable | None = None,
        override: Callable | None = None,
    ) -> None:
        if override is not None and any(
            f is not None for f in (build_number, chrome_major, desktop, android, ios)
        ):
            raise ValueError("override cannot be combined with per-field resolvers")
        self.override = override
        self.build_number = build_number or fetch_build_number
        self.chrome_major = chrome_major or fetch_chrome_major
        self.desktop = desktop or fetch_desktop_versions
        self.android = android or fetch_android_version
        self.ios = ios or fetch_ios_version

    async def resolve(self, session) -> ResolvedVersions:
        if self.override is not None:
            result = self.override(session)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
            if not isinstance(result, ResolvedVersions):
                raise TypeError(
                    "a version_resolver override must return ResolvedVersions, "
                    f"got {type(result).__name__}"
                )
            return result

        # return_exceptions so one broken custom resolver cannot abort boot.
        results = await asyncio.gather(
            self.build_number(session),
            self.chrome_major(session),
            self.desktop(session),
            self.android(session),
            self.ios(session),
            return_exceptions=True,
        )
        build, chrome_major, desktop, android, ios = (
            await asyncio.gather(
                *(
                    _value_or(value, fallback, session)
                    for value, fallback in zip(
                        results,
                        (
                            fetch_build_number,
                            fetch_chrome_major,
                            fetch_desktop_versions,
                            fetch_android_version,
                            fetch_ios_version,
                        ),
                        strict=True,
                    )
                )
            )
        )
        app_v, electron_v, chrome_build = desktop
        android_v, android_header = android
        return ResolvedVersions(
            build_number=build,
            chrome_major=chrome_major,
            desktop_app=app_v,
            desktop_electron=electron_v,
            desktop_chrome=chrome_build,
            android_app=android_v,
            android_header=android_header,
            ios_app=ios,
        )


async def _value_or(result, fallback, session):
    """A failing custom resolver degrades to the built-in source."""
    if isinstance(result, BaseException):
        log.warning("version resolver failed (%r); using the built-in source", result)
        return await fallback(session)
    return result


DEFAULT_RESOLVER = VersionResolver()


def coerce_resolver(value) -> VersionResolver:
    """Accept a VersionResolver, a bare callable, or None."""
    if value is None:
        return DEFAULT_RESOLVER
    if isinstance(value, VersionResolver):
        return value
    if callable(value):
        return VersionResolver(override=value)
    raise TypeError(
        f"version_resolver must be a VersionResolver or callable, "
        f"got {type(value).__name__}"
    )


async def resolve_all(session) -> ResolvedVersions:
    """Fetch everything concurrently; each field falls back independently."""
    return await DEFAULT_RESOLVER.resolve(session)


class ResolvedVersions:
    __slots__ = (
        "build_number",
        "chrome_major",
        "desktop_app",
        "desktop_electron",
        "desktop_chrome",
        "android_app",
        "android_header",
        "ios_app",
    )

    def __init__(
        self,
        *,
        build_number: int = FALLBACK_BUILD_NUMBER,
        chrome_major: str = FALLBACK_CHROME_MAJOR,
        desktop_app: str = FALLBACK_DESKTOP_APP,
        desktop_electron: str = FALLBACK_DESKTOP_ELECTRON,
        desktop_chrome: str = FALLBACK_DESKTOP_CHROME,
        android_app: str = FALLBACK_ANDROID_APP,
        android_header: str = FALLBACK_ANDROID_HEADER,
        ios_app: str = FALLBACK_IOS_APP,
    ) -> None:
        self.build_number = build_number
        self.chrome_major = chrome_major
        self.desktop_app = desktop_app
        self.desktop_electron = desktop_electron
        self.desktop_chrome = desktop_chrome
        self.android_app = android_app
        self.android_header = android_header
        self.ios_app = ios_app


# -- OS-derived defaults (stdlib only) --------------------------------------
# Windows reports locale display names ("English_United States"), not tags.
# Only the common ones are mapped; anything unmapped falls back to the default
# rather than shipping an invalid tag into X-Discord-Locale.
_DROP_PARENS = str.maketrans("", "", "()")

_RAW_WINDOWS_LOCALE_TAGS = {
    "english_united states": "en-US",
    "english_united kingdom": "en-GB",
    "english_canada": "en-CA",
    "english_australia": "en-AU",
    "english_new zealand": "en-NZ",
    "english_india": "en-IN",
    "english_ireland": "en-IE",
    "english_south africa": "en-ZA",
    "german_germany": "de-DE",
    "german_austria": "de-AT",
    "german_switzerland": "de-CH",
    "french_france": "fr-FR",
    "french_canada": "fr-CA",
    "french_belgium": "fr-BE",
    "spanish_spain": "es-ES",
    "spanish_mexico": "es-MX",
    "spanish_argentina": "es-AR",
    "spanish_colombia": "es-CO",
    "portuguese_brazil": "pt-BR",
    "portugal_portuguese": "pt-PT",
    "russian_russia": "ru-RU",
    "italian_italy": "it-IT",
    "dutch_netherlands": "nl-NL",
    "polish_poland": "pl-PL",
    "turkish_turkey": "tr-TR",
    "swedish_sweden": "sv-SE",
    "danish_denmark": "da-DK",
    "norway_norwegian": "nb-NO",
    "finnish_finland": "fi-FI",
    "czech_czech republic": "cs-CZ",
    "greek_greece": "el-GR",
    "hebrew_israel": "he-IL",
    "hungarian_hungary": "hu-HU",
    "romanian_romania": "ro-RO",
    "ukrainian_ukraine": "uk-UA",
    "hindi": "hi-IN",
    "japanese": "ja-JP",
    "korean": "ko-KR",
    "chinese_simplified_china": "zh-CN",
    "chinese_simplified_singapore": "zh-SG",
    "chinese_traditional_taiwan": "zh-TW",
    "chinese_traditional_hong kong": "zh-HK",
    "thai_thailand": "th-TH",
    "vietnamese_vietnam": "vi-VN",
    "indonesian_indonesia": "id-ID",
}


def _win_locale_key(name: str) -> str:
    """Fold a Windows display name to a stable lookup key.

    Windows is inconsistent about separators ("English_United States" vs
    "English (United States)"), so underscores, spaces, and parentheses all
    collapse together.
    """
    return " ".join(name.lower().replace("_", " ").translate(_DROP_PARENS).split())


WINDOWS_LOCALE_TAGS = {
    _win_locale_key(k): v for k, v in _RAW_WINDOWS_LOCALE_TAGS.items()
}

_BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

# Discord validates X-Discord-Timezone, so only forward real IANA ids.
# "UTC" is a legitimate bare id; everything else is Area/City.
_IANA_ZONE = re.compile(r"^(?:UTC|GMT|[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+)$")

# Legacy Windows display names, still present in TimeZoneKeyName on older
# installs. Only the widely used zones are mapped; the rest fall through.
WINDOWS_TIMEZONE_TAGS = {
    "coordinated universal time": "UTC",
    "pacific standard time": "America/Los_Angeles",
    "mountain standard time": "America/Denver",
    "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York",
    "atlantic standard time": "America/Halifax",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "brazil east standard time": "America/Sao_Paulo",
    "argentina standard time": "America/Argentina/Buenos_Aires",
    "romance standard time": "Europe/Paris",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "gtb standard time": "Europe/Bucharest",
    "e. europe standard time": "Europe/Chisinau",
    "russian standard time": "Europe/Moscow",
    "south africa standard time": "Africa/Johannesburg",
    "india standard time": "Asia/Kolkata",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "singapore standard time": "Asia/Singapore",
    "taipei standard time": "Asia/Taipei",
    "w. australia standard time": "Australia/Perth",
    "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
    "egypt standard time": "Africa/Cairo",
    "israel standard time": "Asia/Jerusalem",
    "arabian standard time": "Asia/Dubai",
    "arabic standard time": "Asia/Baghdad",
    "turkey standard time": "Europe/Istanbul",
    "sa pacific standard time": "America/Bogota",
    "sa western standard time": "America/La_Paz",
    "sa east standard time": "America/Cayenne",
    "greenland standard time": "America/Nuuk",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "morocco standard time": "Africa/Casablanca",
    "pakistan standard time": "Asia/Karachi",
    "bangladesh standard time": "Asia/Dhaka",
    "sri lanka standard time": "Asia/Colombo",
    "se asia standard time": "Asia/Bangkok",
    "w. mongolia standard time": "Asia/Hovd",
    "nepal standard time": "Asia/Katmandu",
    "central asia standard time": "Asia/Almaty",
    "aus central standard time": "Australia/Darwin",
    "lincolnshire standard time": "Europe/London",
}


def _normalize_tag(candidate: str | None) -> str | None:
    """Coerce a locale string into BCP-47, or None if it cannot be trusted."""
    if not candidate:
        return None
    candidate = candidate.split(".")[0].split("@")[0].strip()  # drop .UTF-8, @modifier
    if not candidate or candidate.lower() in ("c", "posix"):
        return None  # the C/POSIX locale is not a real language
    dashed = candidate.replace("_", "-")
    if _BCP47.match(dashed):
        return dashed
    # Windows display name: "English_United States" -> "english united states"
    return WINDOWS_LOCALE_TAGS.get(_win_locale_key(candidate))


def system_locale(default: str = "en-US") -> str:
    """Best-effort BCP-47 tag. Env first, then the OS, then ``default``.

    ``locale.getdefaultlocale`` is removed in 3.13 and returns display names
    on Windows, so it is deliberately not used.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        if (tag := _normalize_tag(os.environ.get(var))) is not None:
            return tag
    try:
        if (tag := _normalize_tag(locale.getlocale()[0])) is not None:
            return tag
    except Exception:
        pass
    if os.name == "nt":
        try:  # HKCU\Control Panel\International
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Control Panel\International"
            ) as key:
                for name in ("LocaleName", "sLanguage"):
                    try:
                        value = str(winreg.QueryValueEx(key, name)[0])
                    except OSError:
                        continue
                    if (tag := _normalize_tag(value)) is not None:
                        return tag
        except Exception:
            pass
    return default


def _windows_timezone() -> str | None:
    try:  # TimeZoneKeyName holds the IANA id on current Windows
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as key:
            raw = str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0])
    except Exception:
        return None
    # Older installs store the legacy display name ("Eastern Standard Time")
    # instead of an IANA id; map it, never forward it.
    return _valid_timezone(raw) or WINDOWS_TIMEZONE_TAGS.get(
        " ".join(raw.lower().split("_")).strip()
    )


def _valid_timezone(candidate: str | None) -> str | None:
    """Accept only something Discord will recognise as a tz id."""
    if not candidate or not _IANA_ZONE.match(candidate):
        return None
    try:  # Confirm the id exists when tzdata is available; skip if it is not.
        import zoneinfo

        available = zoneinfo.available_timezones()
        if available and candidate not in available:
            return None
    except Exception:
        pass
    return candidate


def system_timezone(default: str = "America/New_York") -> str:
    """Best-effort IANA timezone. Env first, then OS sources, then ``default``.

    Should agree with the proxy's geolocation or the fingerprint self-defeats,
    so pass ``timezone=`` explicitly whenever it is known.
    """
    if (tz := _valid_timezone(os.environ.get("TZ"))) is not None:
        return tz
    try:  # Debian/Ubuntu: plain text file
        with open("/etc/timezone", encoding="utf-8") as fh:
            if (tz := _valid_timezone(fh.read().strip())) is not None:
                return tz
    except OSError:
        pass
    try:  # Linux/macOS: /etc/localtime -> .../zoneinfo/<Area>/<City>
        if (tz := _valid_timezone(os.readlink("/etc/localtime"))) is not None:
            return tz
    except OSError:
        pass
    if os.name == "nt" and (tz := _windows_timezone()) is not None:
        return tz
    try:  # tzinfo.key is the IANA id when zoneinfo backs the local tz
        import datetime

        key = getattr(datetime.datetime.now().astimezone().tzinfo, "key", None)
        if (tz := _valid_timezone(key)) is not None:
            return tz
    except Exception:
        pass
    return default


def system_cpu_count(default: int = 8) -> int:
    return os.cpu_count() or default
