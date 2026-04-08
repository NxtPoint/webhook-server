"""
ml_pipeline/heatmaps.py — Generate ball landing and player position heatmaps.

Produces 2D court-view heatmaps as PNG images:
  - Ball landing heatmap (bounce positions on court)
  - Player position heatmaps (one per player)
"""

import io
import logging
from typing import List, Dict

import numpy as np
import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from ml_pipeline.config import (
    COURT_LENGTH_M,
    COURT_WIDTH_SINGLES_M,
    COURT_WIDTH_DOUBLES_M,
    SERVICE_BOX_DEPTH_M,
)

logger = logging.getLogger(__name__)

# Court dimensions (metres, ITF standard)
_HALF_LENGTH = COURT_LENGTH_M / 2
_HALF_SINGLES = COURT_WIDTH_SINGLES_M / 2
_HALF_DOUBLES = COURT_WIDTH_DOUBLES_M / 2
_SERVICE_LINE = SERVICE_BOX_DEPTH_M


def _draw_court(ax):
    """Draw a top-down tennis court outline on the given axes."""
    ax.set_xlim(-_HALF_DOUBLES - 1, _HALF_DOUBLES + 1)
    ax.set_ylim(-_HALF_LENGTH - 1, _HALF_LENGTH + 1)
    ax.set_aspect("equal")
    ax.set_facecolor("#2d5016")

    lw = 1.5
    white = "#FFFFFF"

    # Doubles sidelines
    ax.plot([-_HALF_DOUBLES, -_HALF_DOUBLES], [-_HALF_LENGTH, _HALF_LENGTH], color=white, lw=lw)
    ax.plot([_HALF_DOUBLES, _HALF_DOUBLES], [-_HALF_LENGTH, _HALF_LENGTH], color=white, lw=lw)

    # Singles sidelines
    ax.plot([-_HALF_SINGLES, -_HALF_SINGLES], [-_HALF_LENGTH, _HALF_LENGTH], color=white, lw=lw)
    ax.plot([_HALF_SINGLES, _HALF_SINGLES], [-_HALF_LENGTH, _HALF_LENGTH], color=white, lw=lw)

    # Baselines
    ax.plot([-_HALF_DOUBLES, _HALF_DOUBLES], [-_HALF_LENGTH, -_HALF_LENGTH], color=white, lw=lw)
    ax.plot([-_HALF_DOUBLES, _HALF_DOUBLES], [_HALF_LENGTH, _HALF_LENGTH], color=white, lw=lw)

    # Service lines
    ax.plot([-_HALF_SINGLES, _HALF_SINGLES], [-_SERVICE_LINE, -_SERVICE_LINE], color=white, lw=lw)
    ax.plot([-_HALF_SINGLES, _HALF_SINGLES], [_SERVICE_LINE, _SERVICE_LINE], color=white, lw=lw)

    # Center service line
    ax.plot([0, 0], [-_SERVICE_LINE, _SERVICE_LINE], color=white, lw=lw)

    # Net
    ax.plot([-_HALF_DOUBLES, _HALF_DOUBLES], [0, 0], color="#cccccc", lw=2, linestyle="--")

    # Center marks
    ax.plot([0, 0], [-_HALF_LENGTH, -_HALF_LENGTH + 0.3], color=white, lw=lw)
    ax.plot([0, 0], [_HALF_LENGTH - 0.3, _HALF_LENGTH], color=white, lw=lw)

    ax.set_xticks([])
    ax.set_yticks([])


