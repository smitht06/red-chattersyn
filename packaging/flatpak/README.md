# Flatpak

App ID **`net.redcyberdragon.ChatterSyn`** — permanent: the desktop file, the
icon, the metainfo and users' settings folder all use it.

| File | What it is |
|---|---|
| `net.redcyberdragon.ChatterSyn.yml` | The manifest; builds from this checkout |
| `python3-build-tools.json`, `python3-deps.json` | Generated from `uv.lock` by `scripts/gen_flatpak_deps.py` — don't edit |
| `net.redcyberdragon.ChatterSyn.desktop` | Desktop entry (`packaging/linux/` has the metainfo) |
| `screenshots/main.png` | Store screenshot |

## Keeping it current

- After any `uv lock` that changes a runtime dependency, run
  `uv run python scripts/gen_flatpak_deps.py`. `tests/test_gen_flatpak_deps.py`
  fails while the JSON files are stale.
- PySide6 comes from `io.qt.PySide.BaseApp`, not PyPI (Flathub rejects the
  wheels). Bump `runtime-version` and `base-version` together; the app's tests
  pass on PySide6 6.11 as well as 6.12.
- New screenshot:
  `QT_QPA_PLATFORM=offscreen uv run python scripts/screenshot.py packaging/flatpak/screenshots/main.png 15`
  (15 s lets the emote images load).
- Each release: add a `<release>` to `packaging/linux/net.redcyberdragon.ChatterSyn.metainfo.xml`.

## Build and run locally (Linux)

```sh
flatpak install --user flathub org.kde.Sdk//6.11 io.qt.PySide.BaseApp//6.11 \
    org.freedesktop.Sdk.Extension.rust-stable//25.08 org.flatpak.Builder
flatpak-builder --user --install --force-clean build-dir \
    packaging/flatpak/net.redcyberdragon.ChatterSyn.yml
flatpak run net.redcyberdragon.ChatterSyn --demo
```

The first build compiles maturin, cryptography and grpcio from source (Flathub
builds offline from source); later builds are cached. Lint it the way Flathub does:

```sh
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest \
    packaging/flatpak/net.redcyberdragon.ChatterSyn.yml
```

## Distribution

CI builds the bundle on every push (the `flatpak` job, x86_64). On a release
tag, the `release` job attaches it to the GitHub release as
`chattersyn-X.Y.Z-linux-x86_64.flatpak`, next to the `.deb`. Users install it
with `flatpak install --user <file>`, which pulls the KDE runtime and PySide
BaseApp from Flathub, and update by installing the next release's file.

## Why not Flathub

Flathub's [requirements](https://docs.flathub.org/docs/for-app-authors/requirements)
(checked 2026-11-01) ban AI-generated or AI-assisted content in Flathub
manifests, even disclosed, and AI-written submission PRs, commit messages and
review replies; AI-generated app code must be disclosed and may be rejected.
This manifest and `scripts/gen_flatpak_deps.py` were written with AI
assistance, as was much of ChatterSyn, so they can't be submitted as they are.
A Flathub listing would need a hand-written manifest and PR, plus the
disclosure; reviewers decide. [SUBMITTING.md](SUBMITTING.md) walks through it.

## Permissions

| Permission | Why |
|---|---|
| `--share=network` | Twitch IRC/Helix, YouTube API, emote CDNs; OAuth sign-in on a localhost port |
| `--socket=wayland`, `--socket=fallback-x11`, `--share=ipc`, `--device=dri` | Qt UI with OpenGL |
| `--socket=pulseaudio` | The highlight ping sound |
| `--talk-name=org.freedesktop.secrets` | Sign-in tokens in the desktop keyring (GNOME Keyring, KWallet) |

The Flatpak keeps its settings in `~/.var/app/net.redcyberdragon.ChatterSyn`,
separate from the `.deb` install's `~/.config`.
