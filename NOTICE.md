# Notice

## What this repository is

`pepa-sensory-arm-house` is the house scope of the Pepa Sensory Arm: a Home
Assistant conversation agent for Pepa's house — its senses, muscles and voice.
Project documentation lives at [ai4aging.org](https://ai4aging.org).

## Where it comes from

This repository began as an unmodified copy of
[aradlein/hass-agent-llm](https://github.com/aradlein/hass-agent-llm) ("Home
Agent") at commit `6989adb` (v0.9.5, 11 May 2026). That starting point is
bookmarked by the `radlein-base` tag. Everything after that tag is work done
in this repository.

Upstream pull requests that are merged here later will be listed below, so that
their authors stay credited.

| Upstream pull request | What it brought | Merged here |
| --- | --- | --- |
| [#44](https://github.com/aradlein/hass-agent-llm/pull/44) by Ptah-CT | Exposed-entity allow-list refreshed every conversation turn, so entities that appear after a cold boot (late Zigbee/MQTT discovery) are not locked out | #6 |
| [#43](https://github.com/aradlein/hass-agent-llm/pull/43) by Ptah-CT | Memory extraction through the external LLM unwraps its double-wrapped result, so memories are stored instead of silently dropped | #9 |
| [#51](https://github.com/aradlein/hass-agent-llm/pull/51) by DoglausSmackerson | Opt-in setting to keep the satellite's microphone open for a follow-up reply (`continue_conversation`), off by default | #10 |

## Licence

The upstream code is released under the MIT License, copyright (c) 2024 Home
Agent Contributors. The `LICENSE` file is kept unchanged, and additions made in
this repository are offered under the same licence.
