#!/usr/bin/env python3
"""Assemble a flat APT repo index for one .deb.

Usage: gen_apt_index.py <deb> <filename> <out-dir> [control-file]

Writes, under <out-dir>:
  dists/stable/main/binary-amd64/Packages       (stanza + control)
  dists/stable/main/binary-amd64/Packages.gz
  dists/stable/Release                          (MD5Sum/SHA1/SHA256/SHA512)

<filename> is the pool-relative path of the .deb, e.g.
``pool/main/c/chattersyn/chattersyn-0.3.0-linux-x86_64.deb``. apt treats
Filename as relative to the repository base URI, so this must be a path
*inside* the published tree — an absolute URL would be prepended with the
base and 404. Control fields come from `dpkg-deb -f`; pass <control-file>
to use a pre-substituted control template instead (dpkg-deb is not
available everywhere, e.g. macOS manual publishes).

Signing (Release.gpg / InRelease) is done by the caller with gpg —
this script only writes the unsigned index.
"""
import gzip
import hashlib
import os
import subprocess
import sys
import time


def main() -> int:
    if not 4 <= len(sys.argv) <= 5:
        print(__doc__, file=sys.stderr)
        return 2
    deb, filename, out_dir = sys.argv[1:4]
    control_file = sys.argv[4] if len(sys.argv) == 5 else None

    arch_dir = os.path.join(out_dir, "dists", "stable", "main", "binary-amd64")
    os.makedirs(arch_dir, exist_ok=True)

    size = os.path.getsize(deb)
    with open(deb, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    if control_file:
        with open(control_file, encoding="utf-8") as fh:
            control = fh.read()
    else:
        control = subprocess.check_output(["dpkg-deb", "-f", deb]).decode()

    packages_path = os.path.join(arch_dir, "Packages")
    with open(packages_path, "w", encoding="utf-8") as fh:
        fh.write(f"Filename: {filename}\nSize: {size}\nSHA256: {sha}\n{control}")
    with open(packages_path, "rb") as src, gzip.GzipFile(
        filename=packages_path + ".gz", mode="wb", mtime=0
    ) as dst:
        dst.write(src.read())

    # Index files listed in Release, paths relative to the tree root.
    tree = os.path.join(out_dir, "dists", "stable")
    indexes = ["main/binary-amd64/Packages", "main/binary-amd64/Packages.gz"]
    sections = {
        "MD5Sum": hashlib.md5,
        "SHA1": hashlib.sha1,
        "SHA256": hashlib.sha256,
        "SHA512": hashlib.sha512,
    }
    lines = [
        "Origin: ChatterSyn",
        "Label: ChatterSyn",
        "Suite: stable",
        "Codename: stable",
        "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime()),
        "Architectures: amd64",
        "Components: main",
        "Description: ChatterSyn APT repository",
    ]
    for name, algo in sections.items():
        lines.append(name + ":")
        for rel in indexes:
            with open(os.path.join(tree, rel), "rb") as fh:
                digest = algo(fh.read()).hexdigest()
            lines.append(f" {digest} {os.path.getsize(os.path.join(tree, rel)):>16} {rel}")
    with open(os.path.join(tree, "Release"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"indexed {filename} ({size} bytes) into {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
