# ChatterSyn

A Chatterino-style desktop chat client for **Twitch + YouTube live chat**, built
in Python with PySide6. One window, one input box — every message posts to
**both** chats.

```text
┌────────────────────────────────────────────────────┐
│ ● Twitch: redcyberdragon29 · ● YouTube: The Stream │
│ [merged chat feed — Twitch + YouTube interleaved]  │
│ [🛡] [Tw ✓] [Yt ✓]           Big Stream · 1,234…  │
│ ▸ message goes here…                    [😀] [Send]│
└────────────────────────────────────────────────────┘
```

## Features

- **Merged feed** — one interleaved chat view per tab (Twitch channel + YouTube
  stream pair), platform badge on every message, batch-rendered for 50+ msg/s.
- **Stream tab** — a tab that follows *you*: your own Twitch channel chat
  (joinable while you're offline) plus the live chat of your current YouTube
  stream, with a "waiting for your live stream…" placeholder that auto-connects
  within ~2 minutes of going live.
- **One input, dual send** — Enter posts to both chats (per-platform send
  toggles, dual character counters, rate-limit queues, exactly-once echo).
- **Full emotes** — Twitch + 7TV + BTTV + FFZ (animated WebP), per-provider
  toggles and priority.
- **Badges, usercards, replies** — Twitch badge sets, reply banners + `/reply`,
  hover usercards with recent messages and moderation actions.
- **Highlights & mentions** — regex/plain rules, colors, sounds, `/mentions`.
- **Commands** — `/me /reply /clear /announce /timeout /ban /delete /mod /vip
  /slow /subscribers /emoteonly /followers` plus your own `commands.json`.
- **Moderation mode** — per-tab shield; Twitch IRC commands, YouTube
  owner/mod-only delete with server ban + local hide.
- **Live awareness** — live dots, streamer bar (title · viewers · uptime),
  offline-since timestamps, live notifications.
- **The rest** — Ctrl+F search popup (with predicates), text logging, emote
  picker, autocomplete with thumbnails, dark/light themes, hotkeys, window
  geometry and tab restore.

## Install

Requires Python ≥ 3.11 and < 3.14 (3.13 recommended), and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> && cd <this repo>
uv tool install .          # puts `chattersyn` on your PATH
chattersyn
```

Try it with the built-in fake feed first — no credentials needed:

```sh
chattersyn --demo
```

For development:

```sh
uv sync
uv run chattersyn
```

## Packaged builds

Prebuilt bundles for macOS, Windows, and Linux are built by GitHub Actions on
every push and published on the [Releases](https://github.com/smitht06/red-chattersyn/releases)
page (tag `v*`), or as CI artifacts on the run itself. No Python install needed.

**macOS** (Apple Silicon, macOS 14.4+) — the recommended way is the Homebrew
tap, republished automatically on every release:

```sh
brew tap smitht06/chattersyn
brew install --cask chattersyn
```

New releases arrive with `brew upgrade --cask chattersyn`. Prefer a one-off
install? The release also carries a standalone
`chattersyn-<version>-macos-arm64.zip`: unzip and drag `chattersyn.app` to
Applications. The app is ad-hoc signed (not notarized) — brew installs
outside Quarantine, so tap installs open normally, but a manually downloaded
zip needs right-click → Open → Open on first launch to get past Gatekeeper.

macOS asks for your login password whenever the app reads its credentials
from the Keychain (the Twitch/YouTube tokens live there). CI builds are
ad-hoc signed, and an ad-hoc identity is the binary's content hash — every
new build looks like a new app, so the prompts repeat for each credential
item. Local builds via `scripts/build.py` prefer a self-signed code-signing
cert named `ChatterSyn*` (Keychain Access → Certificate Assistant → Code
Signing): a stable identity lets "Always Allow" stick permanently. Keep the
built app outside iCloud-synced folders — copy it to /Applications: the
iCloud file provider can strip a bundle's signature after the build, which
puts the app back on per-rebuild prompts.

**Windows** — `chattersyn-<version>-windows-x86_64.zip`: unzip anywhere and run
`chattersyn.exe` (portable, nothing installed). First launch may show a
SmartScreen prompt — More info → Run anyway.

**Linux** (Ubuntu/Debian/Mint x86_64) — add the APT repository once, and
`apt upgrade` keeps ChatterSyn updated from then on:

```sh
curl -fsSL https://chattersyn-apt.pages.dev/chattersyn.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/chattersyn.gpg
echo "deb [signed-by=/usr/share/keyrings/chattersyn.gpg] https://chattersyn-apt.pages.dev stable main" \
  | sudo tee /etc/apt/sources.list.d/chattersyn.list
sudo apt update
sudo apt install chattersyn
```

The repository is rebuilt and signed automatically on every release tag.
Prefer a one-off install instead? The release also carries a standalone
`chattersyn-<version>-linux-x86_64.deb`
(`sudo apt install ./chattersyn-<version>-linux-x86_64.deb`) and a portable
`chattersyn-<version>-linux-x86_64.tar.gz` (extract and run `./chattersyn`).

Installs to `/opt/chattersyn` with a `chattersyn` launcher and a menu entry;
remove with `sudo apt remove chattersyn`. Built on Ubuntu 22.04 (glibc 2.35),
so it runs on Linux Mint 21+ and similar. Credentials are stored via the
Secret Service (gnome-keyring / KWallet) when available; otherwise keyring
falls back to a local file (`~/.local/share/python_keyring/`).

## Releasing

Every push builds all three platforms; a pushed `v*` tag additionally runs a
`release` job that publishes a GitHub Release with the binaries attached
(macOS zip, Windows zip, Linux tar.gz + .deb), an `apt-repo` job that
publishes the Linux .deb into the APT repository, and a `homebrew` job that
publishes the macOS cask into the Homebrew tap (`smitht06/chattersyn`). The
GPG-signed `dists/stable` index is deployed to Cloudflare Pages; the .deb
itself stays a GitHub release asset (2 GB limit) and is reached via a Pages
redirect, since it exceeds GitHub's 100 MB git limit. To cut a release:

1. Bump `version` in `pyproject.toml`, then run `uv lock`. CI installs with
   `uv sync --locked`, and a stale lockfile fails every build leg — the
   release then contains only the auto-generated source archives, no binaries
   (this bit v0.2.0).
2. Commit and push, then tag the same commit and push the tag:

   ```sh
   git add pyproject.toml uv.lock && git commit -m "bump version to X.Y.Z"
   git push
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   Artifact names are stamped from the `pyproject.toml` version at the tagged
   commit, so tag only after the bump lands on main.
3. Watch the Actions tab: `build` (all three OSes, up to ~45 min), then
   `release` (attaches the artifacts), `apt-repo` (updates the APT
   repository, so Linux users get the new version on their next
   `apt update && apt upgrade`), and `homebrew` (pushes the new cask, so
   macOS users get it on their next `brew upgrade --cask chattersyn`).
   Edit the release notes on the release page afterwards.

If a release comes out missing binaries: check the tag's workflow run
(`gh run list`). Every OS failing at `uv sync --locked` means the lockfile
wasn't regenerated before tagging. Fix by either moving the tag to a fixed
commit and re-pushing (`git tag -f vX.Y.Z && git push -f origin vX.Y.Z`) or
uploading the artifacts from a passing run:

```sh
gh run download <passing-run-id> --dir /tmp/rel
gh release upload vX.Y.Z /tmp/rel/*/*
```

The APT repository only updates from a passing `apt-repo` run, so it stays on
the previous version until the tag is fixed and re-pushed — `apt upgrade`
simply won't offer the broken release, which is the intended behavior. The
Homebrew tap works the same way: the cask stays on the previous version until
a passing `homebrew` run pushes it, and `brew upgrade --cask chattersyn`
keeps reporting the old release.

## Credentials setup

Out of the box the app runs **read-only on Twitch** (anonymous chat) and needs
credentials for everything else. Everything is configured from
**Settings → Accounts** (Cmd/Ctrl+,).

### Twitch (sending, badge metadata, live status)

Sign-in is one click, Chatterino-style: **Settings → Accounts → Connect
Account…** opens the Twitch website in your browser, you authorize there, and
ChatterSyn picks up the result on localhost. No credentials to paste.

ChatterSyn uses its registered "ChatterSyn" Twitch application (an OAuth
**public client** — PKCE auth-code flow, no client secret). The app's Client
ID is built in; the redirect URIs `http://localhost:17821`,
`http://localhost:17822`, `http://localhost:17823` are pre-registered
(ChatterSyn binds the first free port). Your tokens live in the OS keychain
(macOS Keychain), never in `settings.json`.

**Advanced:** to use your own Twitch application instead, register one at
[dev.twitch.tv/console](https://dev.twitch.tv/console) (category "Chat Bot",
OAuth Redirect URLs exactly `http://localhost:17821` +
`http://localhost:17822` + `http://localhost:17823`) and put its Client ID in
`settings.json`:

```json
{"twitch": {"client_id": "your-client-id"}}
```

There is no UI field for this — it's a settings.json override only. Without
sign-in, chat reading + emotes still work anonymously (Twitch, 7TV, BTTV,
FFZ); sending, badge metadata, and live status need the account.

### YouTube (reading, sending, moderation)

Reading works with a plain **API key** (quota-free gRPC push). Sending needs a
Google **OAuth client** with `youtube.force-ssl` consent.

1. [console.cloud.google.com](https://console.cloud.google.com/) → create a
   project → **APIs & Services → Library** → enable **YouTube Data API v3**.
2. **Credentials → Create credentials → API key** — paste it into
   **Settings → Accounts → Data API key**. Chat reading works now.
3. For sending: **OAuth consent screen** → External, add yourself as a test
   user (no scopes needed on the consent screen itself).
4. **Credentials → Create credentials → OAuth client ID** → *Desktop app* →
   **Download JSON** (this is `client_secrets.json`).
5. In ChatterSyn, **Settings → Accounts → Authorize** → pick
   `client_secrets.json` → complete consent in your browser.

The **Stream tab**'s YouTube side always needs OAuth — "my live stream"
(`liveBroadcasts.list mine=true`) rejects API keys, so an API-key-only setup
shows the "YouTube auth required" placeholder until you authorize.

> **Note on "Testing" publishing status:** test-project refresh tokens expire
> after **7 days**. ChatterSyn detects this, tells you in chat, and
> **Settings → Accounts → Authorize** re-consents in one click. Publishing
> the app (or keeping it in Testing and re-consenting weekly) avoids this.

### Quota

- Chat **reading** uses YouTube's gRPC `liveChatMessages.streamList` push —
  quota-free; there is no polling cost.
- Display names for chat authors cost 1 unit per 50 new chatters (gRPC chat
  items carry only channel ids); names are cached for the session.
- The streamer-bar stats refresh polls `videos.list` every ~2 min while a
  stream is live (1 quota unit per call).
- Each **send** costs 20 units (10,000/day default). When the quota is
  exhausted, ChatterSyn shows a banner, pauses reading, and blocks the
  YouTube send toggle until the midnight-PT reset.

## Settings

**Settings → Accounts** and the other pages — General (theme, UI scale,
scrollback, timestamps, tab restore), Commands, Highlights, Moderation,
Notifications, Hotkeys, About — persist to `settings.json`
(macOS: `~/Library/Preferences/ChatterSyn/chattersyn/`). Secrets live in the
OS keychain. Unhandled errors append to `crash.log` in the same directory.

## Development

```sh
uv sync
uv run ruff check src/ tests/   # lint
uv run pytest -q                 # 675 tests, no credentials needed
```

Architecture: one qasync loop on the main thread; Twitch over
IRC-over-WebSocket plus Helix REST; YouTube over gRPC streamList (read) plus
REST (send); Qt model/delegate chat rendering. See the plan in
`~/.claude/plans/i-want-an-app-idempotent-pebble.md` for the full design.
