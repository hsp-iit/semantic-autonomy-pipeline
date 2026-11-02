# semantic-autonomy-pipeline

All the agent code of the robot: the voice of the robot, the orchestrator that decides what to do, the navigator
agent, the MCP servers they use, and the chat front ends. Grasping and head control are not part of it yet.

The robot skills stay in their own repositories: mapping and Nav2 in `visual-language-navigation`, the last
stretch to an object in `manipulation-approach`.

![Autonomy pipeline](docs/diagrams/autonomy_pipeline.png)

See [docs/architecture.md](docs/architecture.md) for how a request flows and why it is built this way, and
[docs/robot_server.md](docs/robot_server.md) for the robot tools.

| Part | What it is | Default |
|---|---|---|
| `chatter.py`, `llmchat_input_mode.py` | MCP host of `yarp-mcpHosts` serving `LlmChat`. A bare stop command skips the LLM. | Azure |
| `mission_server.py` | MCP server that runs one orchestrator at a time. | port 8767, Sonnet |
| `robot_server.py` | MCP server over the semantic map and Nav2. | port 8765 |
| `camera_server.py` | MCP server with `look`, reading the camera topic directly. | port 8768 |
| `approach_server.py` | MCP server around `/reach_object`. | port 8766 |
| `web_chat/`, `speech_pipeline/` | Web page and voice bridge to the chatter. | |
| `agents/` | Prompts of the chatter, the orchestrator and the navigator. | |

## Requirements

- `visual-language-navigation`, which provides `ros2_vlmaps_interfaces`, the semantic map server and Nav2
- `manipulation-approach` in the same colcon workspace, with its pipeline running
- `yarp-mcpServers` and `yarp-mcpHosts` installed, and `yarpserver`
- The `claude` CLI, logged in, for the orchestrator and the navigator

## Build

```bash
cd ~/ws_ros2 && colcon build --packages-select semantic_autonomy speech_pipeline
cd ~/visual-language-navigation && colcon build --packages-select visual_language_navigation
```

**Both builds are needed.** An older install of `visual_language_navigation` still registers the robot server and
wins over this one, so `robot` fails with `No module named 'visual_language_navigation.llm'`. Without the first
build the launchers print `No installed package provides server type` and then wait forever. To check:

```bash
python3 -m yarp_mcpServer_core.launcher --list_servers | grep -E 'Robot|Camera|Approach|Mission'   # all "ok"
```

## Run

The chatter defaults to Azure. `AZURE_API_MODE` selects `responses` (default) or `chat_completions`, which also
needs `AZURE_API_VERSION`.

```bash
set -a; . ~/.config/config.env; set +a     # a file with AZURE_API_KEY, AZURE_ENDPOINT and DEPLOYMENT_ID
~/ws_ros2/src/semantic-autonomy-pipeline/scripts/run_all.sh
```

For an Ollama server on the network, with a model that supports tools:

```bash
scripts/run_all.sh --ros-args -p backend:=ollama -p backend_url:=http://<host>:11434/v1 -p backend_model:=<model>
```

`run_all.sh` starts the four MCP servers and the chatter. `Ctrl+C` stops everything and cancels the goals. It does
not start the web chat or the voice bridge: see below.

## Run each part alone

Use one terminal for each part, and start every terminal with this. The `PYTHONPATH` line is required: the chatter and
the mission server find `agents/` next to the source, so `ros2 run` from the install does not find their prompts.

```bash
source /opt/ros/jazzy/setup.bash
source ~/visual-language-navigation/install/ros2_vlmaps_interfaces/share/ros2_vlmaps_interfaces/local_setup.bash
source ~/ws_ros2/install/local_setup.bash
export PYTHONPATH=~/ws_ros2/src/semantic-autonomy-pipeline/semantic_autonomy:$PYTHONPATH
cd ~/ws_ros2/src/semantic-autonomy-pipeline/semantic_autonomy
```

