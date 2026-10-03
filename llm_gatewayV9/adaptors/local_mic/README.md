# Local-mic adaptor — POINTER (P3)

No mic SDK exists: capture where the microphone lives, POST audio to
`/v1/stt`, play replies from `/v1/tts`. This package documents the split.

## Setup

None. Browser capture needs a mic grant; OS capture needs TCC/device
consent. Transcripts arrive with `owner` trust (physical presence).

## Verify

```bash
curl -s http://localhost:8109/v1/tts/voices | python3 -m json.tool
# voices list = TTS service up; STT needs faster-whisper in the venv.
```
