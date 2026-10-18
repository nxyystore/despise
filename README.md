# despise

A selfbot core that stays connected and stays unnoticed. One HTTP session, seven client profiles, close code driven reconnects.

`discord.py-self` pulls aiohttp, Brotli, orjson, PyNaCl, and a dozen other packages. Despise runs on `curl-cffi` plus the standard library. Dependencies stay minimal by design.

## Install

```bash
pip install -e ".[dev]"
```

Python 3.10 or newer. `curl-cffi` is the single runtime dependency. The gateway session cache ships in the box, in memory by default, `redis://` when you need cross process resume.

## Quickstart

```python
import asyncio
from despise import Client, build_activity

async def main():
    bot = Client("token-here", profile="web")  # web, desktop, android, ios, xbox, playstation, vr

    @bot.event
    async def on_ready():
        print("connected as", bot.user.username)

    @bot.event
    async def on_message(msg):
        print(msg.author.username, msg.content)
        if msg.content == "!ping":
            await bot.rest.send_message(msg.channel_id, "pong")

    await bot.run()  # boot, handshake, then hold the connection open

asyncio.run(main())
```

`run()` holds the connection until it drops for good or you call `close()`. You hold the event loop, so `asyncio.run` has something to wait on. For manual control, call `start()` to connect and return on READY, `close()` to shut down, or use the context manager:

```python
async with Client(token) as bot:
    await bot.wait_until_ready()
    ...
```

Wait on a single event when you need one:

```python
msg = await bot.wait_for("message", check=lambda m: m.author.id == uid)
```

Handlers take friendly discord.py-self style names and receive one model each. Raw dispatch names still work and hand you the raw dict. A dispatch with no friendly name yet (`GUILD_BAN_ADD`, `THREAD_CREATE`, ...) works too -- subscribe by its raw name and get the raw dict, so you are never blocked on a table update.

```python
@bot.event
async def on_message_edit(msg): ...   # MESSAGE_UPDATE -> Message
@bot.event
async def on_guild_join(guild): ...   # GUILD_CREATE -> Guild
@bot.event
async def on_member_join(member): ... # GUILD_MEMBER_ADD -> Member

bot.on("message", handler)            # same as on_message
bot.on("MESSAGE_CREATE", handler)     # raw dict instead of a model
bot.on("GUILD_BAN_ADD", handler)      # any unmapped dispatch, raw dict
```

Names: `ready`, `resume`, `message`, `message_edit`, `message_delete`, `guild_join`, `guild_update`, `guild_remove`, `channel_create`, `channel_update`, `channel_delete`, `member_join`, `member_update`, `member_remove`, `typing`, `reaction_add`, `reaction_remove`, `presence_update`, `voice_state`, `interaction`, `relationship_add`, `relationship_remove`, `invite_create`, `invite_delete`, `members_chunk`. `on_ready` and `on_resume` take no arguments. `reaction_add`/`reaction_remove` hand you a `ReactionEvent` (the message, channel, user, and emoji a bare `Reaction` cannot carry).

## What you get

- Seven spoofed clients with frozen headers, correct super properties, and per surface TLS behavior.
- A gateway that resumes from close codes, backs off with jitter, and replays presence after re identify.
- Rate limiting keyed off Discord response headers, with per bucket locks and an account wide lock for `scope: user`.
- Typed models with snowflake identity, parsed timestamps, and the raw payload kept on `.raw`.
- REST coverage for messages, DMs, read state, guilds, members, roles, bans, friends, notes, profile edits, webhooks, and interactions.
- Friendly event names with a raw-dict fallback, so an unmapped dispatch still fires the day Discord ships it.
- Bounded entity cache plus a built in session cache. No extra installs.

## Profiles

Pick one string: `web`, `desktop`, `android`, `ios`, `xbox`, `playstation`, `vr`.

Despise resolves versions once at boot over the shared session and freezes them for the run. Client build comes from the login page JS, Chrome major from Google version history, Electron and its Chrome from npm and Electron releases, iOS from the iTunes lookup. Locale and CPU come from your OS. Fields with no public feed keep one documented fallback and apply only when offline. During the run, only ephemeral ids rotate: launch id plus the 30 minute heartbeat session id with op40/op41.

