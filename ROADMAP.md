# despise — improvement roadmap

Living document. Status markers:

- `[ ]` not started
- `[x]` done

Items that are *partially* done say so in prose (*Partially done: …*).

Verified against the codebase as of the Tier 0 pass (109 tests, ruff clean).

---

## Tier 0 — Correctness bugs

*The library's anti-detect surface is only ~40% built, and the session layer
underneath it had bugs that would bite before any fingerprint work mattered.*

- [x] `Client.run()` returned immediately, so `asyncio.run` tore the socket
      down. Split into `start()` / blocking `run()` / `close()` / async CM.
- [x] `FATAL_CLOSE_CODES` / `FRESH_CLOSE_CODES` were dead code — `close_code`
      was never read. 4004 looped forever instead of raising.
- [x] Heartbeat zombie-reconnect cancelled *itself* inside `_reconnect`, and
      its own `except CancelledError` swallowed it. `_stop_tasks` now excludes
      the calling task.
- [x] Background reader task was untracked and leaked across every reconnect,
      double-consuming frames.
- [x] Listener exceptions propagated into the read loop and forced a
      reconnect. Now isolated per-listener and routed to `on_error`.
- [x] Presence throttle was a module-level global with no lock; two clients in
      one process stomped each other's 5s window. Moved onto `Gateway`.
- [x] `_ws` / `_ctx` were `None` during reconnect windows while
      `rest.subscribe_guild` reached into `gateway._ws`.
- [x] `assert` used for protocol validation (stripped under `python -O`) →
      explicit raises.
- [x] `_acked` never re-armed on reconnect → phantom zombie.
- [x] `connect()` could hang forever if READY never arrived → handshake
      timeouts.
- [x] `system_locale()` used `locale.getdefaultlocale()` (removed in 3.13, and
      returns Windows display names like `"English_United States"`), shipping
      invalid BCP-47 into `X-Discord-Locale`.
- [x] `system_timezone()` was Linux-only and always claimed `America/New_York`
      on Windows/macOS — silently mismatching the proxy geolocation.
- [x] Typed exception hierarchy replacing `RuntimeError("HTTP 401")`.
- [x] `proxy` / `timeout` existed on `DespiseHttp` but no `Client` path
      forwarded them.

### Found while implementing Tier 0
- [x] `_connect` leaked a half-open socket if the initial send burst raised.
- [x] A failed handshake during reconnect killed the reconnect chain, leaving
      the client permanently silent. Now retried (bounded) with growing backoff.
- [x] `close()` racing an in-flight handshake orphaned the socket and its
      tasks. `_connect` now races READY against `_closed`.

---

## Tier 1 — HTTP / rate-limit hardening

*The part most likely to get you banned or stuck. Highest value after Tier 0.*

- [x] Key rate-limit buckets on Discord's real `X-RateLimit-Bucket` +
      `X-RateLimit-Scope`, falling back to the path heuristic until the first
      response reveals the real id. Learning also *migrates* an armed reset off
      the heuristic bucket, or the next request would go out with no wait.
- [x] Serialize all `scope: user` requests through one account-wide lock.
      `X-RateLimit-Precision: millisecond` is honoured, and a later route can
      never shorten another route's reset.
- [x] Re-check the global 429 *after* acquiring the bucket lock — a burst that
      all passed the pre-lock check proceeds unthrottled.
- [x] Widen 5xx retry to 500/502/503/504; **gate on idempotency**, since
      re-POSTing a message after a 502 sends it twice. 521/522 are edge
      refusals and are not retried.
- [x] Honor `Retry-After`.
- [x] Move frozen headers onto the `AsyncSession` constructor. `Authorization`
      stays per-request on purpose: curl_cffi stringifies a `None` override as
      the literal `"None"`, so a session-level token could not be suppressed
      for unauthenticated calls (`GET /gateway` during boot).
- [x] `X-Audit-Log-Reason` (`reason=` kwarg).
- [x] Configurable `API_BASE` (canary/ptb).
- [x] Multipart file upload via `CurlMime` (curl_cffi rejects `files=`).
      Note: Discord takes avatars/banners/icons/emoji as a **data URI in the
      JSON body** — multipart buys nothing there — so multipart is reserved
      for webhook attachments, where it is actually required.
