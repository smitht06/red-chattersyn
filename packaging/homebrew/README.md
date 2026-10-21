# ChatterSyn Homebrew tap

macOS cask tap for
[ChatterSyn](https://github.com/smitht06/red-chattersyn) — a dual Twitch +
YouTube chat client. Republished automatically by CI on every release tag.

## Install

```sh
brew tap smitht06/chattersyn
brew install --cask chattersyn
```

## Update

```sh
brew upgrade --cask chattersyn
```

## Remove

```sh
brew uninstall --cask chattersyn          # keep preferences
brew uninstall --cask --zap chattersyn    # also trash ~/Library/Preferences/ChatterSyn
```

## Notes

- Apple Silicon only (arm64), macOS 14.4 or later (Qt 6.12). The cask
  declares `:sonoma` (14.0) because Homebrew has no 14.4 symbol.
- Ad-hoc signed, not notarized. Cask installs are downloaded outside
  Quarantine and open normally; a manually downloaded zip needs
  right-click → Open → Open on first launch to get past Gatekeeper.
- Credentials are stored in the Keychain under the service name
  `red-chattersyn`; `--zap` does not touch the Keychain. To remove leftover
  entries, open Keychain Access, search for "red-chattersyn", and delete them.