```python
bot = Client(token, profile="desktop", locale="fr-FR", timezone="Europe/Paris")
```

Mobile sends no `browser_user_agent` property and carries its UA in the header instead (`Discord-Android/...`, `Discord/... CFNetwork`). Only `web` sends `Origin`, `Referer`, and `Sec-Fetch-*`. iOS sends no `Sec-CH-UA` because its WebKit TLS stack sends none. Consoles borrow the closest Chromium impersonation curl_cffi offers. You can pin any version field without patching the module:

```python
from despise.versions import VersionResolver, ResolvedVersions

async def pinned_build(session):
    return 456245

Client(token, version_resolver=VersionResolver(build_number=pinned_build))
Client(token, version_resolver=lambda s: ResolvedVersions(build_number=456245))
```

## Gateway

### Subscriptions

READY reports `max_concurrency`, Discord's *session-start* limit: how many shards may identify at once (`rate_limit_key = shard_id % max_concurrency`). It is not a guild-subscription cap, and user accounts do not receive it. Identifying past the limit earns an Invalid Session (op 9), not a close. `subscribe_guild` keeps a local best-effort cap from that field when it is present and raises `TooManySubscriptions` rather than letting the gateway throttle you. `unsubscribe_guild` and `GUILD_DELETE` free slots. (For the record: close code `4013` is *invalid intents*, `4014` is *disallowed intents* — see the [gateway close codes](https://docs.discord.food/gateway/opcodes-and-close-codes#gateway-close-event-codes).)

```python
print(bot.gateway.max_concurrency)  # e.g. 16
await bot.gateway.subscribe_guild(gid)
await bot.gateway.unsubscribe_guild(gid)
```

### Member lists

```python
nonce = await bot.gateway.lazy_load_members(gid)
chunk = await bot.wait_for("members_chunk", check=lambda d: d["nonce"] == nonce)

await bot.gateway.request_members(gid, query="bob")  # needs the GUILD_MEMBERS intent
```

Lazy sync streams op 12 chunks. Targeted lookup sends op 8 with your query.

### Encoding

```python
Client(token, gateway_encoding="json")  # plain text frames, for traffic inspection
```

`zlib-stream` is the default. It compresses each frame in process and cuts bandwidth on guild heavy accounts. Pass `gateway_encoding="json"` for plain text frames when you inspect traffic.

### Session cache

```python
Client(token, session_cache=True)                  # in memory, same process
Client(token, session_cache="redis://127.0.0.1:6379/0")  # survives restarts
```

With the cache on, a reconnect resumes from `session_id` plus last `seq` instead of sending a fresh Identify. Identify carries a per token rate limit, so a flapping connection without session memory burns budget and risks lockout. Two clients on one token share the entry through the `for_token` registry. Separate tokens stay separate. Entries expire after an hour. `close()` and fatal or fresh closes clear the entry.

### Presence

```python
await bot.set_presence([build_activity("Chess", kind="competing")])
await bot.clear_presence()
```

Send presence after READY. The gateway throttles you to one update per 5 seconds per connection and replays your last presence on re identify.

## REST

`bot.rest` covers DMs, messages, reactions, read state, guilds, members, roles, bans, friends, profile, notes, and webhooks: `open_dm`, `create_group_dm`, `private_channels`, `send_message`, `edit_message`, `delete_message`, `fetch_message`, `history`, `crosspost_message`, `pinned_messages`, `pin`, `unpin`, `typing`, `add_reaction`, `clear_reactions`, `ack_message`, `ack_bulk`, `get_channel`, `edit_channel`, `delete_channel`, `edit_channel_permissions`, `accept_invite`, `get_guild`, `guild_preview`, `leave_guild`, `get_member`, `edit_member`, `kick_member`, `add_member_role`, `ban`, `unban`, `bans`, `create_role`, `edit_role`, `delete_role`, `get_user`, `user_profile`, `set_note`, `relationships`, `add_friend`, `block`, and more.

### Sending

One call sends text, embeds, component rows, a poll, stickers, and files. `bot.send` accepts a channel id, a `Channel`, a `User` (which opens the DM first), or a `Message` (which replies):

