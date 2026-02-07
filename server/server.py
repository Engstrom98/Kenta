import argparse
import queue
import socket
import wave
import io
import os
import time
import logging
import threading
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from functools import partial

from dotenv import load_dotenv
from openai import OpenAI
import soco

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TCP_HOST = "0.0.0.0"
TCP_PORT = 12345
HTTP_PORT = 8731

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
CHANNELS = 1
END_MARKER = b"\xDE\xAD\xBE\xEF"
DONE_BYTE = b"\x01"

OPENAI_MODEL_CHAT = "gpt-4o"
OPENAI_MODEL_TTS = "gpt-4o-mini-tts"
OPENAI_MODEL_STT = "gpt-4o-mini-transcribe"
OPENAI_TTS_VOICE = "onyx"

HISTORY_TIMEOUT = 7200  # seconds (2 hours) of inactivity before clearing history

SONOS_SPEAKER_NAME = "Sovrum"

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Keep your responses concise and "
    "conversational, suitable for being spoken aloud. Aim for 1-3 sentences "
    "unless the user asks for detail."
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("esp_server")

# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------
client = OpenAI()

# ---------------------------------------------------------------------------
# Conversation history (cleared after HISTORY_TIMEOUT of inactivity)
# ---------------------------------------------------------------------------
conversation_history: list[dict] = []
last_message_time: float = 0.0
history_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    """Return the LAN IP address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def pcm_to_wav(pcm_data: bytes) -> io.BytesIO:
    """Wrap raw PCM bytes in a WAV container (in-memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    buf.seek(0)
    buf.name = "audio.wav"  # OpenAI SDK needs a .name with audio extension
    return buf


# ---------------------------------------------------------------------------
# OpenAI: Speech-to-Text
# ---------------------------------------------------------------------------
def transcribe_audio(wav_file: io.BytesIO) -> str:
    """Send WAV audio to OpenAI Whisper and return the transcription."""
    log.info("Transcribing audio...")
    result = client.audio.transcriptions.create(
        model=OPENAI_MODEL_STT,
        file=wav_file,
    )
    text = result.text.strip()
    log.info("Transcription: %s", text)
    return text


# ---------------------------------------------------------------------------
# OpenAI: Chat Completion
# ---------------------------------------------------------------------------
def chat_completion(user_text: str) -> str:
    """Send user text to ChatGPT and return the assistant reply."""
    global last_message_time

    log.info("Getting chat completion...")

    with history_lock:
        now = time.time()
        if last_message_time and now - last_message_time > HISTORY_TIMEOUT:
            log.info("Conversation inactive for >%ds — clearing history", HISTORY_TIMEOUT)
            conversation_history.clear()
        conversation_history.append({"role": "user", "content": user_text})
        last_message_time = now
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *conversation_history,
        ]

    response = client.chat.completions.create(
        model=OPENAI_MODEL_CHAT,
        messages=messages,
    )
    reply = response.choices[0].message.content.strip()

    with history_lock:
        conversation_history.append({"role": "assistant", "content": reply})

    log.info("Chat reply: %s", reply)
    return reply


