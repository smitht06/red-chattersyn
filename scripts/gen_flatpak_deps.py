#!/usr/bin/env python3
"""Generate the Flatpak's Python dependency modules from uv.lock.

    uv run python scripts/gen_flatpak_deps.py [--cache DIR]

writes packaging/flatpak/python3-build-tools.json and
packaging/flatpak/python3-deps.json, which the manifest
(packaging/flatpak/net.redcyberdragon.ChatterSyn.yml) includes as modules.
Re-run it after every `uv lock` that changes a runtime dependency.

flatpak-builder works offline, so every package — and every Rust crate of a
package that compiles Rust — has to be a source with a pinned hash.
flatpak-pip-generator resolves on the machine it runs on, which on macOS
drops keyring's Linux-only backend (secretstorage, jeepney, cryptography).
uv.lock is universal, so this evaluates its markers for the Flatpak instead:
Linux, CPython 3.13 (the KDE 6.11 SDK).

- Runtime packages: the project's dependencies, minus PySide6/shiboken6,
  which io.qt.PySide.BaseApp provides.
- Build tools: the `flatpak-build` dependency group, minus what the KDE SDK
  already ships (pip, setuptools, wheel, Cython, packaging). Their module has
  `cleanup: ["*"]`, so they don't end up in the app.
- Each package uses its pure-Python wheel when it has one, else its sdist.
  sdists are downloaded (hash-checked) to read their build requirements,
  which order the installs, and their Cargo.lock, whose crates are vendored.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "flatpak"
BUILD_GROUP = "flatpak-build"

# The Flatpak's interpreter: org.kde.Sdk//6.11 ships CPython 3.13.15.
# platform_machine is unset on purpose: no lock marker in this project uses it,
# and the same JSON serves Flathub's x86_64 and aarch64 builds.
ENV = {
    "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
    "python_version": "3.13", "python_full_version": "3.13.15",
    "platform_python_implementation": "CPython", "implementation_name": "cpython",
    "implementation_version": "3.13.15", "platform_machine": "", "platform_release": "",
    "platform_version": "", "extra": "",
}
# Provided by io.qt.PySide.BaseApp.
FROM_BASEAPP = {"pyside6", "pyside6-essentials", "pyside6-addons", "pyside6-pdf",
                "pyside6-webengine", "shiboken6"}
# Shipped by org.kde.Sdk//6.11 (build tools only; they are not runtime deps).
FROM_SDK = {"pip", "setuptools", "wheel", "cython", "packaging"}
# Extra environment for particular source builds. grpcio compiles ~1000 C++
# files; more than 4 at once runs builders with 8 GB out of memory.
BUILD_ENV = {"grpcio": "GRPC_PYTHON_BUILD_EXT_COMPILER_JOBS="
                       "$(( FLATPAK_BUILDER_N_JOBS < 4 ? FLATPAK_BUILDER_N_JOBS : 4 )) "}
# Compiled Python extensions without debug info: users never need their
# symbols, and with -g each grpcio compiler process needs 1-2 GB.
BUILD_OPTIONS = {"cflags": "-g0", "cxxflags": "-g0", "no-debuginfo": True}
PIP = ('pip3 install --verbose --prefix=${FLATPAK_DEST} '
       '--no-index --find-links="file://${PWD}" --no-build-isolation --no-deps')
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
CARGO_CONFIG = ('[source.vendored-sources]\ndirectory = "cargo/vendor"\n\n'
                '[source.crates-io]\nreplace-with = "vendored-sources"\n')


def load_lock(path: Path) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    for package in tomllib.loads(path.read_text(encoding="utf-8"))["package"]:
        name = canonicalize_name(package["name"])
        if name in packages:
            raise SystemExit(f"error: {name} is locked at several versions; "
                             "pick one in pyproject.toml")
        packages[name] = package
    return packages


def _applies(dep: dict) -> bool:
    marker = dep.get("marker")
    return marker is None or Marker(marker).evaluate(ENV)


def closure(packages: dict[str, dict], roots: list[dict]) -> set[str]:
    """Every package the root dependency entries pull in on the Flatpak."""
    visited: set[tuple[str, frozenset[str]]] = set()
    todo = [d for d in roots if _applies(d)]
    while todo:
        dep = todo.pop()
        name = canonicalize_name(dep["name"])
        key = (name, frozenset(dep.get("extra", [])))
        if key in visited:
            continue
        visited.add(key)
        package = packages[name]
        deps = list(package.get("dependencies", []))
        for extra in dep.get("extra", []):
            deps += package.get("optional-dependencies", {}).get(extra, [])
        todo += [d for d in deps if _applies(d)]
    return {name for name, _ in visited}


def artifact(package: dict) -> tuple[str, str, str]:
    """(kind, url, sha256): the pure-Python wheel if there is one, else the sdist."""
    for wheel in package.get("wheels", []):
        if wheel["url"].endswith("-none-any.whl"):
            return "wheel", wheel["url"], _sha256(wheel["hash"])
    if "sdist" not in package:
        raise SystemExit(f"error: {package['name']} has neither a pure-Python wheel nor an sdist")
    return "sdist", package["sdist"]["url"], _sha256(package["sdist"]["hash"])


def _sha256(lock_hash: str) -> str:
    algorithm, _, digest = lock_hash.partition(":")
    if algorithm != "sha256":
        raise SystemExit(f"error: unsupported hash {lock_hash!r}")
    return digest


def fetch(url: str, sha256: str, cache: Path) -> bytes:
    path = cache / url.rsplit("/", 1)[-1]
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
        return path.read_bytes()
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"error: {url} does not match its uv.lock hash")
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def sdist_info(data: bytes) -> tuple[list[str], str | None]:
    """(build requirements, top-level Cargo.lock text) of an sdist tarball."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        top = tar.getnames()[0].split("/")[0]

        def read(name: str) -> str | None:
            try:
                member = tar.extractfile(f"{top}/{name}")
            except KeyError:
                return None
            return member.read().decode("utf-8") if member else None

        pyproject = read("pyproject.toml")
        requires = tomllib.loads(pyproject).get("build-system", {}).get("requires", []) if pyproject else []
        return requires, read("Cargo.lock")


