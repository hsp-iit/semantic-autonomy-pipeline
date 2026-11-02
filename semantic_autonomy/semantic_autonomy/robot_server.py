# SPDX-FileCopyrightText: 2024 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
# Author: Simone Micheletti

"""
YARP MCP server that exposes the robot to any MCP host (Claude Code, yarp-mcpHosts).

Pipeline:
1. yarp_mcpServer_launcher builds Yarp_mcpServer_Robot from an XML configuration (see
   config/robot_mcp_server.xml), found through the 'yarp_mcpServer_core.servers' entry point.
   It serves MCP over streamable HTTP and advertises the URL on /mcp_server/<server_name>/info:o.
2. _initialize() spins an rclpy node in a background thread. RobotBridge holds its clients for the
   semantic map server (query_objects, describe_area, pub_goal, index_map), the camera service, the
   Nav2 NavigateToPose / ComputePathToPose actions, and a tf buffer for the robot pose.
3. Each MCP tool is a thin async wrapper that runs the blocking ROS call in a worker thread, so the
   MCP event loop stays responsive while the robot drives.
4. Goals are long-running operations: a navigation tool returns as soon as Nav2 accepts the goal, a
   monitor task mirrors the goal state into the OperationRegistry, and hosts follow it through the
   operation resource. Hosts without operation tracking call wait_for_navigation.
5. Candidates returned by find_objects are cached in the map frame under stable ids ("chair#1"), so
   the agent refers to objects by id and never copies coordinates around. The server is shared by
   every host session, so ids and the active goal survive across sessions.
6. All geometry (frame changes, approach side, standoff, facing angle) is computed here: the LLM
   decides *what* to do, the tools compute *where*.
"""

import asyncio
import base64
import json
import math
import os
import sys
import threading
import time

import cv2
import numpy as np
import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.signals import SignalHandlerOptions
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from visualization_msgs.msg import Marker
from nav2_msgs.action import NavigateToPose, ComputePathToPose
from ros2_vlmaps_interfaces.srv import QueryObjects, DescribeArea, PublishGoal, IndexMap, CameraRGB
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from yarp_mcpServer_core import yarp
from yarp_mcpServer_core.lib_server.YARP_mcpServer_Operations import Yarp_mcpServer_Operations
from yarp_mcpServer_core.lib_server.mcp_tools import mcp_tool
from yarp_mcpServer_core.lib_server.operation_models import OperationSnapshot
from yarp_mcpServer_core.lib_server.operation_registry import OperationNotFoundError
from yarp_mcpServer_core.lib_server.result_models import SuccessResult
from yarp_mcpServer_ros2.runtime import Ros2NodeMixin

from semantic_autonomy.robot_models import (
    ApproachPlan, DescribeAreaResult, FindObjectsResult, HighlightResult, NavigationStartResult,
    Pose2D, ReachabilityResult)

ROBOT_BASES = {
    'ergocub': 'geometric_unicycle',
    'r1': 'mobile_base_body_link',
}
SIDES = ("auto", "front", "back", "observed", "toward_robot", "left", "right")
# Configuration keys (XML <param>), with their defaults. The value type gives the type read from YARP.
DEFAULT_PARAMS = {
    'map_frame': 'map',
    'robot_base_frame': '',                     # empty = pick from YARP_ROBOT_NAME
    'query_objects_service': 'semantic_map_server/query_objects',
    'describe_area_service': 'semantic_map_server/describe_area',
    'goal_pub_service': 'semantic_map_server/pub_goal',
    'index_map_service': 'semantic_map_server/index_map',
    'camera_service': 'semantic_map_builder/robot_camera_rgb',
    'navigate_action': 'navigate_to_pose',
    'compute_path_action': 'compute_path_to_pose',
    'footprint_radius': 0.8,                    # robot footprint radius (m), clearance from objects
    'facing_conf_threshold': 0.5,               # min facing_conf to approach an object from its front side
    'approach_conf_threshold': 0.4,             # min approach_conf to approach from the observed side
    'lies_flat_threshold': 0.6,                 # top_fraction above which an object has no front side
    'camera_max_width': 960,                    # px, images are downscaled before being sent to the LLM
}


def wrap_angle(theta):
    """Wraps an angle in radians to the interval [-pi, pi]."""
    return float((theta + np.pi) % (2 * np.pi) - np.pi)


def quaternion_from_euler(ai, aj, ak):
    ai /= 2.0
    aj /= 2.0
    ak /= 2.0
    ci, si = math.cos(ai), math.sin(ai)
    cj, sj = math.cos(aj), math.sin(aj)
    ck, sk = math.cos(ak), math.sin(ak)
    cc, cs = ci * ck, ci * sk
    sc, ss = si * ck, si * sk
    return np.array([cj * sc - sj * cs, cj * ss + sj * cc, cj * cs - sj * sc, cj * cc + sj * ss])


def _r(value, nd=2):
    return round(float(value), nd)


