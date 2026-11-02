#!/usr/bin/env bash
# Starts the agent layers: robot, camera, approach and mission MCP servers, chatter.
# Needs yarpserver, the semantic map server and Nav2. For the approach it needs the manipulation-approach
# pipeline, which runs on its own. Ctrl+C stops all of them, which cancels any goal they sent.
#
# Extra arguments go to the chatter, whose default backend is Azure (AZURE_API_KEY, AZURE_ENDPOINT, DEPLOYMENT_ID):
#   run_all.sh --ros-args -p backend:=ollama -p backend_url:=http://<host>:11434/v1 -p backend_model:=<model>
# VLN_DIR overrides the visual-language-navigation path, which provides ros2_vlmaps_interfaces.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
WS="$(cd "$REPO/../.." && pwd)"
VLN_DIR="${VLN_DIR:-$HOME/visual-language-navigation}"
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
INTERFACES="$VLN_DIR/install/ros2_vlmaps_interfaces/share/ros2_vlmaps_interfaces/local_setup.bash"
if [ -f "$INTERFACES" ]; then
    source "$INTERFACES"
fi
source "$WS/install/local_setup.bash"
PKG="$REPO/semantic_autonomy"
export PYTHONPATH="$PKG:$PYTHONPATH"

# TERM: a script starts its background jobs with SIGINT ignored. The chatter goes first: it stops the robot on
# exit, through the servers.
stop_all() {
    trap '' INT TERM
    kill -TERM "$chatter" 2>/dev/null
    wait "$chatter"
    kill -TERM "${pids[@]}" 2>/dev/null
    wait
    exit 0
}

pids=()
trap stop_all INT TERM
for server in robot camera approach mission; do
    python3 -m yarp_mcpServer_core.launcher --config "$PKG/config/${server}_mcp_server.xml" &
    pids+=($!)
done
python3 -m semantic_autonomy.chatter "$@" &    # waits for the mission server
chatter=$!
wait
