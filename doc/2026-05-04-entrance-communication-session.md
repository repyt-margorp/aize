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

This keeps the user-facing entrypoint responsive. Entrance should acknowledge, answer simple operational questions, or ask a short clarifying question without forcing every user input through the slower goal-completion loop. When the input is intended to advance work elsewhere in AIze, Entrance infers the target SessionUnit and submits the feedback there on the user's behalf.

The `communication` session UI mode marks this intent while remaining compatible with the existing standard session renderer. Future work should split the runtime dispatch roles more explicitly:

- `communication_agent_priority`
- `goal_completer_agent_priority`
- `goal_manager_priority`
- explicit message kinds for `user_dialogue`, `route_user_prompt`, and `forwarded_user_prompt`
