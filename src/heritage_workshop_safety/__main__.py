"""``python -m heritage_workshop_safety`` 启动入口。"""

from __future__ import annotations

import argparse
import os

from .api import serve
from .clock import SystemClock
from .events import EventStore
from .service import SafetyService


def main() -> None:
    parser = argparse.ArgumentParser(description="非遗体验安全放行后端")
    parser.add_argument("--host", default=os.environ.get("HWS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("HWS_PORT", "8080")))
    parser.add_argument("--store", default=os.environ.get("HWS_STORE", "data/events.jsonl"),
                        help="JSONL 事件存储路径，传空字符串则仅用内存")
    args = parser.parse_args()

    store = EventStore(args.store or None, clock=SystemClock())
    service = SafetyService(store)
    serve(service, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
