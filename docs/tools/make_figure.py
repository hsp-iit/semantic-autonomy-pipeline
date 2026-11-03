# SPDX-FileCopyrightText: 2026 Humanoid Sensing and Perception, Istituto Italiano di Tecnologia
# SPDX-License-Identifier: BSD-3-Clause
"""Draws docs/diagrams/autonomy_pipeline.png. Boxes sit on a hand-placed grid, so no arrow crosses any text."""
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'diagrams', 'autonomy_pipeline.png')
NEW, EXISTING, LATER = '#D6E8FA', '#E3F1DC', '#F1F1F1'
fig, ax = plt.subplots(figsize=(11, 9.2))
ax.set_xlim(0, 11)
ax.set_ylim(0, 9.2)
ax.axis('off')


def box(x, y, w, h, text, color, dashed=False):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle='round,pad=0.02,rounding_size=0.08',
                                facecolor=color, edgecolor='#555555', linestyle='--' if dashed else '-'))
    ax.text(x, y, text, ha='center', va='center', fontsize=10.5)


def arrow(x0, y0, x1, y1, label=None, side=0.1):
    ax.annotate('', xy=(x1, y1), xytext=(x0, y0), arrowprops=dict(arrowstyle='->', color='#333333', lw=1.3))
    if label:
        ax.text((x0 + x1) / 2 + side, (y0 + y1) / 2, label, fontsize=8.5, color='#333333', va='center')


box(4.0, 8.65, 5.0, 0.6, 'Web chat, speech', NEW)
box(4.0, 7.35, 5.0, 0.8, 'Chatter (Azure or Ollama)\nfew short turns, never drives the robot', NEW)
box(4.0, 5.95, 5.0, 0.8, 'Mission server\none mission at a time, stop_all', NEW)
box(4.0, 4.55, 5.0, 0.8, 'Orchestrator (Sonnet)\none background claude process', NEW)
box(1.6, 3.05, 2.6, 0.8, 'Navigator agent (Sonnet)', NEW)
box(4.4, 3.05, 1.8, 0.8, 'Camera\nserver', NEW)
box(7.0, 3.05, 2.2, 0.8, 'Approach server', NEW)
box(9.6, 3.05, 1.8, 0.8, 'Grasping,\nhead control', LATER, dashed=True)
box(1.6, 1.75, 2.6, 0.7, 'Robot MCP server', NEW)
box(4.4, 1.75, 1.8, 0.7, 'Camera topic', EXISTING)
box(7.0, 1.75, 2.2, 0.7, 'manipulation-approach', EXISTING)
box(1.6, 0.5, 2.6, 0.6, 'Semantic map, Nav2', EXISTING)
box(7.0, 0.5, 2.2, 0.6, 'Nav2, arms', EXISTING)

arrow(4.0, 8.33, 4.0, 7.77, 'LlmChat')
arrow(4.0, 6.93, 4.0, 6.37, 'MCP: start_mission')
arrow(4.0, 5.53, 4.0, 4.97, 'claude -p')
arrow(2.6, 4.13, 1.8, 3.47, 'Agent', -0.72)
arrow(4.2, 4.13, 4.4, 3.47, 'look', 0.12)
arrow(5.8, 4.13, 6.8, 3.47, 'MCP')
arrow(1.6, 2.63, 1.6, 2.12)
arrow(4.4, 2.12, 4.4, 2.63)
arrow(7.0, 2.63, 7.0, 2.12, '/reach_object')
arrow(1.6, 1.38, 1.6, 0.82)
arrow(7.0, 1.38, 7.0, 0.82)

ax.text(8.9, 7.35, 'The chatter calls stop_all, also with\nno LLM for a bare "stop". It kills the\norchestrator, then cancels\napproach and navigation',
        fontsize=9, color='#AA3333', ha='center', va='center')
for x, color, name in ((7.5, NEW, 'semantic-autonomy-pipeline'), (7.5, EXISTING, 'Other repositories, robot'), (7.5, LATER, 'Later')):
    y = {NEW: 5.2, EXISTING: 4.85, LATER: 4.5}[color]
    ax.add_patch(FancyBboxPatch((x, y - 0.1), 0.3, 0.2, boxstyle='round,pad=0.01', facecolor=color, edgecolor='#555555'))
    ax.text(x + 0.42, y, name, fontsize=9.5, va='center')
os.makedirs(os.path.dirname(OUT), exist_ok=True)
fig.savefig(OUT, dpi=140, bbox_inches='tight', facecolor='white')
