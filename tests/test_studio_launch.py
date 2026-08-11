from maxmcp.ui.studio import _session
from maxmcp.ui.studio import launch


class ExistingWindow:
    def __init__(self):
        self.bridge = OldBridge()
        self._channel = Channel()
        self.calls = []

    def show(self):
        self.calls.append("show")

    def raise_(self):
        self.calls.append("raise")

    def load_page(self, page):
        self.calls.append(("load_page", page))


class OldBridge:
    def __init__(self):
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


class Channel:
    def __init__(self):
        self.calls = []

    def deregisterObject(self, bridge):
        self.calls.append(("deregister", bridge))

    def registerObject(self, name, bridge):
        self.calls.append(("register", name, bridge))


def test_relaunch_replaces_stale_bridge_before_loading_page(monkeypatch):
    class FreshBridge:
        def __init__(self, cache_dir):
            self.cache_dir = cache_dir

        def setParent(self, parent):
            self.parent = parent

    window = ExistingWindow()
    monkeypatch.setattr(_session, "window", window)
    monkeypatch.setattr(launch, "_new_bridge", lambda: FreshBridge("cache"))

    result = launch.launch()

    assert result is window
    assert isinstance(window.bridge, FreshBridge)
    assert window.bridge.parent is window
    assert window.calls == [
        "show",
        "raise",
        ("load_page", launch.PAGE),
    ]
    assert [call[0] for call in window._channel.calls] == ["deregister", "register"]
