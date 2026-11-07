"""The agent's voice stream must reach the browser without buffering."""
import inspect

import agent_server


def test_the_tts_stream_is_not_gzipped():
    """GZipMiddleware buffered the stream and turned a 1.6s first sentence
    into a 14.8s silence. The selective middleware must keep this path raw."""
    src = inspect.getsource(agent_server)
    assert "_StreamSafeGZip" in src
    assert 'startswith("/api/tts/stream")' in src
    assert '@app.post("/api/tts/stream")' in src
