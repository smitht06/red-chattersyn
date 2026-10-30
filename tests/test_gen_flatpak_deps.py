"""scripts/gen_flatpak_deps.py: Linux marker evaluation, wheel-vs-sdist choice,
build ordering and Rust crate vendoring, on a synthetic uv.lock."""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import gen_flatpak_deps as gen  # noqa: E402

CRATE_SUM = "c" * 64


def _sdist(tmp_path: Path, name: str, requires: list[str], cargo_lock: str | None = None):
    """A fake sdist tarball on disk; returns (file URL, sha256)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        files = {"pyproject.toml": f"[build-system]\nrequires = {json.dumps(requires)}\n"}
        if cargo_lock is not None:
            files["Cargo.lock"] = cargo_lock
        for filename, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{name}-1.0/{filename}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path = tmp_path / f"{name}-1.0.tar.gz"
    path.write_bytes(buffer.getvalue())
    return path.as_uri(), hashlib.sha256(path.read_bytes()).hexdigest()


def _wheel(name: str) -> str:
    return (f'wheels = [{{ url = "https://example.invalid/{name}-1.0-py3-none-any.whl", '
            f'hash = "sha256:{"a" * 64}" }}]')


def _lock(tmp_path: Path, rusty_cargo: str) -> Path:
    native = _sdist(tmp_path, "native", ["setuptools", "helper>=1"])
    helper = _sdist(tmp_path, "helper", ["setuptools"])
    rusty = _sdist(tmp_path, "rusty", ["setuptools", "tomli; python_version < '3.11'"], rusty_cargo)

    def sdist(url_sha):
        url, sha = url_sha
        return f'sdist = {{ url = "{url}", hash = "sha256:{sha}" }}'

    lock = f"""
version = 1
requires-python = ">=3.11"

[[package]]
name = "app"
version = "1.0"
source = {{ editable = "." }}
dependencies = [
    {{ name = "pure" }},
    {{ name = "native" }},
    {{ name = "winonly", marker = "sys_platform == 'win32'" }},
    {{ name = "pyside6" }},
]

[package.dev-dependencies]
flatpak-build = [{{ name = "rusty" }}, {{ name = "setuptools" }}]

[[package]]
name = "pure"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "linuxonly", marker = "sys_platform == 'linux'" }}]
{_wheel("pure")}

[[package]]
name = "linuxonly"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
{_wheel("linuxonly")}

[[package]]
name = "winonly"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
{_wheel("winonly")}

[[package]]
name = "native"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "helper" }}]
{sdist(native)}

[[package]]
name = "helper"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
{sdist(helper)}

[[package]]
name = "pyside6"
version = "6.12.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://example.invalid/pyside6-6.12.0-cp39-abi3-manylinux_2_28_x86_64.whl", hash = "sha256:{"b" * 64}" }}]

[[package]]
name = "rusty"
version = "1.0"
source = {{ registry = "https://pypi.org/simple" }}
{sdist(rusty)}

[[package]]
name = "setuptools"
version = "80.0"
source = {{ registry = "https://pypi.org/simple" }}
{_wheel("setuptools")}
"""
    path = tmp_path / "uv.lock"
    path.write_text(lock)
    return path


CARGO = f"""
version = 4

[[package]]
name = "rusty"
version = "1.0.0"

[[package]]
name = "libc"
version = "0.2.177"
source = "{gen.CRATES_IO}"
checksum = "{CRATE_SUM}"
"""


def test_runtime_module_follows_linux_markers_and_skips_pyside(tmp_path):
    _, deps = gen.generate(_lock(tmp_path, CARGO), tmp_path / "cache")
    urls = [s["url"] for s in deps["sources"]]
    assert any("linuxonly" in u for u in urls)  # sys_platform == 'linux' applies
    assert not any("winonly" in u or "pyside6" in u for u in urls)
    assert "cleanup" not in deps
    assert deps["build-options"] == gen.BUILD_OPTIONS  # no Rust toolchain without crates


def test_wheels_install_first_then_sdists_after_their_build_requirements(tmp_path):
    _, deps = gen.generate(_lock(tmp_path, CARGO), tmp_path / "cache")
    assert deps["build-commands"] == [
        f"{gen.PIP} linuxonly pure",
        f"{gen.PIP} helper",
        f"{gen.PIP} native",  # build-requires helper, another sdist
    ]


def test_build_tools_skip_sdk_packages_vendor_crates_and_clean_up(tmp_path):
    tools, _ = gen.generate(_lock(tmp_path, CARGO), tmp_path / "cache")
    assert tools["cleanup"] == ["*"]
    assert tools["build-commands"] == [f"{gen.PIP} rusty"]  # setuptools: from the SDK
    assert tools["build-options"]["env"]["CARGO_HOME"] == "/run/build/python3-build-tools/cargo"
    archive = next(s for s in tools["sources"] if s["type"] == "archive")
    assert archive == {
        "type": "archive", "archive-type": "tar-gzip",
        "url": "https://static.crates.io/crates/libc/libc-0.2.177.crate",
        "sha256": CRATE_SUM, "dest": "cargo/vendor/libc-0.2.177",
    }
    inline = [s for s in tools["sources"] if s["type"] == "inline"]
    assert json.loads(inline[0]["contents"]) == {"package": CRATE_SUM, "files": {}}
    assert inline[-1]["dest-filename"] == "config.toml"
    # the crate living inside the sdist itself is not downloaded
    assert sum(s["type"] == "archive" for s in tools["sources"]) == 1


def test_git_crates_are_refused(tmp_path):
    cargo = CARGO.replace(gen.CRATES_IO, "git+https://github.com/example/libc#abc")
    with pytest.raises(SystemExit, match="only crates.io"):
        gen.generate(_lock(tmp_path, cargo), tmp_path / "cache")


def test_a_tampered_sdist_is_refused(tmp_path):
    lock = _lock(tmp_path, CARGO)
    (tmp_path / "helper-1.0.tar.gz").write_bytes(b"not the locked file")
    with pytest.raises(SystemExit, match="does not match"):
        gen.generate(lock, tmp_path / "cache")


def test_generated_files_match_uv_lock():
    """The committed modules are current: re-run the script after `uv lock`."""
    root = Path(__file__).resolve().parent.parent
    packages = gen.load_lock(root / "uv.lock")
    for name in ("python3-build-tools", "python3-deps"):
        committed = json.loads((root / "packaging" / "flatpak" / f"{name}.json").read_text())
        files = [s for s in committed["sources"] if s["type"] == "file"]
        locked = {gen.artifact(packages[gen.canonicalize_name(_dist_name(s["url"]))])[1]
                  for s in files}
        assert {s["url"] for s in files} == locked, f"{name}.json is stale"


def _dist_name(url: str) -> str:
    filename = url.rsplit("/", 1)[-1]
    if filename.endswith(".whl"):
        return filename.split("-")[0]
    return filename.removesuffix(".tar.gz").rsplit("-", 1)[0]
