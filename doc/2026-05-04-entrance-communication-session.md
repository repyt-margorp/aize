# Entrance Communication Session

Entrance is the first AIze SessionUnit that explicitly separates user communication from goal completion.

Traditional SessionUnits are centered on:

- `Goal`
- `GoalManager`
- goal-completing agents
- user feedback that helps complete the goal

Entrance adds a communication layer:

- `User <-> EntranceAgent`: short, responsive dialogue with the user.
- `EntranceAgent -> target SessionUnit`: inferred forwarding of user feedback as UserPrompt input.
- `Goal / GoalManager / GoalCompleterAgent`: the longer-running work loop inside the target sessions.

This keeps the user-facing entrypoint responsive. Entrance should acknowledge, answer simple operational questions, or ask a short clarifying question without forcing every user input through the slower goal-completion loop. It must not start doing implementation, debugging, or other worker-style task execution itself. When the input is intended to advance work elsewhere in AIze, Entrance infers the target SessionUnit and submits the feedback there on the user's behalf.

The `communication` session UI mode marks this intent while remaining compatible with the existing standard session renderer. Runtime behavior is controlled by:

- `session_interactive`: marks that the session has a user-facing communication layer.
- `communication_agent_enabled`: turns the quick-response communication agent route on or off.
- `communication_agent_priority`

When `communication_agent_enabled` is true, normal user prompts are queued as `user_dialogue` and dispatched to the existing AgentService pool with the session role `communication_agent`. This does not introduce a separate process type; Codex, Claude Code, and Gemini workers can all serve as communication agents according to provider priority and normal session agent selection. Priority entries can be provider strings or provider profile objects, so an interactive session can select `codex` with a specific model and config overrides while leaving unspecified parameters at provider defaults.

Entrance defaults to the `interactive-fast` Codex profile:

```json
{
  "provider": "codex",
  "profile": "interactive-fast",
  "model": "gpt-5.5",
  "config": {
    "model_reasoning_effort": "low",
    "model_verbosity": "low"
  }
}
```

Future work should split the remaining runtime dispatch roles more explicitly:

- `goal_completer_agent_priority`
- `goal_manager_priority`
- explicit message kinds for `route_user_prompt` and `forwarded_user_prompt`
