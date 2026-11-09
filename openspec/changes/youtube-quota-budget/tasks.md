# youtube-quota-budget — plan

## Goal

Keep ChatterSyn's YouTube polling inside the shared daily quota, let a user's own
API key pay for their public reads, and let tabs recover on their own after the
quota runs out.

## Architecture

A new pure function (`schedule.py`) decides when each watch checks next. The
resolver remembers each channel's uploads playlist so re-checks are cheap.
`YouTubeApi` picks the credential per call (the user's key or the sign-in) and a
per-pool `QuotaTracker` (`quota.py`) follows Pacific time. The watch loop sleeps
on a per-watch wake event, so visibility and quota changes reschedule it at once.
MainWindow reports which tab is on screen and renders the quota-wait state.

**Spec:** openspec/changes/youtube-quota-budget/specs/youtube-live-detection/spec.md and openspec/changes/youtube-quota-budget/specs/youtube-quota/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- No new runtime dependency: Pacific time comes from `PySide6.QtCore.QTimeZone(b"America/Los_Angeles")` (A1; checked to work, 23:00 PST → next midnight 08:00Z).
- Tests use no credentials and no network: respx for REST, fakes for gRPC, an injected clock for time.
- User-facing texts exactly as the spec: "YouTube quota used up · resumes at HH:MM", "YouTube quota used up — resumes at HH:MM", "API key not working — using your Google sign-in", "API key not working — YouTube reading paused", "not found — check the URL/handle", "chat unavailable".
- Behaviour the spec does not name stays as it is: video tabs stop after ENDED/NOT_FOUND, "live but no chat id yet" re-resolves after 30 s, chat reading, stats refresh and name batching keep their timing (only their credential changes).
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. An idle on-screen channel tab over 24 simulated hours must stay at or under 600 units (D4 budget) → task 3.3 pins `test_idle_channel_tab_day_budget`.
2. A LIVE event (the key keeps reading) must not re-enable the YouTube send toggle while the shared pool is used up → task 4.2 pins it in the YQB-57 test.
3. gRPC RESOURCE_EXHAUSTED without a quota reason means "reconnecting too fast" and must stay a transient backoff, not a quota wait → task 2.2 pins it.
4. `asyncio.sleep` runs on the monotonic clock, which stops during system suspend: a sleep until the resume time or a 15-min check could fire hours late → sleeps are chunked to at most 60 s against the wall clock; task 3.3 pins it with a clock that jumps forward.
5. Rapid tab switching must not stack checks → task 3.3 pins `test_visibility_flapping_one_check_per_minute`.

## 1. Quota bookkeeping

- [x] 1.1 Per-pool quota tracker on Pacific time — verify: uv run pytest tests/test_youtube_quota.py tests/test_youtube_api.py -q passes

Covers: YQB-31, YQB-32, YQB-33, YQB-34, YQB-50, YQB-51 · Test: scenario-first

Files: create src/chattersyn/services/youtube/quota.py; modify src/chattersyn/services/youtube/api.py; test tests/test_youtube_quota.py

Interfaces:
- Produces in quota.py: `class Pool(enum.Enum)` with `SHARED = "shared"`, `KEY = "key"`.
- Produces: `next_pacific_midnight(now: dt.datetime) -> dt.datetime` (aware UTC in and out).
- Produces: `QuotaTracker(*, now: Callable[[], dt.datetime] = <aware UTC now>, rng: random.Random | None = None)` with `charge(units: int, pool: Pool = Pool.SHARED)`, `used_in(pool: Pool) -> int`, property `used` (= `used_in(Pool.SHARED)`, kept for existing callers), `exhausted(pool: Pool = Pool.SHARED) -> bool`, `mark_exhausted(pool: Pool) -> dt.datetime` (the resume time), `resume_at(pool: Pool) -> dt.datetime | None`, attribute `on_exhausted: Callable[[Pool, dt.datetime], None] | None`.
- A pool's count restarts once `next_pacific_midnight(<time of its first charge that day>)` has passed. `mark_exhausted` draws `next_pacific_midnight(now) + uniform(60, 600) s` once per pool per reset and returns the same value until then; `exhausted()` turns False at that time.
- `api.py` re-exports `QuotaTracker` (existing imports keep working); `QUOTA["ban"] = 200`.

1. Write `test_ban_and_timeout_cost_200` (respx: `live_chat_ban(...)`, `live_chat_ban(..., duration=300)` and `live_chat_unban(...)` each add 200 to `quota.used`), `test_local_midnight_is_not_reset` (charge at 2026-11-10T07:00Z = 23:00 PST; at 07:59Z the count is unchanged), `test_pacific_midnight_resets` (at 08:00Z `used == 0`), `test_resume_between_0001_and_0010_pt` (`mark_exhausted` at 07:00Z returns a time in [08:01Z, 08:10Z]), `test_one_resume_time_per_pool` (two `mark_exhausted(SHARED)` calls return the same time; KEY draws its own), `test_key_reads_count_in_key_pool` (`charge(5, Pool.KEY)` → `used_in(KEY) == 5`, `used == 0`), `test_exhausted_clears_at_resume_time`. Run them. Expected: FAIL (ImportError).
2. Implement quota.py to the interface; `next_pacific_midnight` converts with QDateTime: UTC → `toTimeZone(QTimeZone(b"America/Los_Angeles"))` → `date().addDays(1)` at 00:00 in that zone → `toUTC().toPython()` with tzinfo UTC.
3. In api.py import `Pool`/`QuotaTracker` from quota.py, delete the old class, set `QUOTA["ban"] = 200`, and fix the module docstring's table (bans 200; search.list 1 unit in its own 100-calls/day bucket).
4. Run the verify command. Expected: PASS.

## 2. Credentials

- [x] 2.1 Per-call credential choice in YouTubeApi — verify: uv run pytest tests/test_youtube_api.py tests/test_youtube_credentials.py tests/test_youtube_names.py -q passes

Covers: YQB-21, YQB-23, YQB-25, YQB-26, YQB-27, YQB-43, YQB-44, YQB-45 · Test: reproduce-first

Files: modify src/chattersyn/services/youtube/api.py, src/chattersyn/services/youtube/service.py, src/chattersyn/services/youtube/names.py; test tests/test_youtube_credentials.py, tests/test_youtube_api.py

Interfaces:
- Consumes: `Pool`, `QuotaTracker` (1.1).
- Produces: `YouTubeApi(get_token, quota=None, *, get_key: Callable[[], str | None] | None = None, client=None)`. `get_key` is now synchronous. The service already passes a plain lambda, and awaiting it crashed every key-only read with TypeError (probe 2026-11-09).
- Public reads prefer a working key: `videos`, `videos_batch`, `channel_by_handle`, `channel_by_id`, `channels_names`, `playlist_items`, `search_live`. `videos(video_id, *, signin: bool = False)` lets the Stream tab force the sign-in (A22).
- Sign-in only: `channels_mine`, `live_broadcasts_mine`, `live_chat_list`, `live_chat_insert`, `live_chat_ban`, `live_chat_unban`.
- Produces: `YouTubeApi.key_problem: str` ("" when fine), `YouTubeApi.reset_key_problem() -> None`, `async YouTubeApi.read_pool(*, signin: bool = False) -> Pool | None`.
- Produces: `KEY_INVALID_REASONS = {"keyInvalid", "keyExpired", "accessNotConfigured", "ipRefererBlocked", "API_KEY_INVALID", "API_KEY_EXPIRED", "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED"}`, matched against `error.errors[].reason` and `error.details[].reason`. Before relying on it, check the set against Google's error reference (cloud.google.com/apis/design/errors, API-key ErrorInfo reasons) and cite the source in a comment (A14).
- A call on an exhausted pool raises `YoutubeApiError(403, "quotaExceeded", ...)` without sending a request (A29). Each call charges the pool it actually used.
- Produces: `YouTubeService.api_key_problem() -> str`, one of "", "API key not working — using your Google sign-in" (signed in), or "API key not working — YouTube reading paused".

1. Write `test_key_only_read_uses_key` through the real wiring (`YouTubeService(Settings({}), store)` with `youtube_api_key` set, no sign-in; respx on `/videos`; the request carries `key=` and no Authorization). Run it. Expected: FAIL (TypeError: object str can't be used in 'await' expression).
2. Write the scenario tests in tests/test_youtube_credentials.py:
   - YQB-21: token plus key; `channel_by_handle` carries `key=` and no Authorization.
   - YQB-23: token plus key; `live_chat_insert` carries Bearer and no `key=`.
   - YQB-25: token, no key; reads carry Bearer.
   - YQB-26: a key call answered 400 `keyInvalid` is repeated once with Bearer, `key_problem != ""`, and later public reads go straight to Bearer.
   - YQB-43: no token plus an invalid key raises an error with `is_unauthenticated`, and `key_problem` is set.
   - YQB-44: after `reset_key_problem()`, reads use the key again.
   - YQB-45: a key call answered 403 `rateLimitExceeded` raises a retryable error and `key_problem` stays "".
   - YQB-27: a key call answered 403 `quotaExceeded` sets `exhausted(Pool.KEY)` with no Bearer retry; the next public read raises `quotaExceeded` with no request sent.

   Run them. Expected: FAIL.
3. Implement in api.py: `_auth(public: bool, signin: bool) -> tuple[dict, dict, Pool]`; `_parse(response, path, pool)` marks the pool exhausted on `quotaExceeded`, and on a KEY call with a reason in `KEY_INVALID_REASONS` sets `key_problem`. When a token exists, `_get` repeats that public read once with the sign-in. Without a token it raises `YoutubeApiError(401, "unauthenticated", "API key not working")`.
4. In service.py: `set_api_key()` also calls `self.api.reset_key_problem()`; add `api_key_problem()`.
5. In names.py: replace `self._api.quota.exhausted()` with "the pool from `await self._api.read_pool()` is not exhausted".
6. Change the async `get_key` fake in tests/test_youtube_api.py (`test_api_key_fallback_when_no_token`) to a plain function. Run the verify command. Expected: PASS.

- [x] 2.2 Streaming reader credential and error classes — verify: uv run pytest tests/test_youtube_reader.py tests/test_youtube_service.py -q passes

Covers: YQB-22, YQB-24, YQB-42, YQB-55 · Test: scenario-first

Files: modify src/chattersyn/services/youtube/reader.py, src/chattersyn/services/youtube/service.py; test tests/test_youtube_reader.py, tests/test_youtube_service.py

Interfaces:
- Consumes: `read_pool`, `key_problem`, `KEY_INVALID_REASONS` (2.1); `Pool`, `mark_exhausted` (1.1).
- Produces in reader.py: `class ReadRefused(Exception)` with `key_invalid: bool` and `reason: str`; `class ReadQuotaExceeded(Exception)`; `error_reason(exc: grpc.aio.AioRpcError) -> str` (the ErrorInfo reason from the `grpc-status-details-bin` trailer via `google.rpc.status_pb2`/`error_details_pb2`, both installed; "" when absent).
- `_translate` becomes:
  - UNAUTHENTICATED, PERMISSION_DENIED or INVALID_ARGUMENT with a reason in `KEY_INVALID_REASONS` → `ReadRefused(key_invalid=True)`.
  - Any other PERMISSION_DENIED → `ReadRefused(key_invalid=False)`.
  - RESOURCE_EXHAUSTED whose ErrorInfo reason or domain names quota → `ReadQuotaExceeded`.
  - Any other RESOURCE_EXHAUSTED stays transient.
  - FAILED_PRECONDITION and NOT_FOUND are unchanged.
- Produces in service.py: `_grpc_metadata(watch) -> tuple[tuple[tuple[str, str], ...], Pool]`, which picks Bearer/SHARED for a `mine:` watch or when `watch.use_signin` is set; otherwise key/KEY when the key works; otherwise Bearer/SHARED. Also produces the field `_Watch.use_signin: bool = False`.

1. Write tests in test_youtube_reader.py: `error_reason` reads an ErrorInfo from a fake `AioRpcError` trailer; each `_translate` class, including RESOURCE_EXHAUSTED without a quota reason staying transient (Review Focus 3).
2. Write tests in test_youtube_service.py. A FakeGrpcReader records the metadata it is given.
   - YQB-22: token plus key, video tab; the metadata is `x-goog-api-key`.
   - YQB-24: token plus key, `mine:` watch; the metadata is `authorization` Bearer, and the stats refresh calls `videos(..., signin=True)`.
   - YQB-42: token plus key; the first read raises `ReadRefused(key_invalid=False)`, the next read uses Bearer, and `api.key_problem` stays "".
   - YQB-55: no token plus key; `ReadRefused(key_invalid=False)` leads to `WatchState.UNAVAILABLE`, and a second watch keeps the key metadata.

   Run them. Expected: FAIL.
3. Implement:
   - In the read loop, charge `QUOTA["list"]` to the pool `_grpc_metadata` returned.
   - `ReadRefused(key_invalid=True)` sets `api.key_problem` and retries at once with the sign-in, or emits AUTH_REQUIRED when not signed in.
   - `ReadRefused(key_invalid=False)` sets `use_signin` when signed in, otherwise returns "unavailable".
   - `ReadQuotaExceeded` calls `self.quota.mark_exhausted(pool)`.
4. Run the verify command. Expected: PASS.

## 3. Live detection

- [x] 3.1 Check schedule — verify: uv run pytest tests/test_youtube_schedule.py -q passes

Covers: YQB-6, YQB-7, YQB-8, YQB-9, YQB-10, YQB-11, YQB-12, YQB-13, YQB-14, YQB-15, YQB-16, YQB-19, YQB-36, YQB-37, YQB-38, YQB-40, YQB-52, YQB-53 · Test: scenario-first

Files: create src/chattersyn/services/youtube/schedule.py; test tests/test_youtube_schedule.py

Interfaces:
- Produces: `class WatchKind(enum.Enum)` with `CHANNEL`, `VIDEO`, `MINE`.
- Produces: `next_check_at(kind: WatchKind, state: ResolveState, *, last_check: dt.datetime, offline_since: dt.datetime, scheduled_starts: Sequence[dt.datetime], on_screen: bool) -> dt.datetime`. It is pure: no I/O and no clock reads.
- Rules, with t = `last_check`:
  - **MINE:** t + 120 s, but t + 30 s inside [s − 5 min, s + 15 min] of its earliest start s whose grace has not ended. Clip to s − 5 min when that mark falls between t and t + 120 s. `on_screen` is ignored.
  - **CHANNEL with NOT_FOUND:** t + 15 min, whether on screen or not.
  - **CHANNEL or VIDEO not on screen:** t + 15 min, with no 5-min mark (YQB-16).
  - **Relevant start:** s = the earliest start with s + 15 min > t. Before s − 5 min: min(t + 15 min, s − 5 min). From s − 5 min to s + 15 min: t + 30 s.
  - **Otherwise offline:** since = max(`offline_since`, the latest s + 15 min that is ≤ t); t + 2 min when t − since < 30 min, else t + 5 min.

1. Write one test per covered scenario with the spec's values. For example: YQB-53, t = s − 12 min → s − 5 min. YQB-13, t = s + 16 min → t + 2 min, and t = s + 15 min + 31 min → t + 5 min. YQB-52, starts 2 days ago and in 3 h → the 3-h start rules. Run them. Expected: FAIL (ImportError).
2. Implement `next_check_at` to these rules.
3. Run the verify command. Expected: PASS.

- [x] 3.2 Resolver: deep scan once, cheap re-checks — verify: uv run pytest tests/test_youtube_resolver.py -q passes

Covers: YQB-1, YQB-2, YQB-3, YQB-4, YQB-5, YQB-35 · Test: scenario-first

Files: modify src/chattersyn/services/youtube/resolver.py; test tests/test_youtube_resolver.py

Interfaces:
- Consumes: the public reads of `YouTubeApi` (2.1).
- Produces: `LiveChatResolver.resolve(target: str, *, deep: bool = False) -> Resolution`; `LiveChatResolver.forget(target: str) -> None`; `Resolution.scheduled_starts: list[dt.datetime]`. That list holds the start times of every upcoming video on the checked page, or of the own scheduled broadcast or the watched video.
- Per channel target the resolver remembers the channel id, the uploads playlist id, the ids on the newest page, and the videos from the last `videos_batch`.

1. Write tests with a counting fake API, as in the scratch probe, where each method charges 1 unit:
   - YQB-1: deep, the live video is on page 3; it is found and the state is LIVE.
   - YQB-2: deep, the live video is on page 1; exactly one `playlist_items` call.
   - YQB-3: a re-check of a 1,000-upload offline channel after a deep resolve costs ≤ 2 units and calls neither `channel_by_handle` nor `channel_by_id`.
   - YQB-4: same page ids, none upcoming or live; 1 unit and no `videos_batch`.
   - YQB-35: same ids, one upcoming without a start time; `videos_batch` is called and the now-live video gives LIVE.
   - YQB-5: a new live id on the newest page gives LIVE.
   - A re-check whose `playlist_items` answers 404 forgets the channel and returns NOT_FOUND.

   Run them. Expected: FAIL.
2. Implement. A deep or uncached resolve takes the existing full path and fills the cache. A re-check reads one `playlist_items` page, then `videos_batch` unless the YQB-4 rule skips it, and fills `scheduled_starts` from `liveStreamingDetails.scheduledStartTime` of the upcoming videos.
3. In `_from_mine` and `_from_video`, fill `scheduled_starts` from the single scheduled start.
4. Run the verify command. Expected: PASS.

- [x] 3.3 Watch loop on the schedule, visibility API — verify: uv run pytest tests/test_youtube_service.py -q passes

Covers: YQB-17, YQB-18, YQB-20, YQB-39, YQB-40, YQB-41, YQB-54 · Test: scenario-first

Files: modify src/chattersyn/services/youtube/service.py; test tests/test_youtube_service.py

Interfaces:
- Consumes: `next_check_at`, `WatchKind` (3.1); `resolve(deep=...)`, `forget`, `scheduled_starts` (3.2).
- Produces: `YouTubeService.set_on_screen(watch_key: str, on_screen: bool) -> None`; `YouTubeService(settings, secrets, *, now: Callable[[], dt.datetime] = <aware UTC now>)`.
- `_Watch` gains `kind: WatchKind`, `on_screen: bool = True`, `last_check: dt.datetime | None`, `offline_since: dt.datetime`, `deep: bool = True` and `wake: asyncio.Event`.
- `RE_RESOLVE_INTERVAL` and `MINE_RE_RESOLVE_INTERVAL` are no longer used for non-live states. `UPCOMING_RE_RESOLVE` stays only for "live but no chat id yet".

1. Write tests with a fake clock and a fake resolver; the sleep helper is patched to record due times:
   - YQB-54: an offline tab last checked 1 min ago gets `set_on_screen(False)`; the next due time is last + 15 min.
   - YQB-17: a hidden tab last checked 5 min ago gets `set_on_screen(True)`; it resolves at once.
   - YQB-39: last checked 20 s ago, `set_on_screen(True)`; no resolve, and the due time is last + 2 min.
   - YQB-18: tabs hidden by minimizing, then restored; the on-screen tab resolves at once.
   - YQB-40: a channel NOT_FOUND gets a due time of last + 15 min and keeps watching.
   - YQB-41: NOT_FOUND, then found and live; that resolve is deep and reading starts.
   - YQB-20: in a hidden live tab, messages keep arriving and the stats loop keeps running.
   - Review Focus 1: `test_idle_channel_tab_day_budget`, with the counting fake API from 3.2, 24 simulated hours on screen and OFFLINE each time; ≤ 600 units.
   - Review Focus 4: the clock jumps 3 h during one sleep, and the check runs within one 60-s chunk.
   - Review Focus 5: `test_visibility_flapping_one_check_per_minute`; 20 on/off toggles within 60 s give at most 1 extra resolve.

   Run them. Expected: FAIL.
2. Implement the loop:
   - After each check, set `last_check`; while LIVE is seen, also set `offline_since = now`.
   - Sleep until `next_check_at(...)` or until `watch.wake` is set, in chunks of at most 60 s against `now()`; on wake, recompute.
   - `set_on_screen` stores the flag, asks for an immediate check when the tab comes on screen with `last_check` older than 60 s, and sets `wake`.
3. A channel NOT_FOUND keeps watching: call `forget(target)`, and the next resolve is deep. Leave the live/no-chat-id path unchanged.
4. Rewrite the `fast` fixture and `test_mine_offline_uses_mine_interval` for the schedule. Run the verify command. Expected: PASS.

- [x] 3.4 Report tab visibility from the window — verify: uv run pytest tests/test_main_window_visibility.py -q passes

Covers: YQB-15, YQB-17, YQB-18 · Test: scenario-first

Files: modify src/chattersyn/ui/main_window.py; test tests/test_main_window_visibility.py

Interfaces:
- Consumes: `YouTubeService.set_on_screen` (3.3).
- Produces: `MainWindow._report_visibility() -> None`, called from `self._notebook.currentChanged`, from `changeEvent` on `QEvent.Type.WindowStateChange`, and after `add_tab`.

1. Write tests with a fake YouTube service that records `set_on_screen` calls, following the fake-service pattern in tests/test_main_window_streams.py:
   - YQB-17: selecting tab B gives `(A, False)` and `(B, True)`.
   - YQB-15: minimizing gives False for every tab.
   - YQB-18: restoring gives True for the selected tab.

   The offscreen platform does not really minimize, so the tests patch `window.isMinimized`. Run them. Expected: FAIL.
2. Implement: a tab is on screen when it is `notebook.currentWidget()` and the window is not minimized (A9). Report it for every tab with a YouTube target.
3. Run the verify command. Expected: PASS.

## 4. Running out and resuming

- [x] 4.1 Watches wait per pool and resume after the reset — verify: uv run pytest tests/test_youtube_service.py -q passes

Covers: YQB-27, YQB-28, YQB-29, YQB-30, YQB-46, YQB-47, YQB-56 · Test: scenario-first

Files: modify src/chattersyn/services/youtube/service.py; test tests/test_youtube_service.py

Interfaces:
- Consumes: `mark_exhausted`, `resume_at`, `exhausted`, `on_exhausted` (1.1); `read_pool` (2.1); `ReadQuotaExceeded` (2.2); `wake` and the chunked sleep (3.3).
- Produces: `YoutubeState.resume_at: dt.datetime | None`, set with `WatchState.QUOTA_EXHAUSTED`.
- Produces: `@dataclass QuotaPoolState(pool: Pool, resume_at: dt.datetime | None)` on `bridge.event`, emitted when a pool runs out and again with `resume_at=None` when it resumes. A send's quota error reaches it on its own, because `_parse` marks the pool on every call.

1. Replace `test_resolve_quota_exhausted_is_terminal` with these tests:
   - YQB-28: resolve raises `quotaExceeded`; QUOTA_EXHAUSTED is emitted with `resume_at`, and the task stays alive.
   - YQB-30: a reading video tab whose reader raises `ReadQuotaExceeded` emits QUOTA_EXHAUSTED and stays alive.
   - YQB-29: the fake clock passes `resume_at`; the watch resolves with `deep=True` and reads when LIVE.
   - YQB-47: two watches on SHARED; when one hits the quota, the other emits QUOTA_EXHAUSTED with the same `resume_at` without calling `resolve`.
   - YQB-56: `watch()` while SHARED is exhausted emits QUOTA_EXHAUSTED and never calls `resolve`.
   - YQB-27: with the KEY pool exhausted, the key-reading watch waits and `svc.send` still reaches the sender.
   - YQB-46: a send that hits the quota emits `QuotaPoolState(SHARED, t)`, and a watch reading with the key keeps reading.

   Run them. Expected: FAIL.
2. Implement:
   - `quota.on_exhausted` emits `QuotaPoolState` and sets `wake` on every watch.
   - Before each check, a watch whose read pool is exhausted emits QUOTA_EXHAUSTED and sleeps until `resume_at` (chunked), then sets `deep = True` and continues.
   - At resume, emit `QuotaPoolState(pool, None)`.
   - The Stream tab's reads use SHARED (A22).
3. Run the verify command. Expected: PASS.

- [ ] 4.2 Quota wait in the window — verify: uv run pytest tests/test_main_window_streams.py tests/test_main_window_quota.py -q passes

Covers: YQB-28, YQB-48, YQB-49, YQB-57, YQB-58 · Test: scenario-first

Files: modify src/chattersyn/ui/main_window.py; test tests/test_main_window_quota.py, tests/test_main_window_streams.py

Interfaces:
- Consumes: `YoutubeState.resume_at`, `QuotaPoolState` (4.1).
- Produces: `_format_resume(resume_at: dt.datetime) -> str` (local time "HH:MM"); `MainWindow._shared_pool_resume_at: dt.datetime | None`.

1. Write tests:
   - YQB-28: QUOTA_EXHAUSTED with `resume_at` sets the status to "YouTube quota used up · resumes at HH:MM" in local time. `QuotaPoolState(SHARED, t)` leaves the send toggle unchecked and disabled, with the tooltip "YouTube quota used up — resumes at HH:MM".
   - YQB-57: `QuotaPoolState(SHARED, t)` followed by a LIVE event; the status shows the live text and the toggle stays disabled (Review Focus 2).
   - YQB-48: a dual send while blocked sends to Twitch only.
   - YQB-49: a send in a YouTube-only tab while blocked adds the system row "YouTube quota used up — resumes at HH:MM" and sends nothing.
   - YQB-58: the toggle was on before; `QuotaPoolState(SHARED, None)` leaves it enabled and on.

   Update the QUOTA_EXHAUSTED expectation in tests/test_main_window_streams.py. Run them. Expected: FAIL.
2. Implement:
   - QUOTA_EXHAUSTED only sets the status text.
   - `QuotaPoolState` for SHARED blocks or unblocks every tab's toggle; the header already restores the earlier state (A24).
   - LIVE re-enables the toggle only while `_shared_pool_resume_at is None`.
   - `_on_send`: when YouTube is wanted, quota-blocked and Twitch is off, add the refusal row.
3. Run the verify command. Expected: PASS.

- [ ] 4.3 API key note in Settings — verify: uv run pytest tests/test_settings_dialog.py -q passes

Covers: YQB-26, YQB-43, YQB-44 · Test: scenario-first

Files: modify src/chattersyn/ui/settings_dialog.py, src/chattersyn/ui/main_window.py; test tests/test_settings_dialog.py

Interfaces:
- Consumes: `YouTubeService.api_key_problem()` (2.1).
- Produces: `SettingsDialog(..., youtube_api_key_note: str = "")`, shown as a QLabel under the "Data API key:" field and hidden when "".

1. Write tests:
   - YQB-26: the note "API key not working — using your Google sign-in" is visible.
   - YQB-43: the note "API key not working — YouTube reading paused" is visible.
   - YQB-44: an empty note leaves the label hidden.

   Run them. Expected: FAIL.
2. Implement the label. MainWindow passes `self._youtube.api_key_problem()` when it opens Settings ("" in demo mode).
3. Run the verify command. Expected: PASS.

## 5. Quota-extension draft and final check

- [ ] 5.1 Draft the quota-extension answers — verify: the file lists every YouTube method the app calls with its unit cost, when it runs, and a per-user daily cost

Covers: — · Test: none: prose the user submits to Google (D9, A19)

Files: create openspec/changes/youtube-quota-budget/quota-extension.md

1. List every YouTube API method the app calls (api.py and reader.py), its unit cost from Google's table, and when and how often it runs under the new schedule.
2. Compute the per-user daily cost from the schedule.py constants: a typical day (one Stream tab and two channel tabs, 4 h open, one 2-h stream) and the worst case (one idle on-screen channel tab for 24 h, ≤ 600 units).
3. Mark what only the user can fill in (project number, contact details, screencast). No secrets in the file.

- [ ] 5.2 Full verification — verify: uv run ruff check src/ tests/ and uv run pytest -q both pass, and the quota probe shows ≤ 600 units/day

Covers: — · Test: none: runs the existing suite and a measurement

Files: none (read-only checks; the probe lives in the session scratchpad)

1. Run `uv run ruff check src/ tests/`. Expected: All checks passed!
2. Run `uv run pytest -q`. Expected: every test passes (913 before this change, plus the new ones).
3. Re-run the scratch quota probe with the real resolver and `next_check_at`: an offline channel with 1,000 uploads, on screen, over 24 simulated hours. Expected: ≤ 600 units/day (30,240 before).
4. Run `uv run chattersyn --help`. Expected: exit code 0.
