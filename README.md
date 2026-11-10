# ChatterSyn

A Chatterino-style desktop chat client for **Twitch + YouTube live chat**, built
in Python with PySide6. One window, one input box — every message posts to
**both** chats.

```text
┌────────────────────────────────────────────────────┐
│ ● Twitch: redcyberdragon29 · ● YouTube: The Stream │
│ [merged chat feed — Twitch + YouTube interleaved]  │
│ [Tw ✓] [Yt ✓]                Big Stream · 1,234…  │
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
  toggles and priority, cached on disk between runs. YouTube chat is plain
  text, so emotes go to Twitch only: they're removed from the YouTube copy of
  your message, and a message that's only emotes isn't sent to YouTube at
  all (optionally send matching emoji instead, KEKW → 🤣, in Settings →
  Emotes). ChatterSyn draws emote names in YouTube messages as images.
- **Badges, usercards, replies** — Twitch badge sets, reply banners + `/reply`,
  usercards (click a name) with recent messages and moderation actions.
- **Highlights & mentions** — regex/plain rules, colors, sounds, `/mentions`.
- **Commands** — `/me /reply /clear /announce /timeout /ban /delete /mod /vip
  /slow /subscribers /emoteonly /followers` plus your own `commands.json`.
- **Moderation** — click a name to open its user card: Ban/Unban, a timeout
  grid and your own actions from Settings → Moderation (Twitch, via Helix),
  or Ban/Unban/Timeout/Delete on YouTube (owner/mod only; Delete hides the
  user's messages locally). Shown only where you have mod rights.
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
brew trust smitht06/chattersyn   # one-time: brew requires trusting third-party taps
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
Secret Service (gnome-keyring / KWallet) when available; otherwise ChatterSyn
warns you and keeps them in `secrets.json` next to its settings, readable only
by you.

**Linux, any distribution (Flatpak)** — download
`chattersyn-<version>-linux-x86_64.flatpak` from the release and install it:

```sh
flatpak install --user chattersyn-<version>-linux-x86_64.flatpak
flatpak run net.redcyberdragon.ChatterSyn
```

It uses the KDE runtime from Flathub, which `flatpak install` fetches for you
(if Flathub isn't set up yet: `flatpak remote-add --if-not-exists --user flathub
https://dl.flathub.org/repo/flathub.flatpakrepo`). To update, install the next
release's file the same way. The Flatpak keeps its settings in
`~/.var/app/net.redcyberdragon.ChatterSyn`, separate from the `.deb`.
ChatterSyn isn't on Flathub: Flathub doesn't accept AI-written packaging.

## Releasing

Every push builds all three platforms; a pushed `v*` tag additionally runs a
`release` job that publishes a GitHub Release with the binaries attached
(macOS zip, Windows zip, Linux tar.gz + .deb), an `apt-repo` job that
publishes the Linux .deb into the APT repository, and a `homebrew` job that
publishes the macOS cask into the Homebrew tap (`smitht06/chattersyn`). The
GPG-signed `dists/stable` index is deployed to Cloudflare Pages; the .deb
itself stays a GitHub release asset (2 GB limit) and is reached via a Pages
redirect, since it exceeds GitHub's 100 MB git limit.

The easy way, in Claude Code, is `/release` (skill in
`.claude/skills/release/`). It defaults to the last tag + 0.0.1 and asks you
about the version, the files and dependency updates as it goes. It writes the
release notes (via `/release-notes`), bumps the version, gets your final
confirmation, runs `scripts/release.sh`, then tracks CI and checks that the
GitHub release, APT repo and Homebrew cask all updated
(`scripts/release_tools.py watch` / `verify`, no `gh` needed).

Release candidates: `/prerelease` (skill in `.claude/skills/prerelease/`)
cuts `vX.Y.ZrcN` (default: the next patch, `rc1`, `rc2`, …). CI marks it as a
GitHub **pre-release**, so "Latest" stays on the stable release. APT and
Homebrew users get it on their next upgrade. The `.deb` is versioned
`X.Y.Z~rcN` so the final `vX.Y.Z` (cut with `/release`) upgrades them. The
README's "Latest release" section stays on the latest stable release.

To cut a release by hand:

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
   Then write the release notes: in Claude Code, `/release-notes` (skill in
   `.claude/skills/release-notes/`) drafts them from the diff since the
   previous tag and refreshes the "Latest release" section at the bottom of
   this README; paste the same notes into the release page.

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

Out of the box the app reads **Twitch chat anonymously**. Sending, YouTube
and moderation need you to sign in, one click each in **Settings → Accounts**
(Cmd/Ctrl+,).

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