def crate_sources(cargo_lock: str, owner: str) -> list[dict]:
    """flatpak-builder sources that vendor a Cargo.lock's crates.io crates."""
    sources = []
    for crate in tomllib.loads(cargo_lock)["package"]:
        source = crate.get("source")
        if source is None:  # a path dependency inside the sdist itself
            continue
        if source != CRATES_IO:
            raise SystemExit(f"error: {owner}'s crate {crate['name']} comes from {source}; "
                             "only crates.io crates can be vendored")
        dest = f"cargo/vendor/{crate['name']}-{crate['version']}"
        sources += [
            {"type": "archive", "archive-type": "tar-gzip",
             "url": f"https://static.crates.io/crates/{crate['name']}/"
                    f"{crate['name']}-{crate['version']}.crate",
             "sha256": crate["checksum"], "dest": dest},
            {"type": "inline", "contents": json.dumps({"package": crate["checksum"], "files": {}}),
             "dest": dest, "dest-filename": ".cargo-checksum.json"},
        ]
    return sources


def _build_deps(requires: list[str]) -> set[str]:
    names = set()
    for line in requires:
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(ENV):
            names.add(canonicalize_name(requirement.name))
    return names


def module(name: str, packages: dict[str, dict], names: set[str], cache: Path,
           cleanup: bool = False) -> dict:
    wheels, sdists, sources, crates = [], {}, [], {}
    for pkg_name in sorted(names):
        kind, url, sha256 = artifact(packages[pkg_name])
        sources.append({"type": "file", "url": url, "sha256": sha256})
        if kind == "wheel":
            wheels.append(pkg_name)
            continue
        requires, cargo_lock = sdist_info(fetch(url, sha256, cache))
        sdists[pkg_name] = _build_deps(requires)
        if cargo_lock is not None:
            for source in crate_sources(cargo_lock, pkg_name):
                crates.setdefault((source["dest"], source["type"]), source)

    commands = [f"{PIP} {' '.join(wheels)}"] if wheels else []
    done: set[str] = set()
    while len(done) < len(sdists):  # an sdist builds after the sdists it build-requires
        ready = [n for n in sorted(sdists) if n not in done and not (sdists[n] & set(sdists)) - done]
        if not ready:
            raise SystemExit(f"error: circular build requirements among {sorted(set(sdists) - done)}")
        for pkg_name in ready:
            commands.append(f"{BUILD_ENV.get(pkg_name, '')}{PIP} {pkg_name}")
            done.add(pkg_name)

    result: dict = {"name": name, "buildsystem": "simple", "build-commands": commands,
                    "build-options": dict(BUILD_OPTIONS)}
    if crates:
        result["build-options"].update({
            "append-path": "/usr/lib/sdk/rust-stable/bin",
            "env": {"CARGO_HOME": f"/run/build/{name}/cargo", "CARGO_NET_OFFLINE": "true"},
        })
        sources += list(crates.values())
        sources.append({"type": "inline", "contents": CARGO_CONFIG,
                        "dest": "cargo", "dest-filename": "config.toml"})
    result["sources"] = sources
    if cleanup:
        result["cleanup"] = ["*"]
    return result


def generate(lock: Path, cache: Path) -> tuple[dict, dict]:
    packages = load_lock(lock)
    project = next(p for p in packages.values() if p.get("source", {}).get("editable") == ".")
    runtime = closure(packages, project.get("dependencies", [])) - FROM_BASEAPP
    group = project.get("dev-dependencies", {}).get(BUILD_GROUP)
    if group is None:
        raise SystemExit(f"error: uv.lock has no `{BUILD_GROUP}` dependency group")
    tools = closure(packages, group) - FROM_SDK - runtime
    return (module("python3-build-tools", packages, tools, cache, cleanup=True),
            module("python3-deps", packages, runtime, cache))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--cache", type=Path, default=ROOT / "build" / "flatpak-sdists",
                        help="where downloaded sdists are kept between runs")
    args = parser.parse_args(argv)
    for mod in generate(args.lock, args.cache):
        path = args.out / f"{mod['name']}.json"
        path.write_text(json.dumps(mod, indent=2) + "\n", encoding="utf-8")
        crates = sum(1 for s in mod["sources"] if s["type"] == "archive")
        print(f"wrote {path} ({len(mod['build-commands'])} install steps, {crates} crates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
