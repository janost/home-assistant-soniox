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

Assist streams 16 kHz mono PCM. `async_transcribe` opens the realtime STT websocket, sends the config, forwards audio, then sends an empty frame to mark the end. Final tokens are joined into the transcript. Audio sending and event receiving run as two tasks; a failure in either stops both.

## Text-to-speech

The entity implements only `async_stream_tts_audio`. Home Assistant routes every request through it, wrapping plain `tts.speak` messages in a one-item stream. Output is MP3.

`_TtsSession` in `api/client.py` runs one websocket:

- A reader task copies the text source into a queue, so the sender can wait on it with a timeout without cancelling the source.
- The sender opens a Soniox stream on the first text. If no text arrives for `TTS_IDLE_SECONDS`, it sends `text_end`; the next text opens a new stream on the same connection. Soniox terminates streams that get no text for a few seconds, which happens when an LLM pauses for a tool call. Keepalives go out while no stream is open.
- The generator yields audio from all streams in order and returns once the source is done and every opened stream reported `terminated`. A connection closed before that is an error, not a short success.
- If the text source fails, the open stream is cancelled and the original error is raised.

Text longer than the 5000 character message limit is split.

## Errors

`api/client.py` maps SDK, HTTP and websocket exceptions to `SonioxAuthenticationError`, `SonioxCommunicationError` and `SonioxRequestError`. `SonioxPermissionError` is a subclass of `SonioxAuthenticationError`. Entities convert errors to Home Assistant results or `HomeAssistantError`, and start reauth when Soniox rejects the key at runtime.
