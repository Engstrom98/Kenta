"""
Component-by-component test for the ESP server.

Run all tests:
    python test_components.py

Run a specific test:
    python test_components.py local_ip
    python test_components.py openai_connection
    python test_components.py whisper
    python test_components.py chat
    python test_components.py tts
    python test_components.py http_server
    python test_components.py sonos
    python test_components.py tcp_loopback
    python test_components.py full_pipeline
"""

import socket
import struct
import math
import wave
import io
import os
import sys
import time
import tempfile
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from functools import partial
from urllib.request import urlopen

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
END_MARKER = b"\xDE\xAD\xBE\xEF"
HTTP_PORT = 8731


# -- Helpers ----------------------------------------------------------------

def generate_pcm_sine(freq=440, duration=2.0):
    """Generate a short sine wave as raw 16-bit PCM."""
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        val = int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        samples.append(struct.pack("<h", val))
    return b"".join(samples)


def pcm_to_wav(pcm_data):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    buf.seek(0)
    buf.name = "audio.wav"
    return buf


def header(name):
    print(f"\n{'=' * 60}")
    print(f"  TEST: {name}")
    print(f"{'=' * 60}")


def passed(msg=""):
    print(f"  PASS {msg}")
    return True


def failed(msg=""):
    print(f"  FAIL {msg}")
    return False


# -- Individual tests -------------------------------------------------------

def test_local_ip():
    """Detect LAN IP address."""
    header("Local IP Detection")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception as e:
        return failed(f"Could not detect LAN IP: {e}")
    finally:
        s.close()

    print(f"  LAN IP: {ip}")
    if ip == "127.0.0.1":
        return failed("Got loopback address -- no network route to external hosts")
    return passed()


def test_openai_connection():
    """Verify OpenAI API key and basic connectivity."""
    header("OpenAI API Connection")
    key = os.getenv("OPENAI_API_KEY", "")
    if not key or key.startswith("sk-proj-your"):
        return failed("OPENAI_API_KEY not set in .env")

    print(f"  Key: {key[:12]}...{key[-4:]}")
    try:
        client = OpenAI()
        models = client.models.list()
        print(f"  Connected. Models available: {len(models.data)}")
        return passed()
    except Exception as e:
        return failed(f"API call failed: {e}")


def test_whisper():
    """Generate a WAV and send it to Whisper for transcription."""
    header("Whisper Speech-to-Text")
    print("  Generating 2s sine wave PCM...")
    pcm = generate_pcm_sine(duration=2.0)
    wav = pcm_to_wav(pcm)
    print(f"  WAV size: {wav.getbuffer().nbytes} bytes")

    try:
        client = OpenAI()
        result = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=wav,
        )
        text = result.text.strip()
        print(f"  Transcription: '{text}'")
        print("  (A sine wave may transcribe as silence or gibberish -- that's OK)")
        return passed("Whisper API responded successfully")
    except Exception as e:
        return failed(f"Whisper API error: {e}")


def test_chat():
    """Send a simple message to ChatGPT."""
    header("Chat Completion")
    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": "Say hello."},
            ],
        )
        reply = response.choices[0].message.content.strip()
        print(f"  Reply: '{reply}'")
        return passed()
    except Exception as e:
        return failed(f"Chat API error: {e}")


def test_tts():
    """Generate TTS audio and verify the file is written."""
    header("Text-to-Speech")
    try:
        client = OpenAI()
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False, dir=tempfile.gettempdir()
        )
        tmp_path = tmp.name
        tmp.close()

        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="coral",
            input="Hello, this is a test.",
            response_format="mp3",
        ) as response:
            response.stream_to_file(tmp_path)

        size = os.path.getsize(tmp_path)
        print(f"  File: {tmp_path}")
        print(f"  Size: {size} bytes")
        os.unlink(tmp_path)

        if size > 0:
            return passed()
        return failed("TTS file is empty")
    except Exception as e:
        return failed(f"TTS API error: {e}")


def test_http_server():
    """Start the HTTP server and verify a file can be fetched."""
    header("HTTP File Server")
    temp_dir = tempfile.gettempdir()

    # Write a test file
    test_file = os.path.join(temp_dir, "_esp_test_http.txt")
    with open(test_file, "w") as f:
        f.write("http test ok")

    handler = partial(SimpleHTTPRequestHandler, directory=temp_dir)
    httpd = HTTPServer(("", HTTP_PORT), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"  HTTP server started on port {HTTP_PORT}")

    # Detect local IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        s.close()

    url = f"http://{local_ip}:{HTTP_PORT}/_esp_test_http.txt"
    print(f"  Fetching: {url}")

    try:
        resp = urlopen(url, timeout=5)
        body = resp.read().decode()
        print(f"  Response: '{body}'")
        ok = body == "http test ok"
    except Exception as e:
        print(f"  Could not fetch from HTTP server: {e}")
        print("  This may be a firewall issue on your company network.")
        ok = False
    finally:
        httpd.shutdown()
        os.unlink(test_file)

    return passed() if ok else failed("HTTP server unreachable")


