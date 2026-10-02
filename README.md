# despise — minimal discord.py-self

Single-session, `curl_cffi`-only selfbot core: dynamic header spoofing,
proper super-properties, 7 restart-frozen profiles.

## Install

```bash
pip install -e ".[dev]"
```

Only runtime dep: `curl-cffi`. (Upstream `discord.py-self` pulls
aiohttp, aiodns, Brotli, cchardet, mmh3, orjson, tzlocal, zstandard,
audioop-lts, discord-protos, davey, PyNaCl… — none of that here.)

Optional extras: `session-cache` (per-token gateway session cache, needs
`cashews`), `session-cache-redis` (same, across processes/workers).

## Use

```python
import asyncio
from despise import Client, build_activity

async def main():
    bot = Client("token-here", profile="web")  # web|desktop|android|ios|xbox|playstation|vr

    @bot.event
    async def on_ready(data):
        print("connected as", bot.user.username)

    @bot.event
    async def on_message(msg):
        print(msg["author"]["username"], msg["content"])
        if msg["content"] == "!ping":
            await bot.rest.send_message(msg["channel_id"], "pong")

    await bot.run()  # boot + gateway, then block until disconnected

asyncio.run(main())
```

`run()` blocks; the gateway's real work happens in background tasks, so an
`await` that returned would let `asyncio.run` tear the socket down with it.
Use `start()` to get a connected client without blocking, `close()` to shut
down, or the context manager:

```python
async with Client(token) as bot:
    await bot.wait_until_ready()
    ...
```

Rich presence (after READY, per-connection 5s throttle):

```python
await bot.set_presence([build_activity("Chess", kind="competing")])
```

DMs / servers / friends via `bot.rest`: `open_dm`, `send_message`,
`edit_message`, `delete_message`, `typing`, `history`, `pin`, `unpin`,
`accept_invite`, `get_guild`, `leave_guild`, `relationships`, `add_friend`,
`remove_relationship`, `block`.

Waiting on a specific event:

```python
msg = await bot.wait_for("MESSAGE_CREATE", check=lambda m: m["author"]["id"] == uid)
```

### Guild subscriptions

READY reports `max_concurrency`; going over it is close code 4013 and costs a
reconnect, so `subscribe_guild` refuses locally instead. Slots are released by
`unsubscribe_guild` or automatically on `GUILD_DELETE`.

```python
print(bot.gateway.max_concurrency)     # e.g. 16
await bot.gateway.subscribe_guild(gid)
await bot.gateway.unsubscribe_guild(gid)
```

### Member lists

```python
# lazy member-list sync: chunks arrive as GUILD_MEMBERS_UPDATE events
nonce = await bot.gateway.lazy_load_members(gid)
chunk = await bot.wait_for("GUILD_MEMBERS_UPDATE", check=lambda d: d["nonce"] == nonce)

# or a targeted lookup (requires the GUILD_MEMBERS intent)
await bot.gateway.request_members(gid, query="bob")
```

### Images and files

Avatars, banners, guild icons and emoji go out as a base64 data URI in the
JSON body; webhook attachments use real multipart.

```python
await bot.rest.set_avatar(png_bytes)                       # b"" clears it
await bot.rest.set_banner(png_bytes)
await bot.rest.create_guild_emoji(gid, "blob", png_bytes)
await bot.rest.upload_webhook_file(wid, token, data, filename="x.png")
```

### Cached lookups

`get_guild` / `get_channel` / `get_user` hit a small bounded cache first and
only fall through to REST on a miss. The cache is fed by gateway dispatches,
so entities you have already seen cost nothing.

```python
guild   = await bot.get_guild(gid)      # None on 404/403
channel = await bot.get_channel(cid)
user    = await bot.get_user(uid)
member  = bot.get_member(uid, gid)      # cache-only, sync
bot.clear_cache()
```

Models compare and hash by snowflake, so identity checks behave:
`msg.author == bot.user` is `True`. Every model keeps its source payload in
`.raw` for anything not modelled.

### Typing indicator

Discord expires a typing indicator after ~10s, so `typing()` alone is not
enough for a slow response:

```python
async with bot.rest.typing_loop(channel_id):
    await do_slow_work()
```

### Interactions

```python
from despise import build_action_row, build_button, defer, message_response

@bot.on("INTERACTION_CREATE")
async def on_interaction(data):
    if data["type"] == 3:  # MESSAGE_COMPONENT
        custom_id = data["data"]["custom_id"]
        await bot.rest.interaction_callback(
            data["id"], data["token"],
            message_response(f"you pressed {custom_id}", ephemeral=True),
        )
```

Builders in `despise.interactions` cover buttons, action rows, string/user
selects, modals, and message / update / defer / modal / autocomplete
responses. Discord's length and count limits are enforced at build time.

### Background tasks

```python
from despise import task_loop

@task_loop(seconds=30)
async def announce(task):
    await bot.rest.send_message(CHANNEL, "tick")

announce.start()      # Client.close() cancels registered tasks
```

`Task` supports `jitter=`, `max_runs=`, and isolates per-run exceptions.

## Options

| kwarg | default | notes |
|---|---|---|
| `profile` | `"web"` | one of the 7 spoofed clients |
| `intents` | `0` | |
| `proxy`, `timeout` | `None`, `30.0` | on the one shared session |
| `api_base` | `/api/v9` | canary/ptb |
| `gateway_encoding` | `"json"` | or `"zlib-stream"` |
| `session_cache` | `False` | `True` = in-memory, or a cashews URL |
| `version_resolver` | `None` | pin version lookups (see below) |
| `cache_caps` | `None` | `(guilds, channels, users)` limits |