- [x] Logging (`despise.http` / `despise.gateway` / `despise.session_cache`),
      DEBUG line per request.
- [x] Repeated query params (`before`/`after`/`around` for history
      pagination) — `history()` emits a pair list.

---

## Tier 2 — Gateway correctness

- [x] Close-code-driven reconnect policy (fatal / fresh / identify-throttled /
      resume).
- [x] Exponential backoff with jitter, capped at 60s, bounded attempt count.
- [x] Identify cooldown on 4008 — Discord throttles Identify per token and a
      fast retry loop makes the penalty worse.
- [x] Track `max_concurrency` from READY. `subscribe_guild` refuses over the
      cap locally (`TooManySubscriptions`) instead of earning a 4013
      reconnect; slots free via `unsubscribe_guild` or `GUILD_DELETE`.
- [x] Cache `session_id` / `seq` / `resume_url` in a **`cashews` cache keyed
      per token** (`despise:gw:session:{token}`), not on disk.
      - Backend is pluggable: `mem://` (default) or `redis://` via
        `Client(session_cache="redis://...")`.
      - Per-token keying, never by user id — a token swapped under the same
        user must not resurrect the old session.
      - Per-token `Cache(name=...)`, not the module singleton: cashews
        instances share nothing (verified).
      - TTL (1h default) so an abandoned token can't resurrect a stale
        session; writes throttled to 30s except on READY/RESUMED, because
        `seq` advances on every dispatch.
      - `close()` and every fresh/fatal close **clear** the entry, so a
        retired session is never resurrected.
      - Reads/writes degrade to "no session" on cache failure — a broken
        cache must never take the gateway down.
      - Two clients sharing a token share the cache (`for_token` registry);
        different tokens never do.
      - **Tradeoff:** `mem://` does *not* survive a process restart, so
        "resume instead of re-identify after a restart" needs `redis://`.
        In-memory still buys token isolation and in-process handoff.