def generate_ball_heatmap(ball_detections, title="Ball Landing Heatmap") -> bytes:
    """
    Generate a ball bounce/landing heatmap on a 2D court diagram.

    Args:
        ball_detections: list of objects with .court_x, .court_y, .is_bounce attributes
        title: plot title

    Returns:
        PNG image as bytes
    """
    bounces = [d for d in ball_detections
               if d.is_bounce and d.court_x is not None and d.court_y is not None]

    fig, ax = plt.subplots(1, 1, figsize=(6, 12), dpi=150)
    _draw_court(ax)

    if bounces:
        xs = [b.court_x for b in bounces]
        ys = [b.court_y for b in bounces]

        # Scatter with transparency
        ax.scatter(xs, ys, c="#ff6600", alpha=0.6, s=40, edgecolors="#cc4400", linewidths=0.5, zorder=5)

        # 2D density if enough points
        if len(bounces) >= 5:
            try:
                from scipy.stats import gaussian_kde
                xy = np.vstack([xs, ys])
                kde = gaussian_kde(xy, bw_method=0.3)

                xgrid = np.linspace(-_HALF_DOUBLES, _HALF_DOUBLES, 100)
                ygrid = np.linspace(-_HALF_LENGTH, _HALF_LENGTH, 200)
                X, Y = np.meshgrid(xgrid, ygrid)
                Z = kde(np.vstack([X.ravel(), Y.ravel()])).reshape(X.shape)

                ax.contourf(X, Y, Z, levels=10, cmap="YlOrRd", alpha=0.4, zorder=3)
            except Exception as e:
                logger.warning(f"KDE failed, showing scatter only: {e}")

        ax.set_title(f"{title}\n({len(bounces)} bounces)", color="white", fontsize=12, pad=10)
    else:
        ax.set_title(f"{title}\n(no bounce data)", color="white", fontsize=12, pad=10)

    fig.patch.set_facecolor("#1a1a1a")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_player_heatmap(player_detections, player_id: int,
                            title: str = None) -> bytes:
    """
    Generate a player position heatmap on a 2D court diagram.

    Args:
        player_detections: list of objects with .player_id, .court_x, .court_y
        player_id: which player to filter for
        title: plot title (auto-generated if None)

    Returns:
        PNG image as bytes
    """
    pts = [d for d in player_detections
           if d.player_id == player_id and d.court_x is not None and d.court_y is not None]

    if title is None:
        title = f"Player {player_id} Position Heatmap"

    fig, ax = plt.subplots(1, 1, figsize=(6, 12), dpi=150)
    _draw_court(ax)

    colors = {0: "#00aaff", 1: "#ff4444"}
    color = colors.get(player_id, "#ffaa00")

    if pts:
        xs = [p.court_x for p in pts]
        ys = [p.court_y for p in pts]

        ax.scatter(xs, ys, c=color, alpha=0.3, s=15, zorder=5)

        if len(pts) >= 10:
            try:
                from scipy.stats import gaussian_kde
                xy = np.vstack([xs, ys])
                kde = gaussian_kde(xy, bw_method=0.3)

                xgrid = np.linspace(-_HALF_DOUBLES, _HALF_DOUBLES, 100)
                ygrid = np.linspace(-_HALF_LENGTH, _HALF_LENGTH, 200)
                X, Y = np.meshgrid(xgrid, ygrid)
                Z = kde(np.vstack([X.ravel(), Y.ravel()])).reshape(X.shape)

                cmap = "Blues" if player_id == 0 else "Reds"
                ax.contourf(X, Y, Z, levels=10, cmap=cmap, alpha=0.5, zorder=3)
            except Exception as e:
                logger.warning(f"KDE failed for player {player_id}: {e}")

        ax.set_title(f"{title}\n({len(pts)} positions)", color="white", fontsize=12, pad=10)
    else:
        ax.set_title(f"{title}\n(no position data)", color="white", fontsize=12, pad=10)

    fig.patch.set_facecolor("#1a1a1a")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_all_heatmaps(result) -> Dict[str, bytes]:
    """
    Generate all heatmaps from an AnalysisResult.

    Returns:
        dict mapping filename → PNG bytes:
            "ball_heatmap.png" → bytes
            "player_heatmap_0.png" → bytes
            "player_heatmap_1.png" → bytes
    """
    heatmaps = {}

    # Ball landing heatmap
    heatmaps["ball_heatmap.png"] = generate_ball_heatmap(result.ball_detections)

    # Player position heatmaps
    player_ids = sorted(set(d.player_id for d in result.player_detections))
    for pid in player_ids:
        key = f"player_heatmap_{pid}.png"
        heatmaps[key] = generate_player_heatmap(result.player_detections, pid)

    logger.info(f"Generated {len(heatmaps)} heatmap(s)")
    return heatmaps
