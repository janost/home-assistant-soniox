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

`async_setup_entry` builds one `SonioxApiClient` for the entry's API key and region and calls `async_get_catalog()`. The catalog holds the chosen STT model and its languages, and the chosen TTS model with its languages and voices. A rejected key raises `ConfigEntryAuthFailed` (starts reauth); other failures raise `ConfigEntryNotReady`. There is no polling: entities read the catalog once and call the client per request.

## Speech-to-text

Assist streams 16 kHz mono PCM. `async_transcribe` opens the realtime STT websocket, sends the config, forwards audio, then sends an empty frame to mark the end. Final tokens are joined into the transcript. Audio sending and event receiving run as two tasks; a failure in either stops both.

## Text-to-speech

Both paths use the realtime TTS websocket with MP3 output:

- `async_stream_tts_audio` (Assist) forwards text chunks as the LLM produces them and yields audio as it arrives.
- `async_get_tts_audio` (`tts.speak`, cache misses) sends the whole message through the same stream and joins the audio.

Text longer than the 5000 character message limit is split. If the text source fails, the stream is cancelled and the original error is raised.

## Errors

`api/client.py` maps SDK, HTTP and websocket exceptions to `SonioxAuthenticationError`, `SonioxCommunicationError` and `SonioxRequestError`. Entities convert them to Home Assistant results or `HomeAssistantError`.
