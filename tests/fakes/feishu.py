"""飞书入口的无网络测试夹具。"""

from collections.abc import Callable

from xiaowei_agent.interfaces.feishu_sdk import FeishuMessageEvent


class RecordingFeishuInboundTransport:
    """同步交付预置事件；模拟 SDK 在 ACK 前等待 callback 返回。"""

    def __init__(self, events: tuple[FeishuMessageEvent, ...] = ()) -> None:
        self.events = events
        self.started = False
        self.callbacks_completed = 0

    def run_forever(
        self, *, on_event: Callable[[FeishuMessageEvent], None]
    ) -> None:
        self.started = True
        for event in self.events:
            on_event(event)
            self.callbacks_completed += 1
