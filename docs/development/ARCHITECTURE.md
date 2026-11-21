# Architecture

```text
custom_components/soniox/
├── __init__.py        # Entry setup: builds the client, fetches the catalog, forwards platforms
├── api/client.py      # Soniox client: REST catalog via the SDK, realtime STT/TTS websockets
├── config_flow.py     # User, reauth and reconfigure flows; options flow
├── const.py           # Domain, option keys, default models, regions
├── data.py            # Runtime data (client + catalog) and the typed config entry
├── diagnostics.py     # Config entry diagnostics with the API key redacted
├── entity.py          # Base entity: shared service device, translation key, unique ID
├── stt/__init__.py    # Speech-to-text entity
├── tts/__init__.py    # Text-to-speech entity
├── utils.py           # Language tag helper
└── translations/en.json
```

## Setup

`async_setup_entry` builds one `SonioxApiClient` for the entry's API key and region, registers its `async_close` with `entry.async_on_unload`, and calls `async_get_catalog()`. The catalog holds the chosen STT model and its languages, and the chosen TTS model with its languages and voices. A rejected key raises `ConfigEntryAuthFailed` (starts reauth); other failures raise `ConfigEntryNotReady`. There is no polling: entities read the catalog once and call the client per request.

## Speech-to-text

Assist streams 16 kHz mono PCM. `async_transcribe` opens the realtime STT websocket, sends the config, forwards audio, then sends an empty frame to mark the end. Final tokens are joined into the transcript; an empty transcript is returned as a success so the pipeline reports "no text recognized". `language_hints_strict` is only sent if the model supports it. Audio sending and event receiving run as two tasks; a failure in either stops both.

## Text-to-speech

The entity implements only `async_stream_tts_audio`. Home Assistant routes every request through it, wrapping plain `tts.speak` messages in a one-item stream. Output is MP3.

`_TtsSession` in `api/client.py` speaks the text through consecutive Soniox streams on one websocket, one stream at a time:

- A reader task copies the text source into a queue, so the sender can wait on it with a timeout without cancelling the source.
- The sender opens a stream on the first non-blank text. It ends the stream with `text_end` after `TTS_IDLE_SECONDS` without text (Soniox terminates streams that get no text for a few seconds, e.g. while an LLM runs a tool) or once the stream holds `TTS_MAX_STREAM_CHARS` (Soniox caps a stream at 2 minutes of audio), cutting at a sentence end or whitespace. The next stream opens only after the previous one terminated, so audio never interleaves and at most one Soniox stream is active.
- While no stream is open the sender sends keepalives. A source that stays silent for `TTS_MAX_IDLE_SECONDS` is abandoned, so a cancelled pipeline does not hold a connection forever.
- The receiver reads events only while a stream is active, with a `RECEIVE_TIMEOUT_SECONDS` limit per message, and forwards audio. A connection closed before the stream terminated is an error, not a short success.
- `run` yields the audio and re-raises the first task failure. Errors raised by the text source itself (and by the STT audio source) pass through unchanged instead of being mapped to Soniox errors.

## Errors

`api/client.py` maps SDK, HTTP and websocket exceptions to `SonioxAuthenticationError`, `SonioxCommunicationError` and `SonioxRequestError`. `SonioxPermissionError` is a subclass of `SonioxAuthenticationError`. Entities convert errors to Home Assistant results or `HomeAssistantError`, and start reauth when Soniox rejects the key at runtime.