> **cashews 7.6.0 API — verified locally, differs from the upstream README.**
>
> ```python
> from cashews import Cache            # or: from cashews import cache  (singleton)
>
> c = Cache("despise")                 # name is arbitrary; gives isolation
> c.setup("mem://?size=1000")          # SYNC — returns a Backend, do NOT await
> await c.set(key, value, expire=3600)  # kwarg is `expire`, not `ttl`
> await c.get(key, default=None)
> await c.delete(key)
> await c.close()
> ```
>
> Gotchas: `cache.setup(...)` is not awaitable in 7.6 (it was typed as a
> coroutine in some 7.x builds — don't trust the signature); `is_setup` is a
> *method*, not a property; `mem://` stores dicts natively, no serialization
> needed.
- [x] Re-identify carries the current `presence`. Correctly still omitted on
      the *first* identify.
- [x] op 12 lazy member-list sync. `lazy_load_members(guild_id)` sends op 8
      with `limit=0` and returns the nonce; chunks dispatch as
      `GUILD_MEMBERS_UPDATE`, and under their own name for `SYNC_MEMBERS` /
      `LAZY_LOAD_FAILED`. *(Note: lazy sync is op 12 — op 9 is
      `INVALID_SESSION`, which the previous draft of this doc had wrong.)*
- [x] `subscribe_guild` moved off `gateway._ws` onto `Gateway`.
- [x] Track subscription state; cleared on READY and `GUILD_DELETE`.
- [x] Optional `encoding=zlib-stream` via `Client(gateway_encoding=...)`.
      Every frame (HELLO included) is compressed; we do the codec in-process.
- [x] `wait_until_ready()` / `wait_for(event, check=, timeout=)`.

---

## Tier 3 — API ergonomics / discord.py-self parity

*The biggest adoption blocker: the ergonomics gap vs `discord.py-self`.*

- [x] `@bot.event` decorator alongside `bot.on`.
- [x] `bot.dispatch(event, *args)`, `bot.on_error`.
- [x] `bot.user` is a `User` after READY (was a raw dict while `rest.me()`
      returned a `User`).
- [x] `bot.close()`, `async with Client(...)`.
- [x] `bot.get_guild` / `get_channel` / `get_user` with a **lightweight**
      cache (`despise/cache.py`). Bounded per type (LRU-ish, oldest evicted);
      fed from GUILD_CREATE/UPDATE/DELETE, CHANNEL_*, GUILD_MEMBER_* and
      MESSAGE_*; `GUILD_CREATE` indexes its whole channel list, and a
      CHANNEL_UPDATE *replaces* the stale entry instead of being ignored. REST
      on a miss, cache as the fallback when the request fails. Still
      deliberately not a member cache — that is unbounded by nature.
- [x] `bot.set_presence(...)` and `bot.clear_presence`.

---

## Tier 4 — Models are too thin

- [x] `__eq__` / `__hash__` on id, so `msg.author == bot.user` works.
      Safe across types: Discord snowflakes are globally unique. Id-less
      models never compare equal, so empty payloads don't collide.
- [x] Timestamps parsed to tz-aware `datetime`. Discord sends `Z`, explicit
      offsets and up to 6+ fractional digits — Python 3.10's `fromisoformat`
      rejects both, so `parse_timestamp` normalises. Unparseable -> `None`,
      never an exception.
- [x] `Message`: `type` (+ `type_name`), `attachments`, `embeds`, `mentions`,
      `mention_roles`, `mention_channels`, `reference`, `referenced_message`,
      `reactions`, `nonce`, `flags` (+ `suppressed_embeds` / `is_components_v2`
      / `is_ephemeral`), `polls`, `clean_content`, `sticker_items`,
      `components`, `member`, `webhook_id`, `application_id`.
- [x] `Channel`: `topic`, `position`, `parent_id`, `permission_overwrites`,
      `nsfw`, `thread_metadata`, + `is_thread`/`is_voice` predicates.
      `Guild`: `channels`, `members`, `roles`, `emojis`, `features`, `banner`,
      `preferred_locale`, `joined_at`, `icon_url`.
- [x] New models: `Member`, `Role`, `Attachment`, `Embed` (+`EmbedField`),
      `Reaction`, `MessageReference`, `Invite`, `Relationship`, `Emoji`,
      `Sticker`, `VoiceState`.
- [x] `raw` retained on every model.

---

## Tier 5 — Feature surface selfbot frameworks actually need

- [x] **Typing loop.** `rest.typing(channel_id)` stays a one-shot;
      `rest.typing_loop(channel_id)` returns a `TypingLoop` (8s refresh, since
      Discord expires the indicator around 10s). Usable as an async context
      manager. A failed POST is cosmetic and never kills the loop.
- [x] **Interactions** — `rest.run_interaction(payload)` (POST /interactions)
      and `rest.interaction_callback(id, token, response)`
      (PUT /interactions/{id}/{token}/callback), plus
      `edit_interaction_response` / `original_response`. Builders live in
      `despise/interactions.py`: buttons, action rows, string/user selects,
      modals, and message/update/defer/modal/autocomplete/pong responses, with
      Discord's length and count limits enforced.
- [x] `MessageCreate` extras: `flags` (`SUPPRESS_EMBEDS`,
      `IS_COMPONENTS_V2`, `EPHEMERAL`), `nonce`, `attachments`, `polls`.
      *Modelled in Tier 4.*
- [x] Avatars / profile edits — `set_avatar`, `set_banner`,
      `set_display_name`, `create_guild_emoji`, `set_guild_icon`, plus real
      multipart `upload_webhook_file`.
- [x] Newsletter channels, webhooks, follows — `newsletters`,
      `get_newsletter`, `newsletter_message`, `create_webhook`,
      `channel_webhooks`, `execute_webhook`, `delete_webhook[_with_token]`,
      `follow_channel`, `get_invite`.
- [x] `ext.tasks`-style helper (`despise/tasks.py`): `@loop(seconds=...)`,
      `Task.start/cancel`, jitter, `max_runs`, per-run crash isolation, and
      automatic cancellation on `Client.close()`. `ext.commands` stays out of
      scope. *`wait_for` shipped in an earlier pass.*

---

## Tier 6 — Profile / version fidelity

*The actual differentiator. Invest here.*

- [x] **Sec-CH-UA consistency per surface.** iOS now sends **no** client
      hints at all: its TLS is WebKit (`impersonate="safari_ios"`) and WebKit
      does not send `Sec-CH-UA`, so claiming Chromium was the contradiction.
      Android keeps them (real Chromium) with the GREASE brand ordered the way
      Chrome-on-Android orders it. Consoles keep them on purpose — their TLS
      is borrowed from Chromium, so omitting the matching CH headers would be
      the bigger mismatch. GREASE brand *strings* are Chromium-version
      dependent and rotate, so they remain an approximation; the structural
      rules above are what's enforced.
- [x] `ClientKind` enum replaces scattered `profile.browser in (...)` string
      comparisons, which broke silently on rename.
- [x] `Accept-Language` is built as `en-US,en;q=0.9` (base language as a
      lower-priority fallback).
- [x] `Origin` / `Referer` / `Sec-Fetch-*` are now sent **only** by the `web`
      preset — the one surface that actually loads a page. A native client
      (desktop/mobile/console) advertising `Sec-Fetch-Mode: cors` while its
      UA says `Discord-Android/...` is a contradiction. Referer is the SPA
      route (`/channels/@me`), not the bare origin.
- [ ] Consider high-entropy CH headers (`sec-ch-ua-platform-version`,
      `-model`, `-full-version-list`) for web/desktop.
- [ ] `X-Fingerprint` is dead. `base_headers` sets `X-Fingerprint: ""`, then
      `set_token` pops it — so it's never sent. Either fetch it from
      `/experiments` (where Discord actually puts it) or delete the header.
- [ ] Cache resolved versions to disk with a TTL. `fetch_build_number`
      downloads the login HTML plus *every* `/assets/*.js` chunk on every
      boot — tens of MB to learn one integer that changes weekly.
- [x] Pluggable version sources. `VersionResolver(build_number=..., ...)`
      takes per-field async callables; anything omitted or raising falls back
      to the built-in source, so one field can be pinned without patching the
      module. `override=` (or a bare callable) replaces the whole bundle.
      Wired through `Client(version_resolver=...)`.

---

## Tier 7 — Engineering hygiene

- [x] `.gitignore` (`__pycache__`, `.pytest_cache`, `.ruff_cache` were sitting
      untracked in the tree).
- [x] ruff config in `pyproject` + `dev` extra; `mypy` config added (mypy not
      installed locally yet).
- [x] `py.typed`, `__version__`.
- [x] End-to-end gateway tests. The old fake socket asserted on inbound
      frames, so a leaked reader showed up as a second consumer; the new one
      blocks like a live socket.
- [ ] Local `websockets` server for true reconnect/resume/heartbeat E2E.
- [ ] Hypothesis/property tests for the `versions.py` parsers — they're
      regex-on-HTML and break silently when Discord reshuffles its bundle.
- [ ] CI workflow, LICENSE, CHANGELOG, docs (at minimum a profiles table).

---

## Suggested target layout

```
despise/
  __init__.py       exports + __version__
  client.py         Client (start/run/close/wait_for/dispatch)
  exceptions.py     typed Discord error hierarchy          [done]
  http.py           session, rate limiter, retries, upload
  gateway.py        state machine, close codes, ops        [mostly done]
  rest.py           tiered endpoints
  models/           User, Message, Channel, Guild, Member, ...   [Tier 4]
  profile/          ClientKind enum, profile builder, CH headers
  versions/         resolvers + disk cache
```

---

## Phasing

| Phase | Scope | Rationale |
|-------|-------|-----------|
| 1 | Tier 0 | Nothing else matters until reconnect is trustworthy. **Done.** |
| 2 | Tier 1 + Tier 6 | The anti-detect story: correct rate limiting + a coherent profile per preset. Where the library's actual differentiator lives. |
| 3 | Tier 4 + Tier 5 | Model parity then interactions, which is what modern selfbot frameworks are built on. |
| 4 | Tier 3 + Tier 7 | Ergonomics and hygiene; polish once the surface has settled. |