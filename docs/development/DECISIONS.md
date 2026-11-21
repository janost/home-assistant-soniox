# Decisions

## Open realtime websockets directly instead of through the SDK

The Soniox SDK's realtime clients call `websockets.connect` without an SSL context, so `websockets` builds a default one and loads the CA bundle inside the event loop. Home Assistant flags that as a blocking call. The client opens the websockets itself with `homeassistant.util.ssl.get_default_context()` and still uses the SDK's pydantic types to build and parse messages. REST calls go through the SDK with the same context passed as `verify`.

Revisit if the SDK accepts an `ssl` argument.

## Use the realtime websocket for one-shot TTS too

The REST TTS endpoint caps input at 5000 characters. The websocket accepts text in chunks, so one code path covers streaming Assist replies and long `tts.speak` messages.

## Domain `soniox` instead of `soniox_stt`

The fork covers STT and TTS. A different domain also lets it be installed next to the upstream `soniox_stt` integration. Existing upstream entries do not migrate; set the integration up again.

## Region is entry data, models are not configurable

Soniox API keys only work in their project's region, so the region is chosen with the key and changed through reconfigure. Models default to the current realtime models (`stt-rt-v6`, `tts-rt-v2`) and fall back to the newest model the key can use.
