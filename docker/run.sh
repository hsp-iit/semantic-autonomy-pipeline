#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
#
# Starts the semantic_autonomy image with the GPU, the host network and the display.
# Optional files on the host, mounted when they exist:
#   CYCLONEDDS_XML   CycloneDDS configuration of the robot network   (default ~/cyclonedds.xml)
#   YARP_CONF        yarp.conf with the name server of the robot     (default ~/.config/yarp/yarp.conf)
#   CONFIG_ENV       AZURE_API_KEY, AZURE_ENDPOINT, DEPLOYMENT_ID     (default ~/.config/config.env)
#   SEMANTIC_MAPS    folder of the semantic maps                     (default ~/semantic_maps)
# The Claude Code login of the host (~/.claude, ~/.claude.json) is mounted too, for the orchestrator.
IMAGE="${IMAGE:-semantic_autonomy:latest}"
CYCLONEDDS_XML="${CYCLONEDDS_XML:-$HOME/cyclonedds.xml}"
YARP_CONF="${YARP_CONF:-$HOME/.config/yarp/yarp.conf}"
CONFIG_ENV="${CONFIG_ENV:-$HOME/.config/config.env}"
SEMANTIC_MAPS="${SEMANTIC_MAPS:-$HOME/semantic_maps}"
HOME_IN=/home/user1

args=(--network=host --ipc=host --gpus all -it --rm --name semantic_autonomy
      -e DISPLAY="${DISPLAY}" -v /tmp/.X11-unix:/tmp/.X11-unix
      -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}" -e YARP_ROBOT_NAME="${YARP_ROBOT_NAME:-}")
mount() { [ -e "$1" ] && args+=(-v "$1:$2"); }
if mount "$CYCLONEDDS_XML" "$HOME_IN/cyclonedds.xml"; then
    args+=(-e CYCLONEDDS_URI="$HOME_IN/cyclonedds.xml")
fi
mount "$YARP_CONF" "$HOME_IN/.config/yarp/yarp.conf"
mount "$CONFIG_ENV" "$HOME_IN/.config/config.env"
mount "$SEMANTIC_MAPS" "$HOME_IN/semantic_maps"
mount "$HOME/.claude" "$HOME_IN/.claude"
mount "$HOME/.claude.json" "$HOME_IN/.claude.json"

xhost +local: >/dev/null 2>&1
exec docker run "${args[@]}" "$IMAGE" bash
