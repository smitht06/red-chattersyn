# Advanced setup

Details for people who want more than the defaults. The [README](../README.md)
covers the normal install and sign-in.

## macOS: Keychain prompts and signing

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

The standalone `chattersyn-<version>-macos-arm64.zip` is ad-hoc signed (not
notarized): a manually downloaded zip needs right-click → Open → Open on first
launch to get past Gatekeeper. Homebrew installs open normally.

## Linux: package details

The `.deb` installs to `/opt/chattersyn` with a `chattersyn` launcher and a
menu entry; remove it with `sudo apt remove chattersyn`. It's built on Ubuntu
22.04 (glibc 2.35), so it runs on Linux Mint 21+ and similar. Credentials are
stored via the Secret Service (gnome-keyring / KWallet) when available;
otherwise ChatterSyn warns you and keeps them in `secrets.json` next to its
settings, readable only by you.

Each release also carries a standalone
`chattersyn-<version>-linux-x86_64.deb`
(`sudo apt install ./chattersyn-<version>-linux-x86_64.deb`) and a portable
`chattersyn-<version>-linux-x86_64.tar.gz` (extract and run `./chattersyn`).

The Flatpak uses the KDE runtime from Flathub, which `flatpak install` fetches
for you (if Flathub isn't set up yet: `flatpak remote-add --if-not-exists
--user flathub https://dl.flathub.org/repo/flathub.flatpakrepo`). It keeps its
settings in `~/.var/app/net.redcyberdragon.ChatterSyn`, separate from the
`.deb`. ChatterSyn isn't on Flathub: Flathub doesn't accept AI-written
packaging.

## Twitch: your own application

ChatterSyn signs in through its registered "ChatterSyn" Twitch application (an
OAuth **public client** — PKCE auth-code flow, no client secret). The redirect
URIs `http://localhost:17821`, `http://localhost:17822` and
`http://localhost:17823` are pre-registered (ChatterSyn binds the first free
port).

To use your own Twitch application instead, register one at
[dev.twitch.tv/console](https://dev.twitch.tv/console) (category "Chat Bot",
OAuth Redirect URLs exactly the three above) and put its Client ID in
`settings.json`:

```json
{"twitch": {"client_id": "your-client-id"}}
```

There is no UI field for this. Without signing in, chat reading and emotes
still work anonymously (Twitch, 7TV, BTTV, FFZ); sending, badges, live status
and moderation need the account.

## YouTube: API key or your own Google app

ChatterSyn's Google app needs one permission, `youtube.force-ssl`, which covers
reading, sending and moderating. The app's Google client secret isn't in the
app: sign-in goes through ChatterSyn's sign-in relay on
`chattersyn.redcyberdragon.net` (`site/functions/`), which adds it, passes the
request to Google and stores nothing.
Its rate limit is a Cloudflare dashboard rule: see
[Rate-limit the Google sign-in relay](relay-rate-limit.md).

With your own Google Cloud project
([console.cloud.google.com](https://console.cloud.google.com/), enable
**YouTube Data API v3**), under Settings → Accounts → *YouTube: advanced*:

- **Your own API key:** create an **API key** and paste it into **Data API
  key**. ChatterSyn then reads public chats and checks channels with it, so
  your own quota pays for reading. Sending, moderation and your Stream tab
  still use your Google sign-in.
- **Your own Google app:** set up the OAuth consent screen (External, add
  yourself as a test user), create an OAuth client ID of type *Desktop app*,
  **Download JSON** (`client_secrets.json`) and pick it with **Use Own Google
  Client…**. It then replaces ChatterSyn's app, with your project's quota.

The Stream tab's YouTube side needs the sign-in: "my live stream"
(`liveBroadcasts.list mine=true`) rejects API keys.

## YouTube quota

Google gives each project 10,000 quota units a day, reset at midnight Pacific
time. Everyone signed in through ChatterSyn's app shares that budget until
Google raises it; your own API key or app has its own.

- Reading live chat uses YouTube's streaming endpoint and costs nothing.
- Checking whether a channel is live costs about 300 units a day for an idle
  channel tab; tabs you aren't looking at check less often.
- Display names cost 1 unit per 50 new chatters; the streamer-bar stats cost
  1 unit about every 2 minutes while a stream is live.
- Each send costs 20 units.

When the quota runs out, ChatterSyn shows "YouTube quota used up · resumes at
HH:MM", pauses YouTube and switches its send toggle off, and resumes on its own
shortly after the reset. Twitch keeps working.
