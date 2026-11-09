import os
import sys
from pathlib import Path

from services.business import Sandbox
from services.orchestrator import Conversation

if __name__ == "__main__":
    folder, session, stage = sys.argv[1:]
    root = Path(folder)

    def clock():
        return 1794042000.0

    conversation = Conversation(root / "app.db", Sandbox(root / "business.db", clock), clock)
    conversation.submit(session, "确认", auto=False)

    def stop_at(point):
        if stage == point:
            os._exit(71)

    stop_at("prepared")
    conversation.advance(session, force=True, barrier=stop_at)
