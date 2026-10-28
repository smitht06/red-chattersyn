---
name: replace-logo
description: Replace the ChatterSyn app logo everywhere from an SVG file. Validates the SVG (vector only; PNG/JPEG/ICNS are refused because bitmaps lose quality when scaled up), previews it at 16–1024 px on dark and light backgrounds, then renders every size straight from the vectors - the 1024 px build icon that the macOS .icns, Windows .ico and Linux .deb icons come from, plus the in-app window icon - and verifies them. Use when asked to change, replace or update the logo, app icon or branding.
argument-hint: "<path/to/logo.svg>"
---

# Replace the logo

The master logo is `packaging/icon.svg`. Everything else comes from it:

| File | Size | Made by | Shows up in |
|---|---|---|---|
| `packaging/icon.svg` | vector | step 4 (copy of the new logo) | the master, committed |
| `packaging/icon.png` | 1024 px | `replace_logo.py apply` | build input for the three below, committed |
| `src/chattersyn/assets/icon.png` | 256 px | `replace_logo.py apply` | window/taskbar icon (`app.py`); the Dock under `uv run`; committed |
| `packaging/icon.icns` | 16–1024 px | `make_icon.py` (build time, gitignored) | macOS .app: Dock, Finder, Launchpad |
| `packaging/icon.ico` | 16–256 px | `make_icon.py` (build time, gitignored) | Windows .exe |
| `/usr/share/icons/hicolor/*/apps/chattersyn.png` | 16–512 px | `build_deb.py` (build time) | Linux menus and launchers |

`scripts/replace_logo.py` renders with QtSvg, the app's own SVG engine. Each
committed PNG records the SVG's SHA-256, and `tests/test_replace_logo.py`
fails if `icon.svg` changes without a re-render.

Ask the developer questions with the AskUserQuestion tool where this says
so. Keep each question short and give 2–4 concrete options with the
recommended one first.

## 1. Get the SVG

- Use the path in `$ARGUMENTS`. With no argument, look for recently added
  `.svg` files (`git status --short`, `~/Downloads`, `~/Desktop`) and ask
  which one to use.
- **SVG only.** If the developer offers a PNG, JPEG, ICNS or other bitmap,
  don't convert it. Explain that the logo has to be an SVG so that every
  size from 16 to 1024 px is drawn from the vectors, and ask them to export
  SVG from their editor. The script refuses other formats anyway.
- To re-render after editing `packaging/icon.svg` in place, pass that path.

## 2. Check it

Run this with a fresh directory in your scratchpad, not the repo:

    uv run python scripts/replace_logo.py check <svg> --preview <scratch>/logo-preview

It prints `error:`, `warning:` and `note:` lines, then the preview paths.
Exit status 1 means errors, and nothing can be applied. Explain each error
(the fix is in the message) and stop until the developer sends a fixed SVG.

Explain each warning in plain words:
- **bitmap ... gets blurry**: part of the "SVG" is a low-resolution bitmap
  (editors such as Pixelmator embed raster layers), so it won't scale
  without loss. That defeats the point of an SVG, so ask for vector shapes.
- **opaque background**: the corners are solid, so the Dock, taskbar and
  launchers show a full square tile instead of the logo's own shape. That's
  fine if it's intended. Otherwise the developer should make the background
  transparent in the editor. A `note:` naming a solid-color bitmap usually
  points at the background layer to hide.
- **not square**: the logo gets centered on a transparent square canvas.
- **Qt's SVG renderer: ...**: something Qt couldn't draw. Compare the
  preview with the design.

## 3. Preview and confirm

Read both preview images. `logo-1024.png` is the full render, and
`logo-sizes.png` shows 16–256 px on a dark and a light background. Describe
what you see: whether it stays recognizable at 16 and 32 px, and how it
looks on both backgrounds. Give the paths so the developer can open them.
Mention that Qt can draw heavy blurs and shadows a little differently from
design tools and browsers.

Ask "Replace the ChatterSyn logo with this?" with the options "Yes, replace
it" and "No, I'll change the SVG first". Mark "Yes" as recommended only if
there are no warnings. **Don't write to the repo before a yes.**

## 4. Apply

    uv run python scripts/replace_logo.py apply <svg>
    uv run --group build python scripts/make_icon.py

The first command writes `packaging/icon.svg`, `packaging/icon.png` and
`src/chattersyn/assets/icon.png`. The second derives `packaging/icon.icns`
and `packaging/icon.ico`. CI does that for every build; running it here is
for local builds and the checks below.

## 5. Verify

- macOS: `iconutil -c iconset packaging/icon.icns -o <scratch>/check.iconset`
  must succeed and list 16x16@2x through 512x512@2x.
- `uv run pytest -q tests/test_replace_logo.py tests/test_app.py`.
  `test_committed_renders_match_the_master_svg` confirms that both PNGs were
  rendered from the current `packaging/icon.svg`.
- `uv run ruff check src/ tests/ scripts/replace_logo.py`.
- `git status --short` should list only the three logo files. If the
  developer's original SVG sits inside the repo, it's now a duplicate of
  `packaging/icon.svg`. Ask before deleting it.

## 6. Report

Say which files changed and where the new logo shows up. `uv run chattersyn`
shows it right away (window, taskbar, Dock). Installed apps (Homebrew, APT,
the zip and tarball downloads) only get it with the next release (/release
or /prerelease), and `uv run python scripts/build.py` makes a local .app or
.deb before then. If macOS keeps showing the old icon after a new build is
installed, the icon cache is stale: `touch` the .app or run `killall Dock`.

Don't commit: the developer or /release does that.
