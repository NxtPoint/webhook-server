"""
test_pipeline.py — End-to-end test of the ML tennis analysis pipeline.
Run: python -m ml_pipeline.test_pipeline
"""

import time
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Resolve test video path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_VIDEO = os.path.join(SCRIPT_DIR, "test_videos", "test_rally.mp4")


def main():
    if not os.path.isfile(TEST_VIDEO):
        logger.error(f"Test video not found: {TEST_VIDEO}")
        sys.exit(1)

    from ml_pipeline.pipeline import TennisAnalysisPipeline

    logger.info(f"Test video: {TEST_VIDEO}")
    logger.info("Loading models...")
    t_load = time.time()
    pipeline = TennisAnalysisPipeline()
    logger.info(f"Models loaded in {time.time() - t_load:.1f}s")

    logger.info("Running pipeline...")
    result = pipeline.process(TEST_VIDEO)

    # Print results
    print("\n" + "=" * 60)
    print("ML PIPELINE TEST RESULTS")
    print("=" * 60)
    print(f"Video:              {os.path.basename(result.video_path)}")
    if result.video_metadata:
        m = result.video_metadata
        print(f"Resolution:         {m.width}x{m.height}")
        print(f"Duration:           {m.duration_sec:.1f}s")
        print(f"Original FPS:       {m.fps:.1f}")
    print(f"Frames processed:   {result.total_frames_processed}")
    print(f"Ball detection %:   {result.ball_detection_rate * 100:.1f}%")
    print(f"Court detected:     {'YES' if result.court_detected else 'NO'} "
          f"(confidence={result.court_confidence:.2f}, fallback={result.court_used_fallback})")
    print(f"Players found:      {result.player_count}")
    print(f"Bounces:            {result.bounce_count} (in={result.bounces_in}, out={result.bounces_out})")
    print(f"Rallies:            {result.rally_count}")
    print(f"Avg rally length:   {result.avg_rally_length:.1f} bounces")
    print(f"Serves:             {result.serve_count}")
    print(f"First serve %:      {result.first_serve_pct:.1f}%")
    print(f"Max ball speed:     {result.max_speed_kmh:.1f} km/h")
    print(f"Avg ball speed:     {result.avg_speed_kmh:.1f} km/h")
    print(f"Processing time:    {result.processing_time_sec:.1f}s")
    print(f"ms/frame:           {result.ms_per_frame:.1f}")
    print(f"Frame errors:       {result.frame_errors}")
    print("=" * 60)

    # Assertions — pipeline must not crash and must produce results
    assert result.total_frames_processed > 0, "No frames processed"
    assert result.frame_errors < result.total_frames_processed * 0.5, "Too many frame errors"
    assert result.court_detected, "Court not detected"
    # Note: YOLO won't detect players in synthetic video (rectangles != people).
    # On real footage, player_count should be 2.
    if result.player_count == 0:
        print("\nWARNING: No players detected (expected on synthetic test video — "
              "YOLO requires real human figures)")
    assert result.ball_detection_rate > 0, "No ball detections at all"
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
