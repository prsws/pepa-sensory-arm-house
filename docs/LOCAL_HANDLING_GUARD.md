# Local handling guard

Status: built: device actions, other intents, switching on with each entry and off with the last, and the startup check and repair notice (see [Keeping it working](#keeping-it-working)), in [`local_guard.py`](../custom_components/pepa_sensory_arm_house/local_guard.py). Not built yet: the setup dialog text and the onboarding scan.

## The problem

Home Assistant's voice pipelines have a setting, **Prefer handling commands locally**. When it is on, Home Assistant tries to understand a request with its own built-in sentences before the pipeline's agent sees it. If it understands, it acts by itself and answers by itself. The agent is never called.

That is fast, and it skips Pepa's confirmation gate. On a test install, with the setting on, "apaga el aire del cuarto" turned the air conditioner off with no question, while "turn off the bedroom AC" reached Pepa and was held. Which requests skip the gate depends only on whether Home Assistant's built-in sentences happen to match the wording.

The setting belongs to the pipeline, not to this integration, and it is on by default. A fresh install, before anyone has set anything up, is therefore the worst case: nothing carries the `reverse` label yet, so every action should ask first, and any action Home Assistant can match runs without asking.

## What the guard does

From the moment the integration is installed, with no setup, it guards Home Assistant's local handling of requests that come through a pipeline whose agent is this integration:

- **An action on things that can be undone runs locally, as fast as before.** Every target of the action carries the `reverse` label.
- **Any other action is handed to Pepa.** Home Assistant does not act; the request goes on to this integration's agent, which holds the action and asks for a yes, exactly as it does today.
- **Questions answer locally.** Reading a state, the temperature, the time or the date does not change anything.
- **Everything else goes to Pepa.** An intent the guard does not know, including one a later Home Assistant release adds, is handed to Pepa. The guard fails closed.

The person hears nothing different, except that a handed-over request takes as long as any request to the agent.

## What the guard does not do

- **It does not touch other pipelines or agents.** Home Assistant's own Assist, other conversation agents, dashboards, scripts and automations behave exactly as before. "Every action on what can't be undone asks first" is true for requests through Pepa's pipelines.
- **It does not cover sentence-trigger automations.** Home Assistant runs automations with a sentence trigger before anything else, prefer-local or not, and they act directly. See [Sentence triggers](#sentence-triggers).
- **It does not change exposure.** Nothing is hidden from Assist; the guard decides who acts, not what is visible.
- **It does not change the confirmation gate.** The gate is unchanged; the guard only makes sure the request reaches it.

## How it works

Home Assistant 2026.11 does the following for each new request in a pipeline whose agent is not Home Assistant's own (`assist_pipeline/default_pipeline.py`, `recognize_intent`):

1. Sentence-trigger automations are tried. A match answers the request.
2. If prefer-local is on, Home Assistant's built-in agent tries a strict match (`conversation.async_handle_intents`). If the match succeeds and the intent's handler succeeds, Home Assistant answers.
3. Otherwise the request goes to the pipeline's agent.

Two details make the guard possible:

- **A handler can hand the request on.** If an intent handler fails with `intent.MatchFailedError`, the built-in agent reports "no valid targets" and `async_handle_intents` returns nothing, so step 3 runs (`conversation/default_agent.py`, `async_handle_intents` and `_async_process_intent_result`). Nothing is said to the person and nothing is added to the conversation.
- **A handler can tell who the pipeline is for.** The request built in step 2 carries the pipeline agent's id, and the built-in agent passes it to the handler as `intent_obj.conversation_agent_id`.

On a follow-up turn that the agent asked for, such as the answer to a confirmation question, Home Assistant skips steps 1 and 2 and sends the answer straight to the agent. The guard is not involved there.

### Where the guard sits

At setup, the integration wraps the intent handlers Home Assistant has registered. Each wrapper first checks `intent_obj.conversation_agent_id`. If it is not one of this integration's agents, the original handler runs unchanged.

For a request through a Pepa pipeline, there are two kinds of handler:

- **Service intents** (`intent.DynamicServiceIntentHandler` and its subclasses): turn on, turn off and toggle, and the light, fan, cover, media player, vacuum and lawn mower intents built on it. All matched entities are known in `async_handle_states(...)` before any service is called. The guard checks each entity's labels there. If every one carries `reverse`, the handler continues. If any does not, the guard raises `intent.MatchFailedError` before anything is called, and the whole request goes to Pepa. A mixed request, such as the lamp and the air conditioner together, goes to Pepa as a whole.
- **All other intents** (`intent.IntentHandler` subclasses that resolve and act inside their own `async_handle`, such as setting a thermostat or a humidifier). These have no common point before acting. The guard lets only a fixed list of read intents through and hands every other intent to Pepa.

The read list is the intents that only answer a question: `HassGetState`, `HassClimateGetTemperature`, `HassGetCurrentTime`, `HassGetCurrentDate`, the weather intent and `HassNevermind`. The exact names are checked against the Home Assistant version at implementation; anything not on the list goes to Pepa.

Handlers registered after setup (by an integration that loads later) are wrapped when they are found; until then they behave as before. The startup check below reports any it cannot wrap.

## Keeping it working

The guard depends on Home Assistant internals: the order of the pipeline steps, the "no valid targets" hand-on, `conversation_agent_id` and `async_handle_states`. A Home Assistant release can change any of them. Two checks catch that:

- **A test in this repository** fails if any of these hook points is missing or behaves differently, so a Home Assistant update is not released until the guard is checked against it.
- **A check when Home Assistant has started**, and again whenever another integration loads, confirms that the hook points are present and that every intent handler is wrapped or is a question. If not, the integration raises a Home Assistant repair notice ("Pepa can't guard Home Assistant's local handling") naming what it found, logs an ERROR, and tells the person what to do: turn off "Prefer handling commands locally" in each voice assistant that uses Pepa until the integration is updated. The notice clears itself once the problem is gone, and when the last Pepa entry is removed. It does not fail silently.

## Sentence triggers

Automations with a sentence trigger run before everything, act directly and are not intents, so the guard cannot see them. They are written by the household, and an existing house may already have some.

The onboarding wizard, when it is run, offers to scan the existing automations for sentence triggers that act on entities without the `reverse` label. It reports what it finds and asks the person what to do. If the person skips the scan, or never runs the wizard, the integration tells them once that Home Assistant's defaults apply to sentence-trigger automations: they act without asking.

A watcher in the Self-Monitor arm is planned to flag, afterwards, any action on an entity without the `reverse` label that was not preceded by a confirmed `pepa_confirm`.

## Setup

The setup dialog explains the guard in a few lines: with Pepa's pipelines, Home Assistant still answers questions and acts on what can be undone straight away; anything else is passed to Pepa, which asks first. Labelling the entities that can be undone with `reverse` is what makes more actions fast.

## Seeing it work

The guard writes one INFO line to the Home Assistant log when it switches on or off, for example (the count depends on the integrations installed):

```text
Local handling guard on: 41 intent handler(s) wrapped; Home Assistant answers Pepa's pipelines by itself only for questions and for actions on entities labelled reverse
Local handling guard off: 41 intent handler(s) restored
```

With debug logging on for the guard, it also writes one line each time it hands a request to the agent:

```yaml
logger:
  logs:
    custom_components.pepa_sensory_arm_house.local_guard: debug
```

```text
Local handling guard: HassTurnOff on ['climate.cuarto_termostato_cuarto'] handed to the agent (not every target carries the reverse label)
Local handling guard: HassClimateSetTemperature handed to the agent (only questions are answered locally)
```

The agent then holds the action as usual, and its `pepa_confirm` events follow.

A known gap: an intent handler that an integration registers late in its own config-entry setup, rather than when the integration loads, is not wrapped. The startup check reports any such handler in the repair notice until another integration loads and the guard wraps it.

## Testing

On the test install, the same six requests are run through a Pepa pipeline with prefer-local on, with service calls and `pepa_confirm` events recorded:

| Request | Expected |
|---|---|
| "turn off the bedroom AC" | Pepa holds and asks |
| "apaga el aire del cuarto" | Pepa holds and asks (today: Home Assistant acts) |
| "set the bedroom AC to 70" | Pepa holds and asks |
| "turn off Termostato Cuarto" | Pepa holds and asks |
| "turn on the bedroom AC" | Pepa holds and asks |
| "turn on the living room lamp" | Runs locally (lamp carries `reverse`) |

Plus: a question about a state answers locally; the same requests through Home Assistant's own pipeline behave exactly as before.
