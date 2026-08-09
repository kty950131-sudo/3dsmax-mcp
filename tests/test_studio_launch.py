import sys
import types

from src.ui.studio import _session
from src.ui.studio import launch


class ExistingWindow:
    def __init__(self):
        self.bridge = object()
        self.calls = []

    def show(self):
        self.calls.append("show")

    def raise_(self):
        self.calls.append("raise")

    def replace_bridge(self, bridge):
        self.bridge = bridge
        self.calls.append("replace_bridge")

    def load_page(self, page):
        self.calls.append(("load_page", page))


def test_relaunch_replaces_stale_bridge_before_loading_page(monkeypatch):
    class FreshBridge:
        def __init__(self, cache_dir):
            self.cache_dir = cache_dir

    window = ExistingWindow()
    monkeypatch.setattr(_session, "window", window)
    monkeypatch.setattr(launch, "_cache_dir", lambda: "cache")
    monkeypatch.setitem(
        sys.modules,
        "src.ui.studio.bridge",
        types.SimpleNamespace(StudioBridge=FreshBridge),
    )

    result = launch.launch()

    assert result is window
    assert isinstance(window.bridge, FreshBridge)
    assert window.calls == [
        "show",
        "raise",
        "replace_bridge",
        ("load_page", launch.PAGE),
    ]
