"""Bridge: a QObject signal bus decoupling async services from UI widgets.

All background coroutines run on the qasync main-thread loop, so direct calls
are safe; the bus exists for decoupled flows (service -> any number of tabs)
and so future executor-thread workers have a thread-safe seam.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    message = Signal(object)  # Message from any platform
    event = Signal(object)  # ClearChat | ClearMsg | RoomState | Names
    connection_state = Signal(object)  # dict(platform, channel_id, state, detail)
    send_result = Signal(object)  # SendResult
    image_ready = Signal(str)  # image url now available
    auth_state = Signal(str, bool, str)  # platform, ok, detail


bridge = Bridge()