class RobotBridge:
    """ROS side of the MCP server: service/action clients, candidate cache and navigation state."""

    def __init__(self, node, params):
        self.node = node
        p = params.__getitem__
        robot_name = os.environ.get('YARP_ROBOT_NAME', '').lower() or 'ergocub'
        self.base_frame = p('robot_base_frame') or next(
            (base for key, base in ROBOT_BASES.items() if key in robot_name), 'geometric_unicycle')
        self.map_frame = p('map_frame')
        self.footprint_radius = p('footprint_radius')
        self.facing_conf_threshold = p('facing_conf_threshold')
        self.approach_conf_threshold = p('approach_conf_threshold')
        self.lies_flat_threshold = p('lies_flat_threshold')
        self.camera_max_width = p('camera_max_width')

        cb = ReentrantCallbackGroup()
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, node)
        self.query_client = node.create_client(QueryObjects, p('query_objects_service'), callback_group=cb)
        self.describe_client = node.create_client(DescribeArea, p('describe_area_service'), callback_group=cb)
        self.goal_client = node.create_client(PublishGoal, p('goal_pub_service'), callback_group=cb)
        self.index_client = node.create_client(IndexMap, p('index_map_service'), callback_group=cb)
        self.camera_client = node.create_client(CameraRGB, p('camera_service'), callback_group=cb)
        self.nav_client = ActionClient(node, NavigateToPose, p('navigate_action'), callback_group=cb)
        self.path_client = ActionClient(node, ComputePathToPose, p('compute_path_action'), callback_group=cb)
        self.marker_pub = node.create_publisher(Marker, '/llm_marker', 10)

        # id -> candidate dict (map frame), refreshed by every find_objects call
        self.candidates = {}
        # Navigation state of the last goal sent, updated from action callbacks
        self.nav_lock = threading.Lock()
        self.goal_handle = None
        self.nav = None
        self.nav_count = 0      # id of the last goal, tells a monitor its goal has been replaced
        self.last_goal = None   # [x, y, theta] map frame of the last goal sent
        node.get_logger().info(f"RobotBridge ready, base frame '{self.base_frame}'")

    # ---------------------------------------------------------------- ROS helpers
    def _wait(self, future, timeout):
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not future.done() and not done.wait(timeout):
            future.cancel()
            raise TimeoutError(f"no answer within {timeout:.0f}s")
        return future.result()

    def _call(self, client, request, timeout):
        if not client.wait_for_service(timeout_sec=2.0):
            raise RuntimeError(f"service '{client.srv_name}' is not available (is the node running?)")
        response = self._wait(client.call_async(request), timeout)
        if response is None:
            raise RuntimeError(f"service '{client.srv_name}' returned no response")
        return response

    def robot_pose(self):
        try:
            t = self.tf_buffer.lookup_transform(self.map_frame, self.base_frame, rclpy.time.Time(),
                                                timeout=Duration(seconds=1.0))
        except Exception as ex:
            raise RuntimeError(f"robot pose not available ({self.map_frame} -> {self.base_frame}): {ex}")
        q = t.transform.rotation
        theta = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        return [t.transform.translation.x, t.transform.translation.y, theta]

    @staticmethod
    def to_robot_frame(robot, x, y):
        dx, dy = x - robot[0], y - robot[1]
        c, s = math.cos(robot[2]), math.sin(robot[2])
        return c * dx + s * dy, -s * dx + c * dy

    @staticmethod
    def from_robot_frame(robot, fwd, left):
        c, s = math.cos(robot[2]), math.sin(robot[2])
        return robot[0] + c * fwd - s * left, robot[1] + s * fwd + c * left

    def _pose_msg(self, x, y, theta):
        msg = PoseStamped()
        msg.header.frame_id = self.map_frame
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.pose.position.x = float(x)
        msg.pose.position.y = float(y)
        q = quaternion_from_euler(0.0, 0.0, float(theta))
        msg.pose.orientation.x, msg.pose.orientation.y = float(q[0]), float(q[1])
        msg.pose.orientation.z, msg.pose.orientation.w = float(q[2]), float(q[3])
        return msg

    def _publish_goal_marker(self, x, y, theta):
        marker = Marker()
        marker.header.frame_id = self.map_frame
        marker.header.stamp = self.node.get_clock().now().to_msg()
        marker.ns = 'llm_goal'
        marker.id = 0
        marker.type = Marker.ARROW
        marker.action = Marker.ADD
        marker.pose = self._pose_msg(x, y, theta).pose
        marker.pose.position.z = 0.1
        marker.scale.x, marker.scale.y, marker.scale.z = 0.6, 0.1, 0.1
        marker.color.r, marker.color.g, marker.color.b, marker.color.a = 0.1, 0.9, 0.2, 1.0
        self.marker_pub.publish(marker)

    # ---------------------------------------------------------------- perception
    def find_objects(self, objects, max_candidates, area):
        request = QueryObjects.Request()
        request.queries = list(objects)
        request.max_candidates = int(max_candidates)
        request.roi = [float(v) for v in area] if area else []
        response = self._call(self.query_client, request, timeout=60.0)
        if not response.is_ok:
            raise RuntimeError(response.error_msg)
        robot = list(response.robot_pose)
        out, by_query = [], {}
        for result in response.results:
            prefix = result.query.strip().replace(' ', '_')
            # Drop stale ids of this query: "chair#3" must never survive from an older query
            self.candidates = {k: v for k, v in self.candidates.items() if not k.startswith(prefix + '#')}
            cands = []
            for rank, o in enumerate(result.objects):
                cand = {
                    "id": f"{prefix}#{rank + 1}",
                    "query": result.query,
                    "x": float(o.pose[0]), "y": float(o.pose[1]),
                    "z_min": float(o.z_min), "z_max": float(o.z_max),
                    "extent": float(o.extent), "length": float(o.length), "width": float(o.width),
                    "box_yaw": float(o.box_yaw),
                    "score": float(o.score), "purity": float(o.purity),
                    "peakiness": float(o.peakiness),
                    "facing_theta": float(o.facing_theta), "facing_conf": float(o.facing_confidence),
                    "approach_theta": float(o.approach_theta), "approach_conf": float(o.approach_confidence),
                    "lies_flat": bool(o.top_fraction > self.lies_flat_threshold),
                    "stamp": time.time(),
                }
                self.candidates[cand["id"]] = cand
                cands.append(cand)
            by_query[result.query] = cands
            out.append({"query": result.query,
                        "status": result.status or "ok",
                        "candidates": [self._describe_candidate(c, robot) for c in cands]})
        self._add_relations(out, by_query)
        return {"robot_pose": self._pose_dict(robot), "results": out}

    def _describe_candidate(self, c, robot):
        fwd, left = self.to_robot_frame(robot, c["x"], c["y"])
        d = {
            "id": c["id"],
            "pos_map": [_r(c["x"]), _r(c["y"])],
            "from_robot": {"forward": _r(fwd), "left": _r(left), "distance": _r(math.hypot(fwd, left)),
                           "bearing_deg": _r(math.degrees(math.atan2(left, fwd)), 0)},
            "size": _r(c["extent"]),
            "footprint": {"length": _r(c["length"]), "width": _r(c["width"]), "yaw": _r(c["box_yaw"])},
            "height": [_r(c["z_min"]), _r(c["z_max"])],
            "score": _r(c["score"], 3), "purity": _r(c["purity"], 3),
            "peakiness": _r(c["peakiness"], 3),
        }
        if c["facing_conf"] > 0.05:
            d["facing"] = {"theta": _r(c["facing_theta"]), "conf": _r(c["facing_conf"])}
        if c["approach_conf"] > 0.05:
            d["observed_from"] = {"theta": _r(c["approach_theta"]), "conf": _r(c["approach_conf"])}
        if c["lies_flat"]:
            d["lies_flat"] = True
        return d

    def _add_relations(self, out, by_query):
        """For each candidate, the nearest candidate of every other queried object, and support."""
        if len(by_query) < 2:
            return
        for entry in out:
            for cand_desc, cand in zip(entry["candidates"], by_query[entry["query"]]):
                rel = {}
                for other_query, others in by_query.items():
                    if other_query == entry["query"] or not others:
                        continue
                    best = min(others, key=lambda o: math.hypot(o["x"] - cand["x"], o["y"] - cand["y"]))
                    dist = math.hypot(best["x"] - cand["x"], best["y"] - cand["y"])
                    r = {"id": best["id"], "distance": _r(dist)}
                    # "on": resting on the other's top surface and inside its footprint
                    inside = dist <= max(best["length"], best["width"]) / 2 + 0.1
                    if inside and abs(cand["z_min"] - best["z_max"]) < 0.2 and cand["z_min"] > 0.2:
                        r["on_top_of_it"] = True
                    elif inside and abs(best["z_min"] - cand["z_max"]) < 0.2 and best["z_min"] > 0.2:
                        r["supports_it"] = True
                    rel[other_query] = r
                cand_desc["nearest"] = rel

    def describe_area(self, x, y, radius, extra_words):
        robot = self.robot_pose()
        center = [robot[0], robot[1]] if x is None or y is None else [x, y]
        request = DescribeArea.Request()
        request.center = [float(center[0]), float(center[1])]
        request.radius = float(radius)
        request.vocabulary = list(extra_words or [])
        response = self._call(self.describe_client, request, timeout=60.0)
        if not response.is_ok:
            raise RuntimeError(response.error_msg)
        items = []
        for lab in response.labels:
            fwd, left = self.to_robot_frame(robot, lab.pose[0], lab.pose[1])
            items.append({"label": lab.label, "pos_map": [_r(lab.pose[0]), _r(lab.pose[1])],
                          "from_robot": {"forward": _r(fwd), "left": _r(left),
                                         "distance": _r(math.hypot(fwd, left))},
                          "size": _r(lab.extent), "height": [_r(lab.z_min), _r(lab.z_max)],
                          "share": _r(lab.share, 3), "confidence": _r(lab.confidence)})
        return {"center": [_r(center[0]), _r(center[1])], "radius": _r(radius),
                "robot_pose": self._pose_dict(robot), "clusters": items}

    def highlight(self, category):
        request = IndexMap.Request()
        request.indexing_string = category
        response = self._call(self.index_client, request, timeout=60.0)
        if not response.is_ok:
            raise RuntimeError(response.error_msg)
        return f"'{category}' highlighted in RViz (red = match, blue = rest of the map)"

    def look(self):
        response = self._call(self.camera_client, CameraRGB.Request(), timeout=15.0)
        if not response.is_ok:
            raise RuntimeError(f"image not available: {response.error_msg}")
        h, w = int(response.rgb.height), int(response.rgb.width)
        raw = np.frombuffer(response.rgb.data, dtype=np.uint8)
        if h <= 0 or w <= 0 or raw.size % (h * w) != 0:
            raise RuntimeError("image not available: invalid buffer")
        img = raw.reshape(h, w, raw.size // (h * w))
        enc = str(response.rgb.encoding).lower()
        if img.shape[2] == 1:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif enc.startswith('rgb'):
            img = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2BGR)
        else:
            img = img[:, :, :3]
        if w > self.camera_max_width:
            img = cv2.resize(img, (self.camera_max_width, int(h * self.camera_max_width / w)))
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("jpeg encoding failed")
        return buf.tobytes(), img.shape[1], img.shape[0]

    # ---------------------------------------------------------------- goal geometry
    def candidate(self, cand_id):
        cand = self.candidates.get(cand_id)
        if cand is None:
            known = ", ".join(sorted(self.candidates)) or "none"
            raise ValueError(f"unknown candidate id '{cand_id}'. Known ids: {known}. Call find_objects first.")
        return cand

    def _half_depth(self, cand, direction):
        """Half-size of the object's oriented footprint along a unit direction (yaw)."""
        a = direction - cand["box_yaw"]
        return abs(math.cos(a)) * cand["length"] / 2 + abs(math.sin(a)) * cand["width"] / 2

    def plan_approach(self, cand_id, side='auto', clearance=None):
        """
        Goal pose to stand next to a candidate, facing it. The approach direction follows the
        same rules the previous prompt asked the LLM to apply, first match wins:
        1) front (facing_theta) if confident and the object does not lie flat,
        2) the side it was observed from (approach_theta) if confident: known reachable,
        3) the side toward the robot.
        The standoff is the object's half-depth along that direction plus the clearance, so large
        objects (tables, shelves) do not put the goal inside them.
        """
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}")
        cand = self.candidate(cand_id)
        robot = self.robot_pose()
        clearance = self.footprint_radius if clearance is None else float(clearance)
        to_robot = math.atan2(robot[1] - cand["y"], robot[0] - cand["x"])
        has_front = cand["facing_conf"] >= self.facing_conf_threshold and not cand["lies_flat"]
        if side == 'auto':
            if has_front:
                side, direction = 'front', cand["facing_theta"]
            elif cand["approach_conf"] >= self.approach_conf_threshold:
                side, direction = 'observed', cand["approach_theta"]
            else:
                side, direction = 'toward_robot', to_robot
        elif side == 'front':
            if not has_front:
                raise ValueError(f"{cand_id} has no reliable front (facing conf {cand['facing_conf']:.2f}"
                                 f"{', lies flat' if cand['lies_flat'] else ''}); use another side")
            direction = cand["facing_theta"]
        elif side == 'back':
            if not has_front:
                raise ValueError(f"{cand_id} has no reliable front, so 'back' is undefined; use another side")
            direction = cand["facing_theta"] + math.pi
        elif side == 'observed':
            direction = cand["approach_theta"]
        elif side == 'toward_robot':
            direction = to_robot
        else:
            # left/right as seen by the robot looking at the object from where it stands now
            direction = to_robot + (-math.pi / 2 if side == 'left' else math.pi / 2)
        direction = wrap_angle(direction)
        standoff = self._half_depth(cand, direction) + clearance
        gx = cand["x"] + standoff * math.cos(direction)
        gy = cand["y"] + standoff * math.sin(direction)
        theta = wrap_angle(direction + math.pi)
        return {"candidate": cand_id, "side": side, "standoff": _r(standoff), "goal": [gx, gy, theta]}

    def snap_goal(self, x, y, theta):
        request = PublishGoal.Request()
        request.goal_pose = [float(x), float(y), float(theta)]
        request.dry_run = True
        response = self._call(self.goal_client, request, timeout=15.0)
        if not response.is_ok or len(response.snapped_goal) < 3:
            raise RuntimeError(f"goal could not be placed on a free cell: {response.error_msg}")
        return list(response.snapped_goal)

    def path_length(self, x, y, theta):
        if not self.path_client.wait_for_server(timeout_sec=2.0):
            raise RuntimeError("Nav2 planner action is not available")
        goal = ComputePathToPose.Goal()
        goal.goal = self._pose_msg(x, y, theta)
        goal.use_start = False
        handle = self._wait(self.path_client.send_goal_async(goal), 10.0)
        if not handle.accepted:
            return None
        result = self._wait(handle.get_result_async(), 20.0)
        poses = result.result.path.poses
        if result.status != GoalStatus.STATUS_SUCCEEDED or len(poses) < 2:
            return None
        pts = np.array([[p.pose.position.x, p.pose.position.y] for p in poses])
        return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())

    def check_reachability(self, cand_ids, side):
        robot = self.robot_pose()
        out = []
        for cand_id in cand_ids:
            try:
                plan = self.plan_approach(cand_id, side)
                goal = self.snap_goal(*plan["goal"])
                length = self.path_length(*goal)
                cand = self.candidates[cand_id]
                out.append({"id": cand_id, "reachable": length is not None,
                            "path_length": _r(length) if length is not None else None,
                            "straight_distance": _r(math.hypot(cand["x"] - robot[0], cand["y"] - robot[1])),
                            "approach_side": plan["side"]})
            except Exception as ex:
                out.append({"id": cand_id, "reachable": False, "error": str(ex)})
        return {"robot_pose": self._pose_dict(robot), "candidates": out}

    # ---------------------------------------------------------------- navigation
    def navigate(self, x, y, theta, snap=True, label=''):
        if not self.nav_client.wait_for_server(timeout_sec=3.0):
            raise RuntimeError("Nav2 navigate_to_pose action is not available (is Nav2 running?)")
        requested = [float(x), float(y), float(theta)]
        goal_xyt = self.snap_goal(*requested) if snap else requested
        self.cancel(wait=True)
        goal = NavigateToPose.Goal()
        goal.pose = self._pose_msg(*goal_xyt)
        with self.nav_lock:
            # Detach the previous handle first: its late 'canceled' result must not overwrite this goal
            self.goal_handle = None
            self.nav_count += 1
            nav_id = self.nav_count
            self.nav = {"id": nav_id, "label": label, "goal": goal_xyt, "status": "sending", "feedback": {},
                        "started": time.time(), "error": ""}
        self._publish_goal_marker(*goal_xyt)
        handle = self._wait(self.nav_client.send_goal_async(goal, feedback_callback=self._on_feedback), 10.0)
        with self.nav_lock:
            if not handle.accepted:
                self.nav["status"] = "rejected"
                raise RuntimeError("Nav2 rejected the goal")
            self.goal_handle = handle
            self.nav["status"] = "active"
            self.last_goal = goal_xyt
        handle.get_result_async().add_done_callback(lambda fut, h=handle: self._on_result(h, fut))
        moved = math.hypot(goal_xyt[0] - requested[0], goal_xyt[1] - requested[1])
        return {"nav_id": nav_id, "label": label, "goal": self._pose_dict(goal_xyt), "snapped_by_m": _r(moved)}

    def _on_feedback(self, msg):
        fb = msg.feedback
        with self.nav_lock:
            if self.nav is not None:
                self.nav["feedback"] = {
                    "distance_remaining": _r(fb.distance_remaining),
                    "eta_s": _r(fb.estimated_time_remaining.sec, 0),
                    "recoveries": int(fb.number_of_recoveries),
                }

    def _on_result(self, handle, future):
        names = {GoalStatus.STATUS_SUCCEEDED: "succeeded", GoalStatus.STATUS_ABORTED: "aborted",
                 GoalStatus.STATUS_CANCELED: "canceled"}
        result = future.result()
        with self.nav_lock:
            # A newer goal may have replaced this one: only the current handle updates the state
            if self.goal_handle is not handle or self.nav is None:
                return
            self.nav["status"] = names.get(result.status, f"status_{result.status}")
            self.nav["error"] = getattr(result.result, 'error_msg', '') or ''
            self.nav["finished"] = time.time()
            self.goal_handle = None

    def nav_state(self, nav_id):
        """State of goal nav_id, None once a newer goal has replaced it."""
        with self.nav_lock:
            if self.nav is None or self.nav["id"] != nav_id:
                return None
            nav = dict(self.nav)
        out = {"status": nav["status"], "label": nav["label"], "goal": self._pose_dict(nav["goal"]),
               "elapsed_s": _r((nav.get("finished") or time.time()) - nav["started"], 0), **nav["feedback"]}
        if nav["error"]:
            out["error"] = nav["error"]
        try:
            robot = self.robot_pose()
            out["robot_pose"] = self._pose_dict(robot)
            out["distance_to_goal"] = _r(math.hypot(nav["goal"][0] - robot[0], nav["goal"][1] - robot[1]))
        except RuntimeError:
            pass
        return out

    def cancel(self, wait=False):
        """Cancel the active goal. With wait, returns False if Nav2 did not confirm the cancel."""
        with self.nav_lock:
            handle = self.goal_handle
        if handle is None:
            return True
        future = handle.cancel_goal_async()
        if wait:
            try:
                self._wait(future, 5.0)
            except TimeoutError:
                return False
        return True

    @staticmethod
    def _pose_dict(p):
        return {"x": _r(p[0]), "y": _r(p[1]), "theta": _r(p[2]), "theta_deg": _r(math.degrees(p[2]), 0)}


