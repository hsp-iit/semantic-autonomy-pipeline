# Robot MCP server

*Reference for `semantic_autonomy/robot_server.py`: what the navigator can see and do.*

---

## 1. What it is

An MCP server over the semantic map and Nav2, used by the navigator agent. It holds the robot-specific logic so
the agent decides *what* to do and the tools compute *where*. It contains no LLM.

**Key files**
- `semantic_autonomy/robot_server.py` — `RobotBridge` (ROS node) + the MCP tool definitions.
- `semantic_autonomy/robot_models.py` — output schemas of the tools.
- `semantic_autonomy/config/robot_mcp_server.xml` — configuration for `yarp_mcpServer_launcher`.
- `agents/orchestrator/.claude/agents/navigator.md` — the prompt of the navigator.

---

## 2. Running it

`scripts/run_all.sh` starts it with the other servers. It serves `http://127.0.0.1:8765/mcp` as one long-lived
process, so candidate ids (`chair#2`) and the active Nav2 goal persist across agent sessions.

For a terminal session on the robot without the chat, start the servers and run `claude` in
`agents/orchestrator`, where `.mcp.json` and `.claude/settings.json` apply.

---

## 3. `RobotBridge` — what backs the tools

A ROS node holding: a `tf2` buffer for the robot pose, service clients to the semantic map server
(`query_objects`, `describe_area`, `pub_goal`, `index_map`), a camera-image service client, and
Nav2 `NavigateToPose` / `ComputePathToPose` action clients.

- **Candidate cache** — every `find_objects` call assigns stable ids (`"chair#1"`, ranked by size,
  not match quality) in the map frame, keyed per query prefix; stale ids for a repeated query are
  dropped before the new ones are added. Other tools take these ids instead of raw coordinates.
- **Approach geometry lives here, not in the LLM.** `plan_approach` picks a direction — front
  (`facing_theta`) if confident and the object doesn't lie flat, else the side it was observed from
  (`approach_theta`) if confident, else toward the robot — and offsets by the object's half-depth
  along that direction plus a clearance (`footprint_radius`, default 0.8 m), so the goal for a large
  object (table, shelf) doesn't land inside it.
- **Nav state** (`self.nav`) tracks one goal at a time: label, requested/snapped pose, status
  (`sending`/`active`/`succeeded`/`aborted`/`canceled`/`rejected`), feedback (distance remaining,
  ETA, recovery count). Sending a new goal cancels the previous one first.
- Robot base frame auto-detects from `YARP_ROBOT_NAME` (`geometric_unicycle` for ergoCub,
  `mobile_base_body_link` for R1) unless `robot_base_frame` is set explicitly — the usual
  `_ecub`/`_r1` split, but resolved at runtime here rather than by separate yaml files.

Relevant params (all on `RobotBridge`, `DEFAULT_PARAMS` in `robot_server.py`): `map_frame`,
`robot_base_frame`, the five service/action name params, `footprint_radius` (0.8 m),
`facing_conf_threshold` (0.5), `approach_conf_threshold` (0.4), `lies_flat_threshold` (0.6, top
fraction above which an object is treated as having no front), `camera_max_width` (960 px, images
are downscaled before reaching the LLM).

---

## 4. MCP tools

All are methods marked with `@mcp_tool`. Positions are meters/radians in the `map` frame unless a field says
`from_robot` (forward/left).

| Tool | Purpose |
|---|---|
| `find_objects(objects, max_candidates=6, area=None)` | Locate objects/rooms in the semantic map. One call per request, one string per physical object (visual attributes ok — "orange chair"; relations must be split — "chair with backpack" → `["chair", "backpack"]`). Returns per candidate: id, `pos_map`, `from_robot`, size, footprint, height, score/purity, optional facing/observed-from angle + confidence, `lies_flat`, and — with several queried objects — `nearest` relations (distance, `on_top_of_it`/`supports_it`) to the closest candidate of every other query. |
| `describe_area(x=None, y=None, radius=2.5, extra_words=None)` | Coarse-vocabulary inventory of voxel clusters around a point (default: the robot). For "what's around/in this room"; confirm a specific hit with `find_objects` before navigating to it. |
| `get_robot_pose()` | Current pose in the map frame (x, y, theta in radians and degrees). |
| `check_reachability(candidate_ids, side="auto")` | Nav2 path length to each candidate's approach pose — one plan per candidate, slower than `find_objects`; use to pick the truly closest instance or rule out unreachable ones. |
| `plan_approach(candidate_id, side="auto", clearance=None)` | Compute, without moving, the standoff pose facing a candidate. `side`: `auto`/`front`/`back`/`observed`/`toward_robot`/`left`/`right` (left/right as seen from the robot's current position). Raises if `front`/`back` is requested but the object has no reliable front. |
| `navigate_to_object(candidate_id, side="auto", clearance=None)` | `plan_approach` + drive there and face it. Returns an operation as soon as Nav2 accepts the goal. |
| `navigate_to(x, y, theta=None, frame="map")` | Drive to an explicit pose. `frame="map"` or `"robot"` (x=forward, y=left of the robot now); `theta` defaults to the direction of travel. Goal is snapped to the nearest free cell. |
| `move_relative(forward=0, left=0, turn_deg=0, reference="robot")` | Small corrections ("a bit more left", "step back", "turn around"). `reference="robot"` (current pose) or `"last_goal"` (useful if the robot hasn't arrived yet). |
| `wait_for_navigation(timeout_s=60)` | Block until the current goal finishes or the timeout elapses; returns status. |
| `get_operation_status(operation_id)`, `list_operations(active_only=True)`, `cancel_operation(operation_id)` | Standard operation tools. Status, distance remaining, recoveries and elapsed time are in the snapshot's `details`. |
| `cancel_navigation()` | Cancel the active Nav2 goal. |
| `highlight_in_rviz(category)` | Highlight a category's voxels in RViz (red = match, blue = rest) for the human watching; returns no positions. |
| `look()` | Current RGB camera frame as a JPEG `Image`, downscaled to `camera_max_width`. |

Tool errors are normalized through `_run()`: any exception becomes a `ToolError` carrying its
message (unknown candidate id, Nav2 unavailable, ...), since MCP otherwise only forwards a bare
"Error executing tool" and the agent needs the reason to recover.

---

## 5. ROS interfaces touched

**Services** (`ros2_vlmaps_interfaces/srv`): `QueryObjects`, `DescribeArea`, `PublishGoal`,
`IndexMap`, `CameraRGB` — all served by `semantic_map_server` / `semantic_map_builder`.
**Actions**: Nav2's `NavigateToPose`, `ComputePathToPose`.
