# ChatterSyn APT repository

Debian/Ubuntu/Mint package repository for
[ChatterSyn](https://github.com/smitht06/red-chattersyn) — a dual Twitch +
YouTube chat client. Republished automatically by CI on every release tag.

## Install

Add the signing key and the source, then install:

```sh
curl -fsSL https://smitht06.github.io/red-chattersyn-apt/chattersyn.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/chattersyn.gpg
echo "deb [signed-by=/usr/share/keyrings/chattersyn.gpg] https://smitht06.github.io/red-chattersyn-apt stable main" \
  | sudo tee /etc/apt/sources.list.d/chattersyn.list
sudo apt update
sudo apt install chattersyn
```

## Update

```sh
sudo apt update && sudo apt upgrade
```

## Remove

```sh
sudo apt remove chattersyn
```

Installs to `/opt/chattersyn` with a `chattersyn` launcher and a menu
entry. Requires glibc ≥ 2.35 (Ubuntu 22.04+, Debian 12+, Linux Mint 21+).