`gateway_encoding="zlib-stream"` compresses every frame — much less bandwidth
on guild-heavy accounts, at the cost of doing the codec in-process.

`session_cache=True` stores the gateway session (`session_id`, `seq`,
`resume_url`) per token so a reconnect resumes instead of re-identifying.
Identify is rate limited per token, so losing the session on every blip
eventually locks the account out. Two clients sharing a token share the
cache; different tokens never do. In-memory only — use a `redis://` URL if
the session must outlive the process.

`version_resolver` overrides how the profile's versions are discovered at
boot. Pass a `VersionResolver` to pin individual fields (anything omitted or
raising falls back to the built-in source), or a bare callable to replace the
whole bundle:

```python
from despise.versions import VersionResolver, ResolvedVersions

async def pinned_build(session):
    return 456245

Client(token, version_resolver=VersionResolver(build_number=pinned_build))
Client(token, version_resolver=lambda s: ResolvedVersions(build_number=456245))
```

## Errors

Every failure is typed and carries Discord's own error code, so you branch
on data instead of parsing strings:

```python
from despise import Forbidden, NotFound, RateLimited, TooManySubscriptions

try:
    await bot.rest.delete_message(cid, mid)
except NotFound:
    ...                       # already gone
except Forbidden as e:
    if e.code == 50013:       # Missing Permissions
        ...
except RateLimited as e:
    await asyncio.sleep(e.retry_after or 1)
```

`HTTPError` is the base; specific subclasses are `AuthenticationHTTPError`,
`Forbidden`, `NotFound`, `RateLimited`, `ServerError`. A fatal gateway close
raises `AuthenticationFailed` / `InvalidIntents` / `GatewayClosed`, and
over-subscribing raises `TooManySubscriptions`.

## Rules

- **One shared `AsyncSession`** (`DespiseHttp` owns it; gateway REST, WS,
  and boot version-fetches borrow it). Same impersonate/proxy/timeout.
- **Restart-frozen profiles**: versions resolve once in `boot()` over the
  shared session — client build (login-page JS scrape), Chrome major
  (Google Version History), Electron + its Chrome (npm + electron
  releases), iOS app (iTunes lookup). Locale/CPU come from the OS.
  Anything with no public feed (desktop `1.0.x` host, Android app,
  console app UAs) stays a single documented fallback, used only offline.
  Mid-run only ephemeral ids change (launch id, 30-min heartbeat
  session id + op40/op41).
- `browser_user_agent` property == `User-Agent` header, except mobile
  (android/ios) where the property is `""` and the header carries
  `Discord-Android/<n>;RNA` / `Discord/<v> CFNetwork/… Darwin/…`.
- Fingerprint extras (resolution, cpu, ram) never enter X-Super-Properties.
- Consoles/VR impersonate the closest Chromium stack (`chrome`;
  android `chrome_android`, ios `safari_ios`) — curl_cffi has no
  console presets; approved approximation, noted in code.
- **Frozen headers live on the `AsyncSession`**, not per request: passing
  `headers=` per call makes curl_cffi merge them over its own impersonation
  defaults, which perturbs header ordering. Only `Authorization`,
  `X-Context-Properties` and `X-Audit-Log-Reason` are per request, and they
  override the session set. `Authorization` stays per-request because
  curl_cffi stringifies a `None` override as the literal `"None"`, so a
  session-level token could not be suppressed for unauthenticated calls.
- **Rate limits are keyed off Discord's own headers** —
  `X-RateLimit-Bucket` + `X-RateLimit-Scope`. The path heuristic is only a
  bootstrap guess until the first response reveals the real bucket, and
  `scope: user` buckets all serialize through one account-wide lock.
  `X-RateLimit-Precision: millisecond` is honoured.
- **Headers are per-surface, not one-size-fits-all.** `Origin`, `Referer` and
  `Sec-Fetch-*` are sent only by the `web` preset — the one surface that loads
  a page. iOS sends no `Sec-CH-UA` at all, because its TLS is WebKit and
  WebKit does not send client hints; claiming Chromium there was a
  self-inflicted contradiction. `ClientKind` encodes these rules instead of
  scattering browser-name string comparisons.

## Connection lifecycle

- `connect()` handshakes, hands the socket to a *tracked* reader task, and
  returns on READY. There are exactly three background tasks (reader,
  heartbeat, rotate) and teardown owns all of them.
- Reconnect policy comes from the WebSocket **close code**, not a guess:
  `4004/4010–4014` are fatal and raise; `4007/4009` re-identify; `4008`
  backs off harder because Discord throttles Identify per token. Anything
  else resumes from `resume_gateway_url` + last `seq`.
- Backoff is exponential with jitter and capped at 60s. A handshake that
  itself fails is retried (up to 10 times) rather than left to kill the
  reconnect chain; giving up sets a terminal error that `run()` re-raises.
- Listener exceptions are caught per-listener and routed to `on_error`;
  one bad callback cannot kill the read loop or force a reconnect.
- A re-Identify (after `INVALID_SESSION: false`, a fresh close code, or 4008)
  replays the last presence. The *first* Identify legitimately sends none.
- `close()` is final: it wins any race with an in-flight handshake and no
  reconnect can resurrect the client afterwards.

## Test

```bash
python -m pytest tests/ -q
python -m ruff check despise/ tests/
```