Sign-in is one click, like Twitch: **Settings → Accounts → YouTube → Connect
Account…** opens Google in your browser, and you approve ChatterSyn's access
to your YouTube account there. ChatterSyn uses its own registered Google app (a
desktop OAuth client whose ID is built in, like the Twitch one); one
permission, `youtube.force-ssl`, covers reading, sending and moderating. Your
tokens live in the OS keychain. The app's Google client secret isn't in the
app: sign-in goes through ChatterSyn's sign-in relay on
`chattersyn.redcyberdragon.net` (`site/functions/`), which adds it, passes the
request to Google and stores nothing.

> **While Google reviews ChatterSyn's app**, Google shows a "Google hasn't
> verified this app" warning: click **Advanced**, then **Go to ChatterSyn**.
> Until the review is done, at most 100 people can sign in through the app.
> Both limits go away once Google approves it.

The **Stream tab**'s YouTube side needs the sign-in: "my live stream"
(`liveBroadcasts.list mine=true`) rejects API keys.

**Advanced** (Settings → Accounts → *YouTube: advanced*), with your own Google
Cloud project ([console.cloud.google.com](https://console.cloud.google.com/),
enable **YouTube Data API v3**):

- **Read without signing in:** create an **API key** and paste it into
  **Data API key**. Reading works; sending and moderation still need a sign-in.
- **Your own Google app:** set up the OAuth consent screen (External, add
  yourself as a test user), create an OAuth client ID of type *Desktop app*,
  **Download JSON** (`client_secrets.json`) and pick it with **Use Own Google
  Client…**. It then replaces ChatterSyn's app, with your project's quota.

### Quota

- Chat **reading** uses YouTube's gRPC `liveChatMessages.streamList` push —
  quota-free; there is no polling cost.
- Display names for chat authors cost 1 unit per 50 new chatters (gRPC chat
  items carry only channel ids); names are cached for the session.
- The streamer-bar stats refresh polls `videos.list` every ~2 min while a
  stream is live (1 quota unit per call).
- Each **send** costs 20 units. A Google project gets 10,000 units a day, and
  everyone signed in through ChatterSyn's app shares that budget until Google
  raises it (your own app from *Advanced* has its own). When the quota is
  exhausted, ChatterSyn shows a banner, pauses reading, and blocks the
  YouTube send toggle until the midnight-PT reset.

## Settings

**Settings → Accounts** and the other pages — General (theme, UI scale,
scrollback, timestamps, tab restore, chat logs), Commands, Highlights,
Moderation, Notifications, Hotkeys, About — persist to `settings.json`
(macOS: `~/Library/Preferences/ChatterSyn/chattersyn/`). Secrets live in the
OS keychain. Unhandled errors append to `crash.log` in the same directory.

Chat logs (on by default; Settings → General) are one text file per channel
per day in `logs/` next to `settings.json`. YouTube chat logs are deleted after
30 days, as YouTube's API policies require; Twitch logs are kept. The app's own
diagnostic log, with tokens and keys redacted, is under Settings → About →
Open Diagnostic Log Folder.

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

<!-- release-notes:start -->
## Latest release: v0.10.6 (2026-11-10)

### Action needed
- **Reconnect Twitch once.** The moderation and broadcaster commands below need new Twitch permissions. Reconnect your account in Settings → Accounts; until you do, those commands tell you to reconnect instead of running.

### New
- **Mods and VIPs list.** /mods and /vips open a list you can filter by name; clicking a name opens that person's user card. Twitch only shows these lists to the broadcaster.
- **Delete message on the user card.** Opening a user card from one of their chat messages shows a "Delete message" button when you're the broadcaster or a mod.
- **Command suggestions.** Typing / or . and a letter at the start of a message shows matching commands; Tab or Enter picks one, and Enter sends a command you've typed in full.
- **Title, category and uptime.** /settitle, /setgame (picks the closest category and says which) and /uptime work.

### Fixed
- **Moderation commands did nothing.** /warn, /mod, /unmod, /vip, /unvip, /announce (all colours), /slow, /followers, /emoteonly, /subscribers, /uniquechat (and their "off" forms), /shield, /clear, /delete, /raid, /unraid, /shoutout, /commercial and /marker work again. Each shows a short confirmation, or Twitch's reason when it fails.
- **Right-click "Delete message".** It said it wasn't supported; it now deletes the message.
- **/host and /unhost.** They now explain that Twitch retired hosting and point to /raid.

Older releases and downloads: [GitHub Releases](https://github.com/smitht06/red-chattersyn/releases).
<!-- release-notes:end -->
