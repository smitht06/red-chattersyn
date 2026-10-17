"""Regenerate stream_list_pb2{,_grpc}.py from proto/stream_list.proto.

Usage: uv run python scripts/codegen_proto.py

The proto comes from https://developers.google.com/youtube/v3/live/streaming-live-chat
(embedded in the docs page; not in any googleapis repo). grpc_tools.protoc emits
`import stream_list_pb2` in the _grpc file; since the generated pair lives in a
package, we patch it to a relative import.
"""

import sys
from pathlib import Path

import grpc_tools
from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto"
OUT_DIR = ROOT / "src" / "chattersyn" / "services" / "youtube"
WKT = Path(grpc_tools.__file__).parent / "_proto"

# ruff E402: sys.path juggling before imports is fine for scripts (per-file-ignored).
sys.path.insert(0, str(PROTO_DIR))


def main() -> None:
    rc = protoc.main([
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"-I{WKT}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        str(PROTO_DIR / "stream_list.proto"),
    ])
    if rc != 0:
        raise SystemExit(rc)
    grpc_file = OUT_DIR / "stream_list_pb2_grpc.py"
    text = grpc_file.read_text(encoding="utf-8")
    text = text.replace(
        "import stream_list_pb2 as stream__list__pb2",
        "from . import stream_list_pb2 as stream__list__pb2",
    )
    grpc_file.write_text(text, encoding="utf-8")
    print(f"generated {OUT_DIR / 'stream_list_pb2.py'} and {grpc_file}")


if __name__ == "__main__":
    main()