# ---------------------------------------------------------------------------
# OpenAI: Text-to-Speech
# ---------------------------------------------------------------------------
def text_to_speech(text: str) -> str:
    """Convert text to speech via OpenAI TTS. Returns path to the MP3 file."""
    log.info("Generating TTS audio...")
    tmp = tempfile.NamedTemporaryFile(
        suffix=".mp3", delete=False, dir=tempfile.gettempdir()
    )
    tmp_path = tmp.name
    tmp.close()

    with client.audio.speech.with_streaming_response.create(
        model=OPENAI_MODEL_TTS,
        voice=OPENAI_TTS_VOICE,
        input=text,
        response_format="mp3",
    ) as response:
        response.stream_to_file(tmp_path)

    log.info("TTS audio saved to %s", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# HTTP server (serves TTS files to Sonos)
# ---------------------------------------------------------------------------
class _TtsHandler(SimpleHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except BrokenPipeError:
            log.debug("HTTP: client disconnected (broken pipe)")

    def log_message(self, format, *args):
        log.debug("HTTP: %s", format % args)


def start_http_server(port: int, directory: str) -> HTTPServer:
    """Start a background HTTP server that serves files from *directory*."""
    handler = partial(_TtsHandler, directory=directory)
    httpd = HTTPServer(("", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log.info("HTTP server started on port %d, serving %s", port, directory)
    return httpd


# ---------------------------------------------------------------------------
# Sonos
# ---------------------------------------------------------------------------
def discover_sonos(ip: str | None = None) -> soco.SoCo:
    """Discover Sonos speakers and let the user choose one.

    If *ip* is provided, skip discovery and connect directly.
    """
    if ip:
        speaker = soco.SoCo(ip)
        log.info("Using Sonos speaker at %s (%s)", speaker.ip_address, speaker.player_name)
        return speaker

    log.info("Discovering Sonos speakers...")
    zones = soco.discover(timeout=10, allow_network_scan=True)
    if not zones:
        raise RuntimeError(
            "No Sonos speakers found on the network. "
            "Try passing --ip <speaker-ip> to connect directly."
        )

    speakers = sorted(zones, key=lambda s: s.player_name)

    # Auto-select preferred speaker if configured
    if SONOS_SPEAKER_NAME:
        for s in speakers:
            if s.player_name == SONOS_SPEAKER_NAME:
                log.info("Auto-selected Sonos speaker: %s (%s)", s.player_name, s.ip_address)
                return s
        log.warning("Preferred speaker '%s' not found among: %s",
                     SONOS_SPEAKER_NAME,
                     ", ".join(s.player_name for s in speakers))

    if len(speakers) == 1:
        speaker = speakers[0]
        log.info("Found one Sonos speaker: %s (%s)", speaker.player_name, speaker.ip_address)
        return speaker

    print("\nAvailable Sonos speakers:")
    for i, s in enumerate(speakers, 1):
        print(f"  {i}. {s.player_name} ({s.ip_address})")

    while True:
        try:
            choice = int(input(f"\nSelect a speaker [1-{len(speakers)}]: "))
            if 1 <= choice <= len(speakers):
                break
        except (ValueError, EOFError):
            pass
        print("Invalid choice, try again.")

    speaker = speakers[choice - 1]
    log.info("Selected Sonos: %s (%s)", speaker.player_name, speaker.ip_address)
    return speaker


def play_on_sonos(speaker: soco.SoCo, audio_url: str):
    """Tell a Sonos speaker to play audio from *audio_url*."""
    log.info("Playing on Sonos: %s", audio_url)
    speaker.play_uri(audio_url, title="ESP Assistant Response")


def wait_for_sonos_done(speaker: soco.SoCo, timeout: int = 120):
    """Poll Sonos transport state until playback finishes or *timeout* expires."""
    deadline = time.time() + timeout
    # Give Sonos a moment to start playing
    time.sleep(1)
    while time.time() < deadline:
        try:
            info = speaker.get_current_transport_info()
            state = info.get("current_transport_state", "")
            if state in ("STOPPED", "PAUSED_PLAYBACK", "NO_MEDIA_PRESENT"):
                log.info("Sonos playback finished (state=%s)", state)
                return
        except Exception:
            log.warning("Error polling Sonos transport state", exc_info=True)
        time.sleep(0.5)
    log.warning("Sonos playback poll timed out after %ds", timeout)


# ---------------------------------------------------------------------------
# TCP: receive audio from ESP32
# ---------------------------------------------------------------------------
def receive_audio(conn: socket.socket) -> bytes:
    """Receive raw PCM audio until the end marker is detected."""
    buf = bytearray()
    while True:
        try:
            chunk = conn.recv(4096)
        except ConnectionResetError:
            log.warning("Client connection reset")
            return bytes()
        if not chunk:
            log.warning("Client disconnected before sending end marker")
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) >= 4 and buf[-4:] == END_MARKER:
            pcm = bytes(buf[:-4])
            duration = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
            log.info("End marker received. PCM: %d bytes (%.1fs)", len(pcm), duration)
            return pcm


# ---------------------------------------------------------------------------
# Audio queue and processing pipeline
# ---------------------------------------------------------------------------
audio_queue: queue.Queue[tuple[bytes, socket.socket]] = queue.Queue()


def _send_done_and_close(conn: socket.socket):
    """Send the done byte and close the connection."""
    try:
        conn.sendall(DONE_BYTE)
    except Exception:
        log.warning("Failed to send done byte", exc_info=True)
    finally:
        conn.close()


def receiver_thread(conn: socket.socket, addr: tuple):
    """Receive audio from ESP32 and put it on the queue."""
    log.info("Connection from %s", addr)
    try:
        pcm_data = receive_audio(conn)
        if pcm_data:
            audio_queue.put((pcm_data, conn))
        else:
            log.warning("No audio data received from %s", addr)
            conn.close()
    except Exception:
        log.error("Error receiving audio from %s", addr, exc_info=True)
        conn.close()


def processor_loop(speaker: soco.SoCo, local_ip: str):
    """Single-threaded loop that processes audio from the queue one at a time."""
    while True:
        pcm_data, conn = audio_queue.get()

        try:
            # 1. Transcribe
            wav_file = pcm_to_wav(pcm_data)
            transcription = transcribe_audio(wav_file)
            if not transcription:
                log.warning("Empty transcription, skipping")
                _send_done_and_close(conn)
                continue

            # 2. Chat completion
            reply = chat_completion(transcription)

            # 3. Text-to-speech
            tts_path = text_to_speech(reply)

            # 4. Play on Sonos
            tts_filename = os.path.basename(tts_path)
            audio_url = f"http://{local_ip}:{HTTP_PORT}/{tts_filename}"
            play_on_sonos(speaker, audio_url)

            # 5. Wait for Sonos to finish playing
            wait_for_sonos_done(speaker)

            # 6. Signal ESP32 that we're done
            _send_done_and_close(conn)

            # 7. Clean up temp file
            try:
                os.unlink(tts_path)
            except OSError:
                pass

        except Exception:
            log.error("Error processing audio", exc_info=True)
            _send_done_and_close(conn)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ESP32 voice assistant server")
    parser.add_argument("--ip", help="Sonos speaker IP (skip discovery)")
    args = parser.parse_args()

    local_ip = get_local_ip()
    log.info("Server LAN IP: %s", local_ip)

    # Start HTTP server for Sonos
    temp_dir = tempfile.gettempdir()
    start_http_server(HTTP_PORT, temp_dir)

    # Discover Sonos
    speaker = discover_sonos(args.ip)

    # Start the single-threaded processor
    threading.Thread(
        target=processor_loop,
        args=(speaker, local_ip),
        daemon=True,
    ).start()

    # TCP server — accepts connections and queues audio
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((TCP_HOST, TCP_PORT))
    srv.listen(5)
    log.info("TCP server listening on %s:%d", TCP_HOST, TCP_PORT)

    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(
                target=receiver_thread,
                args=(conn, addr),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
