# SPDX-FileCopyrightText: 2024 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
# Author: Simone Micheletti

"""
Concrete output schemas of the robot MCP tools.

The MCP SDK drops undeclared keys and rejects missing required ones, so every field a tool returns
in robot_server.py must be declared here (NotRequired when it is only sometimes present).
"""

from typing_extensions import NotRequired, TypedDict

from yarp_mcpServer_core.lib_server.operation_models import StartOperationResult


class Pose2D(TypedDict):
    x: float
    y: float
    theta: float
    theta_deg: float


class FromRobot(TypedDict):
    forward: float
    left: float
    distance: float
    bearing_deg: NotRequired[float]


class Footprint(TypedDict):
    length: float
    width: float
    yaw: float


class Heading(TypedDict):
    theta: float
    conf: float


class NearestRelation(TypedDict):
    id: str
    distance: float
    on_top_of_it: NotRequired[bool]
    supports_it: NotRequired[bool]


class Candidate(TypedDict):
    id: str
    pos_map: list[float]
    from_robot: FromRobot
    size: float
    footprint: Footprint
    height: list[float]
    score: float
    purity: float
    peakiness: float
    facing: NotRequired[Heading]
    observed_from: NotRequired[Heading]
    lies_flat: NotRequired[bool]
    nearest: NotRequired[dict[str, NearestRelation]]


class QueryResult(TypedDict):
    query: str
    status: str
    candidates: list[Candidate]


class FindObjectsResult(TypedDict):
    robot_pose: Pose2D
    results: list[QueryResult]


class AreaCluster(TypedDict):
    label: str
    pos_map: list[float]
    from_robot: FromRobot
    size: float
    height: list[float]
    share: float
    confidence: float


class DescribeAreaResult(TypedDict):
    center: list[float]
    radius: float
    robot_pose: Pose2D
    clusters: list[AreaCluster]


class CandidateReachability(TypedDict):
    id: str
    reachable: bool
    path_length: NotRequired[float | None]
    straight_distance: NotRequired[float]
    approach_side: NotRequired[str]
    error: NotRequired[str]


class ReachabilityResult(TypedDict):
    robot_pose: Pose2D
    candidates: list[CandidateReachability]


class ApproachPlan(TypedDict):
    candidate: str
    side: str
    standoff: float
    goal: Pose2D


class HighlightResult(TypedDict):
    message: str


class NavigationStartResult(StartOperationResult):
    """Accepted goal: the standard operation fields plus where the robot is actually going."""

    goal: Pose2D
    snapped_by_m: float
    approach_side: str | None = None
