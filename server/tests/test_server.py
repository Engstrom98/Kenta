"""Offline tests for the Kenta server — no API keys or hardware needed."""

import io
import math
import os
import socket
import struct
import tempfile
import threading
import time
import wave
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import urlopen

import pytest

# ---------------------------------------------------------------------------
# Import server module (avoid triggering OpenAI client at import time)
# ---------------------------------------------------------------------------
import importlib.util
import sys
import types
from pathlib import Path

# Stub out heavy dependencies so importing server.py doesn't require API keys
# or network hardware at test time.
_fake_openai = types.ModuleType("openai")
_fake_openai.OpenAI = lambda *a, **kw: None  # type: ignore[attr-defined]
sys.modules.setdefault("openai", _fake_openai)

_fake_soco = types.ModuleType("soco")
_fake_soco.SoCo = lambda *a, **kw: None  # type: ignore[attr-defined]
_fake_soco.discover = lambda *a, **kw: None  # type: ignore[attr-defined]
sys.modules.setdefault("soco", _fake_soco)

_fake_zeroconf = types.ModuleType("zeroconf")
_fake_zeroconf.Zeroconf = lambda *a, **kw: None  # type: ignore[attr-defined]
_fake_zeroconf.ServiceInfo = lambda *a, **kw: None  # type: ignore[attr-defined]
sys.modules.setdefault("zeroconf", _fake_zeroconf)

_fake_dotenv = types.ModuleType("dotenv")
_fake_dotenv.load_dotenv = lambda *a, **kw: None  # type: ignore[attr-defined]
sys.modules.setdefault("dotenv", _fake_dotenv)

# Import server.py by file path (server/ is not a Python package)
_server_path = Path(__file__).resolve().parent.parent / "server.py"
_spec = importlib.util.spec_from_file_location("srv", _server_path)
srv = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(srv)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Constants (mirror server values for clarity)
# ---------------------------------------------------------------------------
SAMPLE_RATE = srv.SAMPLE_RATE  # 16000
SAMPLE_WIDTH = srv.SAMPLE_WIDTH  # 2
CHANNELS = srv.CHANNELS  # 1
END_MARKER = srv.END_MARKER  # b"\xDE\xAD\xBE\xEF"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def generate_pcm_sine(freq: float = 440, duration: float = 1.0) -> bytes:
    """Generate a sine wave as raw 16-bit LE PCM."""
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        val = int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        samples.append(struct.pack("<h", val))
    return b"".join(samples)


# ---------------------------------------------------------------------------
# pcm_to_wav
# ---------------------------------------------------------------------------
class TestPcmToWav:
    def test_valid_header(self):
        """pcm_to_wav() produces a WAV with correct sample rate, channels, sample width."""
        pcm = generate_pcm_sine(duration=0.1)
        wav_buf = srv.pcm_to_wav(pcm)

        with wave.open(wav_buf, "rb") as wf:
            assert wf.getnchannels() == CHANNELS
            assert wf.getsampwidth() == SAMPLE_WIDTH
            assert wf.getframerate() == SAMPLE_RATE

    def test_roundtrip(self):
        """PCM data survives WAV encode then decode."""
        pcm = generate_pcm_sine(duration=0.5)
        wav_buf = srv.pcm_to_wav(pcm)

        with wave.open(wav_buf, "rb") as wf:
            decoded = wf.readframes(wf.getnframes())

        assert decoded == pcm


# ---------------------------------------------------------------------------
# generate_pcm_sine helper
# ---------------------------------------------------------------------------
class TestGeneratePcmSine:
    def test_length(self):
        """Sine generator produces the correct number of bytes for a given duration."""
        duration = 2.0
        pcm = generate_pcm_sine(duration=duration)
        expected = int(SAMPLE_RATE * duration) * SAMPLE_WIDTH
        assert len(pcm) == expected

    def test_value_range(self):
        """All sample values stay within int16 range."""
        pcm = generate_pcm_sine(duration=0.5)
        for i in range(0, len(pcm), SAMPLE_WIDTH):
            value = struct.unpack("<h", pcm[i : i + SAMPLE_WIDTH])[0]
            assert -32768 <= value <= 32767


# ---------------------------------------------------------------------------
# receive_audio
# ---------------------------------------------------------------------------
class TestReceiveAudio:
    @staticmethod
    def _make_socketpair() -> tuple[socket.socket, socket.socket]:
        """Create a connected TCP socket pair via loopback."""
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(1)
        port = srv_sock.getsockname()[1]

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        conn, _ = srv_sock.accept()
        srv_sock.close()
        return conn, client

    def test_end_marker(self):
        """receive_audio() correctly strips the 0xDEADBEEF end marker."""
        conn, client = self._make_socketpair()
        pcm = generate_pcm_sine(duration=0.1)

        def send():
            client.sendall(pcm + END_MARKER)
            client.close()

        threading.Thread(target=send, daemon=True).start()
        result = srv.receive_audio(conn)
        conn.close()

        assert result == pcm

    def test_empty(self):
        """Handles client disconnect (no data) gracefully."""
        conn, client = self._make_socketpair()
        client.close()  # immediate disconnect

        result = srv.receive_audio(conn)
        conn.close()

        assert result == b""


# ---------------------------------------------------------------------------
# TCP loopback
# ---------------------------------------------------------------------------
def test_tcp_loopback():
    """Send PCM + end marker over TCP, verify data integrity on the other side."""
    pcm = generate_pcm_sine(duration=0.5)
    received = bytearray()
    server_ready = threading.Event()

    def server_fn():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        server_ready.port = s.getsockname()[1]  # type: ignore[attr-defined]
        server_ready.set()
        conn, _ = s.accept()
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
        conn.close()
        s.close()

    t = threading.Thread(target=server_fn, daemon=True)
    t.start()
    server_ready.wait(timeout=3)

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", server_ready.port))  # type: ignore[attr-defined]
    client.sendall(pcm + END_MARKER)
    client.close()
    t.join(timeout=3)

    assert received[-4:] == END_MARKER
    assert bytes(received[:-4]) == pcm


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
def test_http_server():
    """Start an HTTP server, fetch a file, verify its content."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Write a test .mp3 file (content doesn't matter, just needs .mp3 ext)
        test_content = b"fake mp3 content for testing"
        test_path = os.path.join(tmp_dir, "test.mp3")
        with open(test_path, "wb") as f:
            f.write(test_content)

        handler = partial(SimpleHTTPRequestHandler, directory=tmp_dir)
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        try:
            url = f"http://127.0.0.1:{port}/test.mp3"
            resp = urlopen(url, timeout=5)
            body = resp.read()
            assert body == test_content
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# Conversation history timeout
# ---------------------------------------------------------------------------
def test_conversation_history_timeout(monkeypatch):
    """History clears after the inactivity threshold is exceeded."""
    # Reset history state
    srv.conversation_history.clear()
    srv.last_message_time = 0.0

    # Pretend a message was sent long ago
    srv.conversation_history.append({"role": "user", "content": "old message"})
    srv.conversation_history.append({"role": "assistant", "content": "old reply"})
    srv.last_message_time = time.time() - srv.HISTORY_TIMEOUT - 1

    # Stub out the OpenAI API call
    class FakeChoice:
        class message:
            content = "mocked reply"

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(srv, "client", FakeClient())

    reply = srv.chat_completion("new message")

    assert reply == "mocked reply"
    # History should contain only the new exchange (old ones cleared)
    assert len(srv.conversation_history) == 2
    assert srv.conversation_history[0]["content"] == "new message"
    assert srv.conversation_history[1]["content"] == "mocked reply"
