# Architecture

## How a request flows

For "go to the table and get close to the red mug":

1. The web chat or the speech pipeline calls `chat_agent/user_text`.
2. The chatter runs one LLM turn on Azure or Ollama, which calls `start_mission` and says "on it". The chat is
   free again.
3. The mission server starts `claude -p` in `agents/orchestrator`, and returns an operation.
4. The orchestrator asks the navigator to go to the mug. The navigator uses the robot MCP server and blocks on
   `wait_for_navigation`, which is fine here because nobody is waiting on this process.
5. It calls `look`, then `approach_object` with a visual prompt such as "red mug", never
   a candidate id: the detector reads text, and the map's ids mean nothing to it.
6. It waits for the approach, and calls `look` again, because `reached` only means that Nav2 finished.
7. Its final message is the mission result. The host of the chatter follows the operation, and runs one more LLM
   turn to tell the person on `llm_reasoning`, which the web chat and the voice read.

## Why it is built this way

**Every layer is an MCP server with operations.** It is the convention of `yarp-mcpServers`, and it keeps the
layers replaceable.

**The chatter is a `yarp-mcpHosts` host, not a Claude agent.** Talking needs a small model, and a host brings the
Azure and Ollama backends, the tool loop and the operation tracking. What this repo adds is listed in `Chatter`: a
short prompt, a tool allowlist that is also enforced on calls because a model can call a tool it was not offered,
LLM timeouts, and the delivery of mission outcomes. An outcome reaches the person even with the LLM down, in the
orchestrator's words.

**The chatter never waits for the robot.** The web chat gives up after 30 s and a mission takes minutes. A
blocking agent also could not hear the person, which is why `claude_chat_bridge` needs `wait=false` and a
special prompt. Here the two jobs are separate processes.

**The chatter decides what a stop means.** Words like "stop" and "wait" also appear inside requests ("go to the
kitchen, stop there and take a photo"), so the LLM reads the whole message and calls `stop_all` when the person
means to give up. Only a bare command skips it: it has nothing to interpret, and it runs on the ROS thread of the
request, with its own event loop and MCP client, because the host blocks on each LLM call. So "stop" works with the
LLM slow, down or hanging. `stop_all` kills the
orchestrator's whole process group first, because it could otherwise send another goal, then cancels
`/reach_object` and the navigation. Both cancels are needed: the robot server only knows the goals it sent, and
`/reach_object` sends its own through the detector. The chatter only says "Stopped." when both servers confirmed.

**A mission that ends badly stops the robot.** After a timeout or a crash nobody watches the goal any more, so
the mission server cancels it. Cancelling a mission does the same.

**The camera is its own server.** `look` reads the image topic directly, so it works without the map or the
navigation, and the orchestrator can verify a result with its own model, without spending a navigator turn. It
refuses a frame older than `max_image_age`: these checks are what a mission relies on, so an old image would be
worse than none. Taking a photo means this and nothing more: the image goes to the agent, and to the web chat on the
`chat_image` topic if one is open. Nothing is saved. The chat has its own topic because `speech_bridge` reads
`llm_reasoning` aloud.

**One Nav2 server, one owner.** The navigator and `/reach_object` share a Nav2 server, where a new goal silently
replaces the running one. `approach_object` reads the status topic and refuses while a goal is active. The other
direction is not enforced: the robot server does not check for a running approach, and the orchestrator is only
told not to do this.

## What the agents can do

Headless `claude -p` ignored the `allow` list of the workspace settings in a test, so the orchestrator's permissions
are passed on the command line. `agents/orchestrator/.claude/settings.json` applies to an interactive session in that
folder.

| Agent | Tools |
|---|---|
| Chatter | `start_mission`, `stop_all`, `get_tracked_operation`, `list_tracked_operations` |
| Orchestrator | `Agent`, approach tools, `look`, and robot tools (see below) |
| Navigator | The tools listed in `navigator.md`: robot tools except `cancel_operation`, and `look` |

The orchestrator should not use the robot tools, but it can. Tested with Claude Code 2.1.316: a server declared in
the sub-agent's frontmatter did not connect, and denying `mcp__robot` to the orchestrator also denied it to the
sub-agent. So the server stays in the shared configuration, `tools:` limits the navigator, and only the prompt
limits the orchestrator.

## Prompts

The orchestrator's `CLAUDE.md` is passed with `--system-prompt-file`. Read only as `CLAUDE.md`, it was treated as
background: in two dry runs the agent skipped the final camera check and wrote a long report. As the system
prompt, it did the check and kept to two or three sentences.

The chatter's `prompt.md` is written for a smaller model: it states plainly that nothing happens without the tool
call, and it replaces the host's default prompt entirely.

## What the dry runs showed

With the real orchestrator and navigator over a scripted robot (`test/live_dry_run.py`):

| Check | Result |
|---|---|
| Mission | Tools in the expected order, `red mug` sent to `/reach_object` |
| Final report | Without a change in the image, it said the camera did not confirm the approach. With a mug that grows after the approach, it confirmed it |
| Stop during a mission | Replied in well under a second, mission `cancelled`, navigation cancelled |

These are not robot timings: the scripted robot answers at once, so a real mission takes much longer. With a real
Azure chatter, replies took 2 to 3 s through the whole chain, and the chatter turned the long report of the
orchestrator into one spoken sentence.

The first run of `test/chatter_intent_check.py` on Azure showed the prompt at fault, not the model: told to ask when
a request was vague, it asked "what of?" to "take a photo", and to "behave accordingly". Told to offer an alternative
to grasping, it started the mission without waiting. The rules in `prompt.md` now say so explicitly.