# ==================================================================== MCP server
class Yarp_mcpServer_Robot(Ros2NodeMixin, Yarp_mcpServer_Operations):
    """MCP server for the semantic map and Nav2. Tools are in map frame, meters and radians."""

    navigation_poll_interval = 0.5  # [s]

    def __init__(self, conf=None):
        self.bridge: RobotBridge = None     # created in _initialize(), before tool calls are served
        self._nav_operation_id = None       # operation of the last goal sent, for wait_for_navigation
        self.node = None
        self._executor_thread = None
        self._owns_rclpy = False
        self.params = dict(DEFAULT_PARAMS)
        if conf:
            conf.setDefault("server_name", "robot")
            conf.setDefault("mcp_port", 8765)
            for name, default in DEFAULT_PARAMS.items():
                if conf.check(name):
                    value = conf.find(name)
                    self.params[name] = (value.asString() if isinstance(default, str)
                                         else type(default)(value.asFloat64()))
        super().__init__(conf)

    def _initialize(self) -> bool:
        if not super()._initialize():
            return False
        # rclpy's own handlers would shut the context down on SIGINT, before cleanup() can cancel the goal
        if not rclpy.ok():
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
        self._initialize_ros()
        self._owns_rclpy = True
        self.bridge = RobotBridge(self.node, self.params)
        self._start_info_port()
        return True

    async def cleanup(self) -> None:
        # Reached twice on a signal: from the launcher, then from the MCP lifespan
        if self.bridge is None:
            return
        # Stopping the server must not leave the robot driving to a goal nobody watches
        await asyncio.to_thread(self.bridge.cancel, True)
        await self._cleanup_operations()
        self.bridge = None
        self._cleanup_ros()
        self._cleanup_base_resources()

    def _register_internal_tools(self) -> None:
        """Tools are declared as methods with @mcp_tool."""

    def _build_system_prompt_addendum(self) -> str:
        return (
            "ROBOT SERVER INSTRUCTIONS:\n"
            "Tools to perceive the semantic map and drive a mobile robot with Nav2. Positions are "
            "meters/radians in the 'map' frame unless a field says 'from_robot' (forward/left).\n"
            "- Put every object a request needs in ONE find_objects call, one physical object or room per "
            "string. Candidates are ranked by size, not match quality. Refer to them by id.\n"
            "- Never compute approach poses yourself: navigate_to_object does it.\n"
            "- Navigation tools return as soon as the goal is accepted, with an operation_id: the robot "
            "is still moving. get_operation_status reports progress and wait_for_navigation blocks until "
            "the goal ends. A new goal replaces the previous one.\n"
            "- 'stop', 'wait', 'halt' mean cancel_navigation, before anything else.\n"
            "- The robot cannot grasp or manipulate anything.")

    async def _run(self, fn, *args):
        # mcp only forwards the message of a ToolError: anything else reaches the agent as a bare
        # "Error executing tool", and the agent needs the reason (unknown id, Nav2 down, ...) to recover
        try:
            return await asyncio.to_thread(fn, *args)
        except ToolError:
            raise
        except Exception as ex:
            raise ToolError(f"{type(ex).__name__}: {ex}") from ex

    # ---------------------------------------------------------------- navigation operations
    async def _start_navigation(self, operation_type, send_goal) -> NavigationStartResult:
        """Run send_goal in a worker thread and, once Nav2 accepted, track the goal as an operation."""
        sent = await self._run(send_goal)
        operation = await self.operation_registry.create(
            operation_type,
            status_message=f"Goal accepted: {sent['label']}",
            details={"label": sent["label"], "goal": sent["goal"], "status": "active"},
            poll_interval_ms=round(self.navigation_poll_interval * 1000),
        )
        self._nav_operation_id = operation.operation_id
        await self._start_operation_task(
            operation, self._navigation_monitor_loop(operation.operation_id, sent["nav_id"]))
        return NavigationStartResult(
            operation_id=operation.operation_id,
            operation_type=operation.operation_type,
            status_uri=operation.status_uri,
            poll_interval_ms=operation.poll_interval_ms,
            message=f"Goal accepted: {sent['label']}",
            goal=sent["goal"],
            snapped_by_m=sent["snapped_by_m"],
            approach_side=sent.get("approach_side"),
        )

    async def _navigation_monitor_loop(self, operation_id, nav_id) -> None:
        """Mirror the Nav2 goal state into the operation until it ends or a newer goal replaces it."""
        last_key = None
        first_distance = None
        try:
            while True:
                state = await asyncio.to_thread(self.bridge.nav_state, nav_id)
                if state is None:
                    await self.operation_registry.update(
                        operation_id, status="cancelled", details={"superseded": True},
                        status_message="Replaced by a newer goal")
                    return
                status = state["status"]
                if status == "succeeded":
                    await self.operation_registry.update(
                        operation_id, status="completed", progress=1.0, details=state, result=state,
                        status_message="Goal reached")
                    return
                if status == "canceled":
                    await self.operation_registry.update(
                        operation_id, status="cancelled", details=state, status_message="Goal canceled")
                    return
                if status != "active":
                    message = f"Navigation ended with status: {status}"
                    await self.operation_registry.update(
                        operation_id, status="failed", details=state, status_message=message,
                        error={"message": state.get("error") or message})
                    return

                remaining = state.get("distance_remaining")
                if remaining and first_distance is None:
                    first_distance = remaining
                # Half-meter steps: every commit is an event for each subscribed host
                key = (state.get("recoveries"), None if remaining is None else round(remaining * 2))
                if key != last_key:
                    last_key = key
                    progress = None
                    if remaining is not None and first_distance:
                        progress = min(max(1.0 - remaining / first_distance, 0.0), 1.0)
                    await self.operation_registry.update(
                        operation_id, status="working", details=state, progress=progress,
                        status_message=f"Driving to {state['label']}")
                await asyncio.sleep(self.navigation_poll_interval)
        except asyncio.CancelledError:
            await self.operation_registry.update(
                operation_id, status="cancelled", status_message="Navigation monitor cancelled")
            raise
        except Exception as ex:
            self.log.error(f"Navigation monitor {operation_id} failed: {ex}")
            await self.operation_registry.update(
                operation_id, status="failed", error={"message": str(ex)},
                status_message=f"Navigation monitor failed: {ex}")

    async def _stop_robot(self) -> None:
        if not await self._run(self.bridge.cancel, True):
            raise ToolError("Nav2 did not confirm the cancel: the robot may still be moving")

    async def _cancel_operation(self, operation_id: str) -> OperationSnapshot:
        """Stop the robot before cancelling the operation that monitors it."""
        operation = await self.operation_registry.get(operation_id)
        if operation.status == "working" and operation.operation_type.startswith("navigation_"):
            await self._stop_robot()
        return await super()._cancel_operation(operation_id)

    # ---------------------------------------------------------------- perception tools
    @mcp_tool()
    async def find_objects(self, objects: list[str], max_candidates: int = 6,
                           area: list[float] | None = None) -> FindObjectsResult:
        """
        Locate objects in the semantic map. Put ALL objects needed for the request in ONE call.

        Each string names ONE physical object or ONE room/area; visual attributes are fine ("orange chair"),
        relations are not ("chair with backpack" -> ["chair", "backpack"]).
        Candidates are ranked by size (largest first), NOT by match quality. Each has an id (e.g. "chair#2")
        used by the other tools, pos_map, from_robot (forward/left/distance/bearing), size (m), footprint,
        height [z_min, z_max] above the ground, score (match strength), purity (low = mixed region),
        peakiness (near 0 = uniformly strong match throughout, higher = a sharp hotspot inside a weaker
        area, e.g. one real instance amid clutter), optional facing / observed_from yaw with confidence,
        lies_flat. With several objects, each candidate lists the nearest candidate of every other object
        (distance, on_top_of_it / supports_it).

        :param objects: object strings to search
        :param max_candidates: max candidates per object (largest first)
        :param area: optional [x, y, radius] in map frame to search only inside that circle
        """
        return await self._run(self.bridge.find_objects, objects, max_candidates, area)

    @mcp_tool()
    async def describe_area(self, x: float | None = None, y: float | None = None, radius: float = 2.5,
                            extra_words: list[str] | None = None) -> DescribeAreaResult:
        """
        Inventory of what the map contains around a point (default: around the robot): clusters of voxels
        labelled with their best-matching word from a fixed indoor vocabulary plus extra_words. Use it for
        "what is around you / near the table / in this room". Labels are coarse; confirm a specific object
        with find_objects before navigating to it.
        """
        return await self._run(self.bridge.describe_area, x, y, radius, extra_words)

    @mcp_tool()
    async def get_robot_pose(self) -> Pose2D:
        """Current robot pose in the map frame (x, y in meters, theta in radians and degrees)."""
        return RobotBridge._pose_dict(await self._run(self.bridge.robot_pose))

    @mcp_tool()
    async def check_reachability(self, candidate_ids: list[str], side: str = "auto") -> ReachabilityResult:
        """
        Nav2 path length to the approach pose of each candidate. Use it to pick the truly closest instance
        or to rule out unreachable ones before navigating. Slower than find_objects (one plan per candidate).
        """
        return await self._run(self.bridge.check_reachability, candidate_ids, side)

    @mcp_tool()
    async def plan_approach(self, candidate_id: str, side: str = "auto",
                            clearance: float | None = None) -> ApproachPlan:
        """
        Compute (without moving) the pose to stand next to a candidate, facing it.

        side: "auto" (front if known, else the side it was observed from, else toward the robot), "front",
        "back", "observed", "toward_robot", "left" / "right" (as seen from the robot's current position).
        clearance: distance kept from the object's edge (default: robot footprint radius).
        """
        def run():
            plan = self.bridge.plan_approach(candidate_id, side, clearance)
            plan["goal"] = RobotBridge._pose_dict(plan["goal"])
            return plan
        return await self._run(run)

    @mcp_tool()
    async def highlight_in_rviz(self, category: str) -> HighlightResult:
        """Show the voxels matching a category in RViz (for the human watching). Does not return positions."""
        return {"message": await self._run(self.bridge.highlight, category)}

    @mcp_tool()
    async def look(self) -> CallToolResult:
        """Current RGB image from the robot camera. Use it to visually confirm an object or describe the scene."""
        jpeg, width, height = await self._run(self.bridge.look)
        info = {"width": width, "height": height, "image_index": 1}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(info)),
                     ImageContent(type="image", data=base64.b64encode(jpeg).decode(), mime_type="image/jpeg")],
            structured_content=info)

    # ---------------------------------------------------------------- navigation tools
    @mcp_tool(structured_output=True)
    async def navigate_to_object(self, candidate_id: str, side: str = "auto",
                                 clearance: float | None = None) -> NavigationStartResult:
        """
        Drive next to a candidate from find_objects and face it (plan_approach + navigate). Returns an
        operation that tracks the goal, as soon as Nav2 accepts it.
        """
        def send():
            plan = self.bridge.plan_approach(candidate_id, side, clearance)
            sent = self.bridge.navigate(*plan["goal"], label=f"{candidate_id} ({plan['side']})")
            return {**sent, "approach_side": plan["side"]}
        return await self._start_navigation("navigation_object", send)

    @mcp_tool(structured_output=True)
    async def navigate_to(self, x: float, y: float, theta: float | None = None,
                          frame: str = "map") -> NavigationStartResult:
        """
        Drive to a pose. frame="map": x, y in map frame; frame="robot": x forward, y left of the robot now.
        theta (radians, same frame) defaults to facing the direction of travel. The goal is snapped to the
        closest free cell. Prefer navigate_to_object for objects and move_relative for small corrections.
        Returns an operation that tracks the goal, as soon as Nav2 accepts it.
        """
        def send():
            if frame not in ("map", "robot"):
                raise ValueError("frame must be 'map' or 'robot'")
            robot = self.bridge.robot_pose()
            gx, gy = (x, y) if frame == "map" else self.bridge.from_robot_frame(robot, x, y)
            if theta is None:
                gt = math.atan2(gy - robot[1], gx - robot[0])
            else:
                gt = theta if frame == "map" else theta + robot[2]
            return self.bridge.navigate(gx, gy, wrap_angle(gt), label=f"pose ({frame})")
        return await self._start_navigation("navigation_pose", send)

    @mcp_tool(structured_output=True)
    async def move_relative(self, forward: float = 0.0, left: float = 0.0, turn_deg: float = 0.0,
                            reference: str = "robot") -> NavigationStartResult:
        """
        Small corrections: "a bit more to the left", "step back", "turn around". forward/left in meters and
        turn_deg (positive = counter-clockwise) are applied in the frame of reference="robot" (where it
        stands now) or "last_goal" (the last goal sent, useful when the robot has not arrived yet).
        Returns an operation that tracks the goal, as soon as Nav2 accepts it.
        """
        def send():
            if reference == "last_goal":
                if self.bridge.last_goal is None:
                    raise ValueError("no previous goal: use reference='robot'")
                base = self.bridge.last_goal
            elif reference == "robot":
                base = self.bridge.robot_pose()
            else:
                raise ValueError("reference must be 'robot' or 'last_goal'")
            gx, gy = self.bridge.from_robot_frame(base, forward, left)
            gt = wrap_angle(base[2] + math.radians(turn_deg))
            return self.bridge.navigate(gx, gy, gt, label=f"relative f={forward} l={left} turn={turn_deg}")
        return await self._start_navigation("navigation_relative", send)

    @mcp_tool()
    async def wait_for_navigation(self, timeout_s: float = 60.0) -> OperationSnapshot:
        """
        Block until the last goal ends or timeout_s passes, and return its operation. status is "completed",
        "failed" or "cancelled" when it ended; "working" means the robot is still moving after the timeout:
        call this again or cancel_navigation.
        """
        operation_id = self._nav_operation_id
        if operation_id is None:
            raise ToolError("No navigation goal sent yet")
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                operation = await self.operation_registry.get(operation_id)
                if operation.status != "working" or time.monotonic() >= deadline:
                    return operation
                await asyncio.sleep(0.2)
        except OperationNotFoundError as ex:
            raise ToolError(f"Unknown operation: {operation_id}") from ex

    @mcp_tool()
    async def cancel_navigation(self) -> SuccessResult:
        """Stop the robot: cancel the active Nav2 goal and its operation."""
        await self._stop_robot()
        for operation in await self.operation_registry.list(active_only=True):
            if operation.operation_type.startswith("navigation_"):
                await self.operation_registry.cancel(operation.operation_id)
        return {"success": True}


def main():
    """Run this server alone. yarp_mcpServer_launcher is the usual way, see scripts/run_all.sh."""
    config = yarp.ResourceFinder()
    config.configure(sys.argv)
    Yarp_mcpServer_Robot(config).run()


if __name__ == '__main__':
    main()
