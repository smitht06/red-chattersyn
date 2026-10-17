"""Readers: gRPC streamList against a fake in-process server, REST fallback."""

import asyncio

import grpc
import httpx
import pytest
import pytest_asyncio

from chattersyn.services.youtube import stream_list_pb2 as pb
from chattersyn.services.youtube import stream_list_pb2_grpc
from chattersyn.services.youtube.api import QuotaTracker, YouTubeApi
from chattersyn.services.youtube.reader import GrpcReader, RestReader, StreamEnded


def _pb_item(msg_id, text):
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        published_at="2026-10-15T10:00:00Z",
        has_display_content=True,
        display_message=text,
        text_message_details=pb.LiveChatTextMessageDetails(message_text=text),
    )
    item = pb.LiveChatMessage(id=msg_id, snippet=snippet)
    item.author_details.display_name = "Alice"
    item.author_details.channel_id = "UCa"
    return item


class FakeServicer(stream_list_pb2_grpc.V3DataLiveChatMessageServiceServicer):
    """Replays canned responses, optionally aborting with a gRPC status."""

    def __init__(self, responses=(), abort=None):
        self.responses = list(responses)
        self.abort = abort  # (status_code, details) — aborts immediately

    async def StreamList(self, request, context):
        if self.abort is not None:
            code, details = self.abort
            await context.abort(code, details)
            return
        for response in self.responses:
            yield response


@pytest_asyncio.fixture
async def grpc_server():
    server = grpc.aio.server()
    servicer = FakeServicer()
    stream_list_pb2_grpc.add_V3DataLiveChatMessageServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    yield channel, servicer
    await channel.close()
    await server.stop(0)


async def _metadata():
    return (("x-goog-api-key", "test-key"),)


async def test_grpc_reader_streams_items_and_returns_token(grpc_server):
    channel, servicer = grpc_server
    servicer.responses = [
        pb.LiveChatMessageListResponse(items=[_pb_item("a", "one")]),
        pb.LiveChatMessageListResponse(
            items=[_pb_item("b", "two")], next_page_token="page-7"),
    ]
    received = []
    stop = asyncio.Event()
    reader = GrpcReader(channel, _metadata)
    token, offline = await reader.read("Cg0KC2Zvbw", "page-1", received.append, stop_event=stop)
    assert token == "page-7"  # server EOF: caller reconnects from here
    assert offline is None
    assert [item["id"] for item in received] == ["a", "b"]
    assert received[0]["snippet"]["textMessageDetails"]["messageText"] == "one"


async def test_grpc_reader_offline_at_captured(grpc_server):
    channel, servicer = grpc_server
    servicer.responses = [
        pb.LiveChatMessageListResponse(offline_at="2026-10-15T11:00:00Z"),
    ]
    token, offline = await GrpcReader(channel, _metadata).read("Cg0KC2Zvbw", None, lambda i: None)
    assert token is None
    assert offline == "2026-10-15T11:00:00Z"


async def test_grpc_reader_stops_on_event(grpc_server):
    channel, servicer = grpc_server
    servicer.responses = [
        pb.LiveChatMessageListResponse(items=[_pb_item("a", "one")]),
    ]
    stop = asyncio.Event()
    stop.set()
    reader = GrpcReader(channel, _metadata)
    token, _ = await reader.read("Cg0KC2Zvbw", None, lambda i: None, stop_event=stop)
    # the call ends before items are delivered (server EOF follows cancel)
    assert token is None


@pytest.mark.parametrize("code,expected_message", [
    (grpc.StatusCode.FAILED_PRECONDITION, "chat ended or disabled"),
    (grpc.StatusCode.NOT_FOUND, "live chat not found"),
    (grpc.StatusCode.UNAUTHENTICATED, "not authorized"),
])
async def test_grpc_reader_terminal_statuses(grpc_server, code, expected_message):
    channel, servicer = grpc_server
    servicer.abort = (code, "nope")
    with pytest.raises(StreamEnded) as exc_info:
        await GrpcReader(channel, _metadata).read("Cg0KC2Zvbw", None, lambda i: None)
    assert expected_message in str(exc_info.value)


async def test_grpc_reader_transient_status_reraises(grpc_server):
    channel, servicer = grpc_server
    servicer.abort = (grpc.StatusCode.RESOURCE_EXHAUSTED, "reconnecting too fast")
    with pytest.raises(grpc.aio.AioRpcError):
        await GrpcReader(channel, _metadata).read("Cg0KC2Zvbw", None, lambda i: None)


# -- REST reader ---------------------------------------------------------------------


def _rest_api(handler):
    return YouTubeApi(
        lambda: _token(),
        QuotaTracker(),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/youtube/v3"))


async def _token():
    return "tok"


def _rest_item(msg_id, text):
    return {
        "id": msg_id,
        "snippet": {
            "type": "textMessageEvent", "liveChatId": "Cg0KC2Zvbw",
            "displayMessage": text,
            "textMessageDetails": {"messageText": text},
        },
        "authorDetails": {"channelId": "UCa", "displayName": "Alice"},
    }


async def test_rest_reader_single_cycle_with_drain():
    pages = 0

    async def handler(request):
        nonlocal pages
        pages += 1
        items = [_rest_item(f"m{pages}", f"msg{pages}")]
        return httpx.Response(200, json={
            "items": items,
            "pollingIntervalMillis": 8000,
            **({"nextPageToken": f"p{pages}"} if pages < 4 else {}),
        }, request=request)

    received = []
    await RestReader(_rest_api(handler)).read("Cg0KC2Zvbw", None, received.append)
    # one poll + the full MAX_DRAIN_PAGES (3) drain pages, then the cap stops it
    assert pages == 4
    assert [item["id"] for item in received] == ["m1", "m2", "m3", "m4"]


async def test_rest_reader_resumes_from_page_token():
    seen_token = {}

    async def handler(request):
        seen_token["token"] = request.url.params.get("pageToken")
        return httpx.Response(200, json={"items": [], "pollingIntervalMillis": 8000},
                              request=request)

    await RestReader(_rest_api(handler)).read("Cg0KC2Zvbw", "saved-token", lambda i: None)
    assert seen_token["token"] == "saved-token"


async def test_rest_reader_paces_by_server_interval():
    polls = 0

    async def handler(request):
        nonlocal polls
        polls += 1
        return httpx.Response(200, json={"items": [], "pollingIntervalMillis": 100},
                              request=request)

    api = _rest_api(handler)
    stop = asyncio.Event()
    task = asyncio.ensure_future(
        RestReader(api).read("Cg0KC2Zvbw", None, lambda i: None,
                             stop_event=stop, min_interval=0.05))
    await asyncio.sleep(0.35)  # interval = max(0.05, 0.1) = 0.1 s -> ~3 polls
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert 2 <= polls <= 6
