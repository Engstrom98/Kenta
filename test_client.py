"""
Test client that simulates an ESP32 sending audio over TCP.

Usage:
    # Send a generated sine wave (tests the pipeline, Whisper will return gibberish)
    python test_client.py

    # Send a real WAV file (tests with actual speech)
    python test_client.py path/to/speech.wav
"""

import socket
import struct
import math
import sys
import wave

SERVER_IP = "127.0.0.1"
SERVER_PORT = 12345
SAMPLE_RATE = 16000
END_MARKER = b"\xDE\xAD\xBE\xEF"


def generate_sine_wave(freq: float = 440, duration: float = 3.0) -> bytes:
    """Generate a sine wave as 16-bit little-endian PCM."""
    samples = []
    for i in range(int(SAMPLE_RATE * duration)):
        val = int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        samples.append(struct.pack("<h", val))
    return b"".join(samples)


def load_wav_pcm(path: str) -> bytes:
    """Read a WAV file and return its raw PCM frames."""
    with wave.open(path, "rb") as wf:
        print(
            f"WAV: {wf.getnchannels()}ch, {wf.getframerate()}Hz, "
            f"{wf.getsampwidth() * 8}-bit, {wf.getnframes()} frames"
        )
        return wf.readframes(wf.getnframes())


def main():
    if len(sys.argv) > 1:
        wav_path = sys.argv[1]
        print(f"Loading PCM from {wav_path}")
        pcm = load_wav_pcm(wav_path)
    else:
        duration = 3.0
        print(f"Generating {duration}s sine wave (440 Hz)")
        pcm = generate_sine_wave(duration=duration)

    print(f"Sending {len(pcm)} bytes of PCM to {SERVER_IP}:{SERVER_PORT}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.sendall(pcm)
    sock.sendall(END_MARKER)
    print("End marker sent. Done.")
    sock.close()


if __name__ == "__main__":
    main()
