# Flatpak (Flathub)

App ID **`net.redcyberdragon.ChatterSyn`** — permanent: Flathub, the desktop
file, the icon and the metainfo all use it.

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

## Submitting to Flathub (once)

1. Fork [flathub/flathub](https://github.com/flathub/flathub) and branch from `new-pr`.
2. Add the manifest and the two JSON files. In the manifest, switch the
   `chattersyn` module's `sources` from this directory to the release tag:

   ```yaml
       sources:
         - type: git
           url: https://github.com/smitht06/red-chattersyn.git
           tag: vX.Y.Z
           commit: <the tag's full commit hash>
           x-checker-data:
             type: git
             tag-pattern: ^v([\d.]+)$
   ```

3. Open a pull request against `new-pr` titled `Add net.redcyberdragon.ChatterSyn`.
   Flathub's bot builds it (comment `bot, build` to rebuild); answer the reviewers.
4. After the merge, Flathub creates `flathub/net.redcyberdragon.ChatterSyn` with
   you as maintainer. Updates are pull requests there: `x-checker-data` makes
   Flathub's bot open one for each new release tag. When dependencies change,
   also copy in freshly generated JSON files.
5. Verified badge: the Flathub developer portal gives a token to publish at
   `https://redcyberdragon.net/.well-known/org.flathub.VerifiedApps.txt`.

## Permissions (for reviewers)

| Permission | Why |
|---|---|
| `--share=network` | Twitch IRC/Helix, YouTube API, emote CDNs; OAuth sign-in on a localhost port |
| `--socket=wayland`, `--socket=fallback-x11`, `--share=ipc`, `--device=dri` | Qt UI with OpenGL |
| `--socket=pulseaudio` | The highlight ping sound |
| `--talk-name=org.freedesktop.secrets` | Sign-in tokens in the desktop keyring (GNOME Keyring, KWallet) |

The Flatpak keeps its settings in `~/.var/app/net.redcyberdragon.ChatterSyn`,
separate from the `.deb` install's `~/.config`.
