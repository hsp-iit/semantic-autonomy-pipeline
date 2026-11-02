# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Scripted robot MCP server for live_dry_run.py: the tool names of the real one, with one table and one mug.
Every call is appended to a log with its time. While the file <log>.hold exists, a goal never arrives."""
import json
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

LOG, PORT = sys.argv[1], int(sys.argv[2])
mcp = MCPServer(name='robot')
cancelled = False


def log(name, **args):
    with open(LOG, 'a') as out:
        out.write(json.dumps({'t': time.time(), 'tool': name, 'args': args}) + '\n')


def candidate(cid, x, z, size):
    return {'id': cid, 'pos_map': [x, 0.0], 'from_robot': {'forward': x, 'left': 0.0, 'distance': x}, 'size': size,
            'height': z, 'score': 0.8, 'purity': 0.9}


@mcp.tool()
def find_objects(objects: list[str], max_candidates: int = 6, area: list[float] | None = None) -> dict:
    """Locate objects in the semantic map."""
    log('find_objects', objects=objects)
    found = {}
    for name in objects:
        if 'mug' in name or 'cup' in name:
            found[name] = [dict(candidate('mug#1', 3.0, [0.75, 0.85], [0.08, 0.08]),
                                nearest={'table': {'id': 'table#1', 'distance': 0.1, 'on_top_of_it': True}})]
        elif 'table' in name:
            found[name] = [candidate('table#1', 3.0, [0.0, 0.75], [1.5, 0.8])]
        else:
            found[name] = []
    return {'candidates': found}


@mcp.tool()
def navigate_to_object(candidate_id: str, side: str = 'auto', clearance: float | None = None) -> dict:
    """Drive next to a candidate and face it. Returns as soon as the goal is accepted."""
    global cancelled
    log('navigate_to_object', candidate_id=candidate_id)
    cancelled = False
    return {'operation_id': 'nav1', 'message': f'Goal accepted: {candidate_id}'}


@mcp.tool()
def wait_for_navigation(timeout_s: float = 60.0) -> dict:
    """Block until the last goal ends."""
    log('wait_for_navigation')
    deadline = time.time() + min(timeout_s, 20.0)
    while time.time() < deadline and os.path.exists(LOG + '.hold') and not cancelled:
        time.sleep(0.1)
    if cancelled:
        return {'status': 'cancelled'}
    if os.path.exists(LOG + '.hold'):
        return {'status': 'working'}
    return {'status': 'completed', 'status_message': 'Goal reached'}


@mcp.tool()
def cancel_navigation() -> dict:
    """Cancel the active goal."""
    global cancelled
    log('cancel_navigation')
    cancelled = True
    return {'success': True}


for name in ('describe_area', 'get_robot_pose', 'check_reachability', 'plan_approach', 'highlight_in_rviz',
             'navigate_to', 'move_relative', 'get_operation_status', 'list_operations'):
    mcp.add_tool(lambda: {'error': 'not available in the dry run'}, name=name, description='Not available in the dry run.')

mcp.run(transport='streamable-http', host='127.0.0.1', port=PORT)