```python
from despise import File, build_button, build_action_row

await bot.send(cid, "hello")
await bot.send(user, "hi there")                       # opens the DM
await bot.send(message, "replying")                    # replies in its channel
await bot.send(cid, "pic", files=[File.from_path("cat.png")])
await bot.send(cid, components=[build_action_row(build_button("Ping", "ping"))])
await bot.send(cid, poll={"question": {"text": "lunch?"}, "answers": [...], "duration": 24})
```

Files may be a `File`, a path, `bytes`, a `(filename, data[, content_type])` tuple, or a dict. Passing any of them switches the request to multipart automatically; everything else rides along in `payload_json`.

Anything the client or a dispatch handed you acts on itself:

```python
await message.reply("yes", files=[File.from_path("cat.png")])
await message.edit("fixed")
await message.react("\U0001f44d")
await message.delete()
await channel.send("in the channel")
async with channel.typing_loop():
    await do_slow_work()
```

### Read state

Read states drive the unread and ping badges. Ack a channel up to a message id (any valid snowflake) and Discord fires a `Message Ack`:

```python
await bot.rest.ack_message(cid, mid)                    # read up to mid
await message.ack()                                     # same, from the model
await bot.rest.ack_message(cid, mid, manual=True, mention_count=2)  # move back
await bot.rest.ack_pins(cid)                            # acknowledge pins
await bot.rest.ack_bulk([{"channel_id": cid, "message_id": mid}])
```

Moving the marker *backwards* needs `manual=True` and an accurate `mention_count`, or clients may ignore it. `message_ack`, `channel_pins_update` and `channel_unread_update` arrive as events.

### Typing

Discord drops the indicator after around 10 seconds. `typing()` fires once. `typing_loop()` refreshes every 8 seconds for slow work:

```python
async with bot.rest.typing_loop(channel_id):
    await do_slow_work()
```

A failed typing POST stays silent. It has no business killing your loop.

### Images and files

Avatars, banners, guild icons, and emoji travel as base64 data URIs in the JSON body. Webhook attachments use real multipart:

```python
await bot.rest.set_avatar(png_bytes)                       # b"" clears it
await bot.rest.set_banner(png_bytes)
await bot.rest.create_guild_emoji(gid, "blob", png_bytes)
await bot.rest.upload_webhook_file(wid, token, data, filename="x.png")
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

Builders in `despise.interactions` cover buttons, action rows, string and user selects, modals, and message, update, defer, modal, autocomplete, and pong responses. Discord length and count limits fail at build time, before you hit the network.

### Background tasks

```python
from despise import task_loop

@task_loop(seconds=30)
async def announce(task):
    await bot.rest.send_message(CHANNEL, "tick")

announce.start()  # Client.close() cancels registered tasks
```

`Task` takes `jitter=` and `max_runs=` and keeps per run exceptions from stopping later runs.

## Cache and models

`get_guild`, `get_channel`, and `get_user` check a small bounded cache first and fall through to REST on a miss. Gateway dispatches feed the cache, so entities you have seen cost no round trip. A failed fetch falls back to the cached copy instead of failing your handler.

```python
guild   = await bot.get_guild(gid)   # None on 404/403
channel = await bot.get_channel(cid)
user    = await bot.get_user(uid)
member  = bot.get_member(uid, gid)   # cache only, sync
bot.clear_cache()
```

Models compare and hash by snowflake, so `msg.author == bot.user` holds. Timestamps parse to timezone aware datetimes. Each model keeps its source payload on `.raw`.

## Modules

Drop a file in `cmd/` and it becomes prefix commands plus event listeners. `cmd/general.py` ships as the example:

```python
from despise.cmd import Module, command, listen

class General(Module):
    @command("ping", aliases=["p"])
    async def ping(self, ctx):
        await ctx.reply(f"pong ({ctx.author.username})")

    @listen("message")
    async def log(self, msg):
        print(msg.author.username, msg.content)
