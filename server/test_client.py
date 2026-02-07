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


def _stereo_to_mono(pcm: bytes, sample_width: int) -> bytes:
    """Convert stereo PCM to mono by averaging left and right channels."""
    if sample_width == 1:
        fmt = "B"  # unsigned 8-bit
        offset = 128
    elif sample_width == 2:
        fmt = "<h"
        offset = 0
    elif sample_width == 4:
        fmt = "<i"
        offset = 0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    frame_size = sample_width * 2  # stereo = 2 channels
    num_frames = len(pcm) // frame_size
    out = []
    for i in range(num_frames):
        pos = i * frame_size
        left = struct.unpack_from(fmt, pcm, pos)[0] - offset
        right = struct.unpack_from(fmt, pcm, pos + sample_width)[0] - offset
        mono = (left + right) // 2 + offset
        out.append(struct.pack(fmt, mono))
    return b"".join(out)


def _convert_sample_width(pcm: bytes, from_width: int, to_width: int) -> bytes:
    """Convert PCM between sample widths (1/2/4 bytes)."""
    if from_width == to_width:
        return pcm

    # Read samples
    if from_width == 1:
        samples = [(b - 128) << 24 for b in pcm]  # 8-bit unsigned -> 32-bit signed
    elif from_width == 2:
        num = len(pcm) // 2
        samples = [s << 16 for s in struct.unpack(f"<{num}h", pcm)]
    elif from_width == 4:
        num = len(pcm) // 4
        samples = list(struct.unpack(f"<{num}i", pcm))
    else:
        raise ValueError(f"Unsupported source sample width: {from_width}")

    # Write samples
    if to_width == 1:
        return bytes((s >> 24) + 128 for s in samples)
    elif to_width == 2:
        return struct.pack(f"<{len(samples)}h", *[s >> 16 for s in samples])
    elif to_width == 4:
        return struct.pack(f"<{len(samples)}i", *samples)
    else:
        raise ValueError(f"Unsupported target sample width: {to_width}")


def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM using linear interpolation."""
    num_samples = len(pcm) // 2
    if num_samples == 0:
        return pcm
    samples = struct.unpack(f"<{num_samples}h", pcm)

    ratio = from_rate / to_rate
    out_len = int(num_samples / ratio)
    out = []
    for i in range(out_len):
        src_pos = i * ratio
        idx = int(src_pos)
        frac = src_pos - idx
        if idx + 1 < num_samples:
            val = samples[idx] * (1.0 - frac) + samples[idx + 1] * frac
        else:
            val = samples[idx] if idx < num_samples else 0
        out.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(out)}h", *out)


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
        pcm = _stereo_to_mono(pcm, sample_width)

    if sample_width != 2:
        pcm = _convert_sample_width(pcm, sample_width, 2)

    if framerate != SAMPLE_RATE:
        pcm = _resample(pcm, framerate, SAMPLE_RATE)

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