def test_sonos():
    """Discover Sonos speakers on the network."""
    header("Sonos Discovery")
    try:
        import soco
    except ImportError:
        return failed("soco not installed -- run: pip install soco")

    print("  Scanning network (timeout 10s)...")
    try:
        zones = soco.discover(timeout=10, allow_network_scan=True)
    except Exception as e:
        return failed(f"Discovery error: {e}")

    if not zones:
        print("  No Sonos speakers found.")
        print("  Possible causes on company WiFi:")
        print("    - Client isolation (devices can't see each other)")
        print("    - Multicast/SSDP blocked")
        print("    - Sonos on a different VLAN/subnet")
        return failed("No speakers discovered")

    for z in zones:
        print(f"  Found: {z.player_name} ({z.ip_address})")
    return passed(f"{len(zones)} speaker(s) found")


def test_tcp_loopback():
    """Start a TCP server, connect locally, send PCM + end marker, verify receipt."""
    header("TCP Loopback (send/receive)")
    port = 12346  # different port to avoid conflict with running server

    pcm = generate_pcm_sine(duration=1.0)
    received = bytearray()
    server_ready = threading.Event()

    def server_thread():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        server_ready.set()
        conn, _ = srv.accept()
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
        conn.close()
        srv.close()

    t = threading.Thread(target=server_thread, daemon=True)
    t.start()
    server_ready.wait()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port))
    sock.sendall(pcm)
    sock.sendall(END_MARKER)
    sock.close()
    t.join(timeout=3)

    # Verify
    if len(received) == 0:
        return failed("No data received")

    if received[-4:] != END_MARKER:
        return failed("End marker not found at end of received data")

    received_pcm = bytes(received[:-4])
    if received_pcm == pcm:
        print(f"  Sent and received {len(pcm)} bytes + 4-byte marker")
        return passed("PCM data matches")
    else:
        return failed(f"Data mismatch: sent {len(pcm)}, got {len(received_pcm)}")


def test_full_pipeline():
    """Run the full pipeline without Sonos -- just saves TTS to a local file."""
    header("Full Pipeline (no Sonos)")
    print("  Generating 2s sine wave...")
    pcm = generate_pcm_sine(duration=2.0)
    wav = pcm_to_wav(pcm)

    client = OpenAI()

    # Whisper
    print("  [1/3] Transcribing...")
    try:
        result = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe", file=wav
        )
        text = result.text.strip()
        print(f"         Transcription: '{text}'")
    except Exception as e:
        return failed(f"Whisper failed: {e}")

    if not text:
        text = "Hello, can you hear me?"
        print(f"         Empty transcription, using fallback: '{text}'")

    # Chat
    print("  [2/3] Chat completion...")
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": text},
            ],
        )
        reply = response.choices[0].message.content.strip()
        print(f"         Reply: '{reply}'")
    except Exception as e:
        return failed(f"Chat failed: {e}")

    # TTS
    print("  [3/3] Generating TTS...")
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False, dir=tempfile.gettempdir()
        )
        tmp_path = tmp.name
        tmp.close()

        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="coral",
            input=reply,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(tmp_path)

        size = os.path.getsize(tmp_path)
        print(f"         TTS file: {tmp_path} ({size} bytes)")
        # Don't delete -- let the user listen to it if they want
        print(f"         (File kept so you can play it manually)")
    except Exception as e:
        return failed(f"TTS failed: {e}")

    return passed("Whisper -> Chat -> TTS completed successfully")


# -- Runner -----------------------------------------------------------------

ALL_TESTS = {
    "local_ip": test_local_ip,
    "openai_connection": test_openai_connection,
    "whisper": test_whisper,
    "chat": test_chat,
    "tts": test_tts,
    "http_server": test_http_server,
    "sonos": test_sonos,
    "tcp_loopback": test_tcp_loopback,
    "full_pipeline": test_full_pipeline,
}


def main():
    if len(sys.argv) > 1:
        name = sys.argv[1]
        if name not in ALL_TESTS:
            print(f"Unknown test: {name}")
            print(f"Available: {', '.join(ALL_TESTS)}")
            sys.exit(1)
        ok = ALL_TESTS[name]()
        sys.exit(0 if ok else 1)

    # Run all
    results = {}
    for name, fn in ALL_TESTS.items():
        try:
            results[name] = fn()
        except Exception as e:
            print(f"  FAIL (unhandled exception: {e})")
            results[name] = False

    # Summary
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")

    total = len(results)
    passing = sum(1 for v in results.values() if v)
    print(f"\n  {passing}/{total} tests passed")


if __name__ == "__main__":
    main()