```

```python
bot.modules.load("cmd/general.py")    # by file, no packaging needed
bot.modules.reload("general")         # picks up your edits
bot.modules.unload("general")         # unhooks listeners, forgets commands
```

Commands parse with shell style quoting, so `!say "hello world"` arrives as one arg. `ctx` carries `bot`, `message`, `name`, `args`, `text`, plus `reply` and `send`. Command errors dispatch as `command:ping`, so `bot.on_error("command:ping", ...)` scopes failure handling to one command. Set the prefix once with `Client("token", command_prefix=".")`.

## Options

| kwarg | default | notes |
|---|---|---|
| `profile` | `"web"` | one of the 7 clients |
| `proxy`, `timeout` | `None`, `30.0` | set on the one shared session |
| `api_base` | `/api/v9` | point at canary or ptb here |
| `gateway_encoding` | `"zlib-stream"` | or `"json"` |
| `session_cache` | `False` | `True` for memory, `redis://` URL for cross process |
| `version_resolver` | `None` | pin version lookups |
| `cache_caps` | `None` | `(guilds, channels, users)` caps; defaults 512/2048/4096 |

## Errors

Failures carry types and Discord error codes. Branch on data:

```python
from despise import Forbidden, NotFound, RateLimited, TooManySubscriptions

try:
    await bot.rest.delete_message(cid, mid)
except NotFound:
    ...                    # already gone
except Forbidden as e:
    if e.code == 50013:    # Missing Permissions
        ...
except RateLimited as e:
    await asyncio.sleep(e.retry_after or 1)
```

`HTTPError` is the base. Subclasses include `AuthenticationHTTPError`, `Forbidden`, `NotFound`, `RateLimited`, and `ServerError`. Fatal closes raise `AuthenticationFailed`, `InvalidIntents`, `TooManySessions`, or `GatewayClosed`. Over subscribing raises `TooManySubscriptions`. An `AuthenticationHTTPError` (REST `401`) is terminal: it stops the client rather than being retried.

## How connections behave

- `connect()` handshakes, tracks the reader task, and returns on READY. Three background tasks exist (reader, heartbeat, rotate) and teardown owns them all.
- The close code picks the policy. `4004` and `4010` through `4015` raise (`4015` means too many user-account sessions — reconnect just re-trips it). `4007` and `4009` re identify. `4008` backs off harder because Discord throttles Identify per token. Other closes resume from `resume_gateway_url` plus last `seq`.
- Backoff grows exponentially with jitter and caps at 60 seconds. A failed handshake retries up to 10 times. Repeated failure sets a terminal error that `run()` re raises.
- Listener exceptions stay per listener and route to `on_error`. One bad callback stops neither the read loop nor the connection.
- `close()` is final. It wins races with in flight handshakes and no reconnect revives the client after it.

## How requests behave

- One shared `AsyncSession` serves gateway REST, the socket, and boot version fetches with one impersonation, proxy, and timeout.
- Frozen headers live on the session. Only `Authorization`, `X-Context-Properties`, and `X-Audit-Log-Reason` travel per request. `Authorization` stays per request because curl_cffi renders a `None` override as the string `"None"`, which blocks suppression of the token on unauthenticated calls such as `GET /gateway`.
- Rate limit keys come from Discord response headers (`X-RateLimit-Bucket` plus `X-RateLimit-Scope`). A path guess bootstraps the first request, then the first response teaches the real bucket. `scope: user` requests serialize through one account wide lock. Millisecond precision applies.
- Retries cover 500, 502, 503, and 504 on idempotent calls only, so a retried POST cannot double send. `Retry-After` applies. 521 and 522 stay untouched as edge refusals.
- A `401` on an authenticated request means the token is dead. The client trips a breaker instead of persisting: every authenticated call after it fails immediately with no network hit, the gateway stops for good (no reconnect, no resume), background tasks are cancelled, and `run()`/`wait_closed()` raises the `AuthenticationHTTPError`. A dead token never burns the Identify budget.

## Test

```bash
python -m pytest tests/ -q
python -m ruff check despise/ tests/
python -m mypy despise/
```

`pip install -e ".[test]"` pulls in `pytest-timeout`, so a hung test fails instead of wedging the run.

## License

MIT. See [LICENSE](LICENSE).
