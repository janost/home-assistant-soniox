# Decisions

## Open realtime websockets directly instead of through the SDK

The Soniox SDK's realtime clients call `websockets.connect` without an SSL context, so `websockets` builds a default one and loads the CA bundle inside the event loop. Home Assistant flags that as a blocking call. The client opens the websockets itself with `homeassistant.util.ssl.get_default_context()` and still uses the SDK's pydantic types to build and parse messages. REST calls go through the SDK with the same context passed as `verify`.

Revisit if the SDK accepts an `ssl` argument.

## Only streaming TTS, rotating Soniox streams on pauses

Home Assistant sends every request through `async_stream_tts_audio` once an entity implements it, so there is no separate one-shot path. Soniox ends a stream that gets no text for a few seconds without `text_end`, and keepalives do not prevent that. Assist starts streaming TTS exactly when an LLM reply is followed by a tool call, so the client ends the stream after `TTS_IDLE_SECONDS` of silence and opens a new one when text resumes. The MP3 output of consecutive streams is concatenated.

## No speech-to-text language option

Assist pipelines only use an STT engine for languages it lists, and the pipeline language is sent to Soniox as a strict hint. A fixed-language option could only narrow the list without changing what is sent.

## Domain `soniox` instead of `soniox_stt`

The fork covers STT and TTS. A different domain also lets it be installed next to the upstream `soniox_stt` integration. Existing upstream entries do not migrate; set the integration up again.

## Region is entry data, models are not configurable

Soniox API keys only work in their project's region, so the region is chosen with the key and changed through reconfigure. Models default to the current realtime models (`stt-rt-v6`, `tts-rt-v2`) and fall back to the newest model the key can use.
