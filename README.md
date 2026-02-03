# Kenta

I read a lot at night. Non-fiction mostly. And when I come across something I don't fully understand or need translated, my instinct is to reach for my phone. But I keep my phone outside the bedroom on purpose -- screen time before sleep is a battle I'd rather not fight.

So the question became: what if I had a small dedicated device on my nightstand that I could just talk to? Ask it to translate a word, explain a concept, or answer a quick question about what I'm reading -- and hear the answer out loud through a speaker already in the room.

Yes, this is basically what an Alexa does. But building it myself means I actually learn something. How TCP connections work beyond the textbook version. How audio is represented as raw bytes -- PCM, sample rates, WAV headers. How to stitch together speech-to-text, an LLM, and text-to-speech into something that feels like a conversation. Those are things I wanted to understand at a lower level, and wrapping them in a product I actually use every night was the best excuse to do it.

## How it works

```
                        push-to-talk
                        ┌──────────┐
                        │  ESP32   │
                        │ + INMP441│
                        │   mic    │
                        └────┬─────┘
                             │ raw PCM audio over TCP
                             ▼
                     ┌───────────────┐
                     │ Python server │
                     │               │
                     │  1. Whisper   │──→ speech to text
                     │  2. GPT-4o   │──→ generate response
                     │  3. TTS      │──→ text to speech
                     │  4. HTTP     │──→ serve audio file
                     └───────┬──────┘
                             │ play_uri
                             ▼
                     ┌───────────────┐
                     │ Sonos speaker │
                     └───────────────┘
```

The ESP32 with an INMP441 microphone listens while I hold a button. When I release it, the recorded audio gets streamed over TCP to a Python server running on my local network. The server sends the audio to OpenAI's Whisper for transcription, passes that text to GPT-4o for a response, converts the reply to speech using OpenAI's TTS, and plays it through my Sonos speaker.

One push-to-talk interaction is one TCP connection. The ESP32 streams 16-bit PCM audio at 16kHz, terminates with a 4-byte end marker, and disconnects. The server handles the rest.

## What I've learned so far

The OpenAI API surprised me. I expected the Whisper and TTS integration to require more work -- dealing with audio formats, chunking, special handling. In practice it's a few lines of code: hand it a WAV file, get text back. Hand it text, get an MP3 back. The hard part isn't the API, it's everything around it: getting audio off a microphone as raw bytes, framing a TCP stream so the server knows when you're done talking, and serving a file over HTTP in a format a Sonos speaker will accept.

## What's next

- Wake word detection so I don't need to press a button
- Conversation context that persists across sessions
- Support for multiple Sonos speakers / rooms
