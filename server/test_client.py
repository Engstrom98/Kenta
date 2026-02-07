"""
Test client that simulates an ESP32 sending audio over TCP.

Usage:
    # Send a generated sine wave (tests the pipeline, Whisper will return gibberish)
    python test_client.py

    # Send a real WAV file (tests with actual speech)
    python test_client.py path/to/speech.wav
"""

import audioop
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
    """Read a WAV file and return raw PCM converted to 16kHz, 16-bit, mono."""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        print(
            f"WAV: {channels}ch, {framerate}Hz, "
            f"{sample_width * 8}-bit, {wf.getnframes()} frames"
        )
        pcm = wf.readframes(wf.getnframes())

    if channels > 1:
        pcm = audioop.tomono(pcm, sample_width, 1.0, 1.0)

    if sample_width != 2:
        pcm = audioop.lin2lin(pcm, sample_width, 2)

    if framerate != SAMPLE_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, framerate, SAMPLE_RATE, None)

    print(f"Converted to: 1ch, {SAMPLE_RATE}Hz, 16-bit")
    return pcm


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
