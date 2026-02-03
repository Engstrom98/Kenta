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

OPENAI_MODEL_CHAT = "gpt-4o"
OPENAI_MODEL_TTS = "gpt-4o-mini-tts"
OPENAI_MODEL_STT = "gpt-4o-mini-transcribe"
OPENAI_TTS_VOICE = "coral"

HISTORY_TIMEOUT = 7200  # seconds (2 hours) of inactivity before clearing history

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
def discover_sonos() -> soco.SoCo:
    """Discover and return the first Sonos speaker on the network."""
    log.info("Discovering Sonos speakers...")
    zones = soco.discover(timeout=10)
    if not zones:
        raise RuntimeError("No Sonos speakers found on the network")
    speaker = list(zones)[0]
    log.info("Found Sonos: %s (%s)", speaker.player_name, speaker.ip_address)
    return speaker


def play_on_sonos(speaker: soco.SoCo, audio_url: str):
    """Tell a Sonos speaker to play audio from *audio_url*."""
    log.info("Playing on Sonos: %s", audio_url)
    speaker.play_uri(audio_url, title="ESP Assistant Response")


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
# Client handler (full pipeline)
# ---------------------------------------------------------------------------
def handle_client(
    conn: socket.socket,
    addr: tuple,
    speaker: soco.SoCo,
    local_ip: str,
):
    """Handle one push-to-talk interaction from the ESP32."""
    log.info("Connection from %s", addr)
    try:
        # 1. Receive PCM audio
        pcm_data = receive_audio(conn)
        if not pcm_data:
            log.warning("No audio data received")
            return

        # 2. Wrap in WAV
        wav_file = pcm_to_wav(pcm_data)

        # 3. Transcribe
        transcription = transcribe_audio(wav_file)
        if not transcription:
            log.warning("Empty transcription, skipping")
            return

        # 4. Chat completion
        reply = chat_completion(transcription)

        # 5. Text-to-speech
        tts_path = text_to_speech(reply)

        # 6. Build URL for Sonos
        tts_filename = os.path.basename(tts_path)
        audio_url = f"http://{local_ip}:{HTTP_PORT}/{tts_filename}"

        # 7. Play on Sonos
        play_on_sonos(speaker, audio_url)

        # 8. Clean up temp file after Sonos has had time to fetch it
        time.sleep(30)
        try:
            os.unlink(tts_path)
        except OSError:
            pass

    except Exception:
        log.error("Error handling client", exc_info=True)
    finally:
        conn.close()
        log.info("Connection from %s closed", addr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    local_ip = get_local_ip()
    log.info("Server LAN IP: %s", local_ip)

    # Start HTTP server for Sonos
    temp_dir = tempfile.gettempdir()
    start_http_server(HTTP_PORT, temp_dir)

    # Discover Sonos
    speaker = discover_sonos()

    # TCP server
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((TCP_HOST, TCP_PORT))
    srv.listen(1)
    log.info("TCP server listening on %s:%d", TCP_HOST, TCP_PORT)

    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, addr, speaker, local_ip),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