| Part | Command | Port | Needs first |
|---|---|---|---|
| Robot server | `python3 -m yarp_mcpServer_core.launcher --config config/robot_mcp_server.xml` | 8765 | `yarpserver`. For its tools: `semantic_map_server`, Nav2, localization |
| Camera server | `python3 -m yarp_mcpServer_core.launcher --config config/camera_mcp_server.xml` | 8768 | `yarpserver`, the camera topic, `YARP_ROBOT_NAME` |
| Approach server | `python3 -m yarp_mcpServer_core.launcher --config config/approach_mcp_server.xml` | 8766 | `yarpserver`. For its tools: the `manipulation-approach` pipeline |
| Mission server | `python3 -m yarp_mcpServer_core.launcher --config config/mission_mcp_server.xml` | 8767 | `yarpserver`, `claude` logged in, and the three servers above for a mission to work |
| Chatter | `python3 -m semantic_autonomy.chatter` | | The backend settings of [Run](#run). It waits for the mission server |
| Web chat | `python3 -m semantic_autonomy.web_chat.backend` | 5000 | The chatter, to answer. Open `http://<robot>:5000` |
| Voice bridge | `ros2 run speech_pipeline speech_bridge` | | The chatter, and the YARP speech ports |

The orchestrator and the navigator are not processes to start: the mission server runs them. To talk to them
directly, with the first three servers running and no chat:

```bash
cd ~/ws_ros2/src/semantic-autonomy-pipeline/agents/orchestrator && claude
```

To send one message without the web chat, with the chatter running:

```bash
ros2 service call /chat_agent/user_text ros2_vlmaps_interfaces/srv/LlmChat "{text: 'hello'}"
```

The `manipulation-approach` pipeline runs on its own: see `docs/getting_started.md` in that repository.

## Tests

Source the workspaces first, then run from `semantic_autonomy/test`.

```bash
python3 test_stack.py                           # Scripted LLMs and robot: no usage, about one minute
python3 live_dry_run.py                         # Real orchestrator and navigator over a scripted robot: spends Claude usage
python3 live_dry_run.py azure                   # The same with a real chatter
python3 live_dry_run.py ollama <url> <model>
python3 chatter_intent_check.py azure           # Tricky messages against the model only: no robot, no servers
python3 chatter_intent_check.py ollama <url> <model>
```

Run `chatter_intent_check.py` on any new model before using it: it shows which tool the model picks for messages
like "stop at the table and take a photo" or "forget it".

## Known gaps

- **Only one real chatter model has been tested.** An Azure deployment passed 18 of 18 messages in
  `chatter_intent_check.py`, twice, and a full `live_dry_run.py azure`. No Ollama model has run. How well a model
  calls `start_mission` matters: one that says "on it" without the call leaves the person waiting for nothing.
- **A model with reasoning can return no text.** On a test call limited to 200 tokens, one GPT-5 deployment gave
  empty replies. If the chatter stays silent, raise `max_tokens`.
- **Not run on the robot.** Real Nav2, the semantic map, DINO/SAM, the arms and real audio have not been part of
  any run of this pipeline. The robot server moved with its tool schemas unchanged, but nothing has driven it here.
- **The orchestrator can see the robot tools.** Claude Code offers no way to give an MCP server to a sub-agent alone
  (see [the architecture notes](docs/architecture.md#what-the-agents-can-do)). Only its prompt keeps it on the navigator.
- **Hands stay raised.** Nothing lowers them after an approach or a cancel, so the next navigation walks with raised
  arms. Lowering them belongs in `manipulation-approach`.
- **No head control.** `/reach_object` needs the object in view at the start. With a fixed head, the 0.8 m standoff
  of the navigator may not show it. The orchestrator checks with the camera and reports it.
- **`look` has been tested on generated images only.** The image topic comes from `YARP_ROBOT_NAME`, or from
  `image_topic` in `config/camera_mcp_server.xml`. The display in the web page itself was not checked in a browser:
  the tests stop at the message the page receives.
- **The chatter connects once.** If the mission server restarts, restart the chatter. Its tools fail until then,
  but stop words keep working, because they open their own connection.
- **Conversation history is not trimmed.** It grows until `new conversation`. This matters most on a small local
  model during a long session.
- **`LlmChat.srv` is still in `ros2_vlmaps_interfaces`.** Nothing in `visual-language-navigation` uses it now.
- **A stop in words depends on the LLM.** Only a bare command ("stop", "ferma", "please stop") skips it. Anything
  else, like "forget it", needs the model to recognise the intent, so it takes a turn and fails if the LLM is down.
- **Usage limits.** Each mission spends Claude usage on an orchestrator and a sub-agent.
- **This is not a safety stop.** `stop_all` needs Python, ROS and MCP to be up. The emergency stop of the robot
  remains the safety system.
