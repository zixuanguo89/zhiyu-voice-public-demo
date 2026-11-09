"""Dedicated WebRTC loopback server with test-only speech providers."""
import asyncio
import math
import struct

import uvicorn

from services import media
from services.api import create_app
from services.config import Settings


async def recognize(_provider, queue, on_text, on_ready=None):
    if on_ready:
        on_ready()
    script = ["我叫林小满", "手机号是19900000991", "我是本人，我想预约办理 Visa 借记卡",
              "我选第一个可用时段", "改成第二个时段", "确认预约"]
    for utterance in script:
        # Require incoming RTP media before advancing. The mock transcript is predefined.
        for _ in range(38):
            if await queue.get() is None:
                return
        await on_text(utterance, False, True)
        for _ in range(3):
            if await queue.get() is None:
                return
        await on_text(utterance, True, True)
    while await queue.get() is not None:
        pass


async def synthesize(_provider, _voice, _text):
    tone = struct.pack("<480h", *(round(350 * math.sin(n * math.tau * 200 / 24000)) for n in range(480)))
    for _ in range(160):
        yield tone
        await asyncio.sleep(0.01)


if __name__ == "__main__":
    config = Settings()
    if config.host != "127.0.0.1" or config.port == 8765:
        raise SystemExit("Run this test server on a separate loopback port")
    config.audio = True
    media.recognize = recognize
    media.synthesize = synthesize
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)
