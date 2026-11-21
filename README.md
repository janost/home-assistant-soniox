# Soniox for Home Assistant

Custom integration that adds [Soniox](https://soniox.com/) speech-to-text and text-to-speech to Home Assistant.

Forked from [greimela/home-assistant-soniox-stt](https://github.com/greimela/home-assistant-soniox-stt), which only provided speech-to-text.

## Features

- **Speech-to-text** for Assist pipelines, using Soniox realtime transcription.
- **Text-to-speech** for Assist pipelines and `tts.speak`. Assist streams LLM responses to Soniox as they are generated, so playback starts before the full reply is written.
- Built-in voices plus any voices you cloned in your Soniox project.
- Speaking rate per request (`speed`, 0.7 to 1.3).
- Region selection: United States, European Union, Japan or India.
- Configured entirely in the UI.

## Requirements

- Home Assistant 2026.10.4 or newer
- A Soniox project and its API key. Keys are bound to the region of their project, so pick the matching region during setup.

## Installation

### HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/janost/home-assistant-soniox` as type **Integration**.
3. Install **Soniox** and restart Home Assistant.

### Manual

Copy `custom_components/soniox` into your Home Assistant `custom_components` directory and restart.

## Setup

1. **Settings** → **Devices & services** → **Add integration** → **Soniox**.
2. Enter the API key and select the region of your Soniox project.

This creates one service device with two entities:

- `stt.soniox_speech_to_text`
- `tts.soniox_text_to_speech`

Use them in **Settings** → **Voice assistants** by picking Soniox as the speech-to-text and text-to-speech engine of a pipeline.

### Options

**Configure** on the integration offers:

- **Speech-to-text language**: `Automatic` follows the pipeline language. A fixed language restricts recognition to that language.
- **Default text-to-speech voice**: used when a request does not name a voice.

Change the API key or region with **Reconfigure**.

## Automations

Speak on a media player:

```yaml
action: tts.speak
target:
  entity_id: tts.soniox_text_to_speech
data:
  media_player_entity_id: media_player.living_room
  message: "The washing machine is done."
  language: en
  options:
    voice: Maya
    speed: 1.1
```

`language` and `options` are optional. Without them the Home Assistant language (or English) and the default voice are used.

## Troubleshooting

- **Invalid API key**: the key may belong to a project in another region. Check the region of the project in the Soniox Console.
- **No text-to-speech entity**: the API key has no access to a Soniox TTS model.
- Debug logging:

  ```yaml
  logger:
    logs:
      custom_components.soniox: debug
  ```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). `script/setup/bootstrap` creates the environment, `script/check` runs type checks, lint and spelling, `script/test` runs the tests.
