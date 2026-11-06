# Submitting ChatterSyn to Flathub (by hand)

ChatterSyn's Flatpak already ships on every GitHub release
(`chattersyn-X.Y.Z-linux-x86_64.flatpak`), so a Flathub listing is optional.
This guide is for doing it anyway. Checked against Flathub's documentation on
2026-11-01; the rules can change, so re-read them before you start.

## The rules that shape everything

Flathub's [requirements](https://docs.flathub.org/docs/for-app-authors/requirements)
include a Generative AI policy:

- **The manifest must contain no AI-generated or AI-assisted content**, even if
  you disclose it. The manifest and dependency files in this folder, and
  `scripts/gen_flatpak_deps.py`, were written with AI help, so **don't submit
  them or copy from them**. Write the Flathub manifest yourself.
- **No AI in the submission itself:** the pull request, its description, its
  commit messages and your replies to reviewers must be your own writing, and
  the PR mustn't be opened or automated by an AI.
- **Disclose AI-generated app code, documentation and packaging.** Much of
  ChatterSyn's code, and its packaging files (the AppStream metainfo, the
  desktop file, the icons), were written with AI help. Say which parts and
  roughly how much. Reviewers decide, and can reject on that basis.
- Using AI only for research, discussion or debugging needs no disclosure, as
  long as nothing it produced ends up in the app or the packaging.
- Undisclosed AI material or AI-written PRs can lead to rejection, and repeated
  violations to a permanent ban from Flathub.

When in doubt about whether something counts as AI-assisted, ask the Flathub
reviewers or the [Flathub Matrix room](https://matrix.to/#/#flathub:matrix.org)
before submitting.

## Steps

### 0. Read the rules

[Requirements](https://docs.flathub.org/docs/for-app-authors/requirements) and
[Submission](https://docs.flathub.org/docs/for-app-authors/submission). Carry on
only if you're comfortable hand-writing the manifest and PR and making the
disclosure.

### 1. Prepare

- **GitHub:** turn on two-factor authentication; Flathub requires it before it
  makes you the app's maintainer.
- **A Linux machine** with Flatpak, the Flathub remote and `org.flatpak.Builder`
  installed (the Submission page has the commands).

### 2. Write the manifest yourself

Name it `net.redcyberdragon.ChatterSyn.yml`. The app ID is permanent: the
desktop file, icon and metainfo in this repository already use it.

Docs that cover what you'll need:

- [Flatpak: Python](https://docs.flatpak.org/en/latest/python.html)
- [KDE: publishing a Python app as a Flatpak](https://develop.kde.org/docs/getting-started/python/python-flatpak/),
  which covers PySide through `io.qt.PySide.BaseApp`
- [flatpak-pip-generator](https://github.com/flatpak/flatpak-builder-tools/blob/master/pip/readme.md)
  and, for Rust-based Python packages, `flatpak-cargo-generator` in the same
  repository

Flathub's constraints:

- Everything is **built from source, offline**; no network during the build.
- Dependency lists must come from **external generator tools** like the ones
  above, not be written by hand or by an AI.
- The app's own source is **this repository at a release tag**, with that tag's
  commit hash, for example `v0.10.1`.

### 3. Build and lint on the Linux machine

From Flathub's Submission page:

```sh
flatpak run --command=flathub-build org.flatpak.Builder --install net.redcyberdragon.ChatterSyn.yml
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest net.redcyberdragon.ChatterSyn.yml
```

Run the app (`flatpak run net.redcyberdragon.ChatterSyn`), and fix everything the
linter reports before you submit.

### 4. Fork, branch, commit

1. Fork [flathub/flathub](https://github.com/flathub/flathub/fork) and **untick
   "Copy the master branch only"**: you need its `new-pr` branch.
2. Clone the `new-pr` branch and make your own branch from it:

   ```sh
   git clone --branch=new-pr git@github.com:<your-github-username>/flathub.git
   cd flathub
   git checkout -b add-chattersyn new-pr
   ```

3. Put your manifest and the generated dependency files **at the top level**.
   No binaries, no build output.
4. Commit with a message you write, and push the branch to your fork.

### 5. Open the pull request (in the browser)

- **Base:** `new-pr` in `flathub/flathub`, **not** `master`.
- **Title:** `Add net.redcyberdragon.ChatterSyn`
- Keep Flathub's PR template and fill in its checklist yourself.
- Explain, in your own words, why each permission is needed. ChatterSyn uses
  network access (Twitch, YouTube, emote image servers, the sign-in page on a
  localhost port), a display (Wayland, with X11 as fallback, plus graphics),
  audio (the highlight ping) and the desktop keyring (sign-in tokens).
- Include the AI disclosure from "The rules that shape everything".

### 6. Review

- Answer the reviewers yourself, and push fixes to the same branch. Don't close
  the PR to make changes, and don't merge `master` into your branch.
- Once their comments are addressed, comment `bot, build` to start a test build.

### 7. After approval

Flathub creates the repository `flathub/net.redcyberdragon.ChatterSyn` and
invites you as its maintainer. **Accept within 7 days.** Future updates are pull
requests to that repository.

### 8. Optional: the verified badge

The Flathub developer portal gives you a token to publish at
`https://redcyberdragon.net/.well-known/org.flathub.VerifiedApps.txt`. That's
ChatterSyn's website (`site/` in this repository, deployed by CI), not Flathub
content, so it's fine to get help adding it.

## Checklist before you submit

- [ ] I've read the current Requirements and Submission pages.
- [ ] I wrote the manifest myself; nothing in it comes from this folder or an AI.
- [ ] Dependency files come from the external generator tools.
- [ ] The app source is a release tag with its commit hash.
- [ ] It builds, runs and passes the linter on my Linux machine.
- [ ] The PR targets `new-pr`, keeps the template, explains the permissions
      and contains my AI disclosure, all in my own words.
- [ ] Two-factor authentication is on for my GitHub account.
