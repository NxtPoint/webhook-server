# T5 Market & Academic Scan — 2026-05-20

**Audience:** Tomo. Investment-grade decisions, not a literature review.
**Produced by:** background research agent during Stream B of the 2026-05-20 infrastructure session.
**Scope:** SOTA single-camera tennis ball tracking, commercial competitors, datasets, annotation services, multi-task model viability — applied to T5's actual state (13% ball coverage on validation match, TrackNetV2 backbone, $0.05/match cost target).

---

## TL;DR — most actionable findings

1. **WASB-tennis weights are MIT-licensed and on Google Drive.** Drop-in candidate to replace TrackNetV2. WASB beat TrackNetV2 **86.2% vs 77.2% F1** on the same tennis benchmark — ~9 pts F1 for zero training cost. Repo: https://github.com/nttcom/WASB-SBDT (see MODEL_ZOO.md, weights via `setup_scripts/setup_weights.sh` → `wasb_tennis_best.pth.tar`). **Action: integrate this week**, before any further self-labeling.
2. **TrackNetV3 weights exist but are badminton-only** (shuttlecock). https://github.com/qaz812345/TrackNetV3 — MIT, weights on Google Drive. **Not transferable to tennis without retraining**. Confirms: no public V3-tennis weights anywhere — you'd have to train them.
3. **TrackNetV4** (Sept 2024, https://arxiv.org/abs/2409.14543) is a *motion-attention plug-in on top of V2/V3*, not a new backbone. Improves V2/V3 F1 on tennis + shuttlecock. No tennis weights published. Cheap stacking move if WASB underperforms.
4. **TOTNet** (Aug 2024, https://arxiv.org/abs/2508.09650, code: https://github.com/AugustRushG/TOTNet) — best 2024-2025 result for our exact problem. **Fully-occluded frame accuracy 0.63 → 0.80, RMSE 37.30 → 7.19**. Built for the 13%-coverage class.
5. **SwingVision has the same failure modes we'd worry about**: net-cord drops mis-tracked, serve speeds underestimated ~10%, scoring 10+ points wrong per 20 sets, shot-filter broken, watch app glitchy. **Our $0.05/match target undercuts their $14.99/mo by 300×.**
6. **Roboflow Outsource: ~$0.10/bbox, $0.05/keypoint.** 10 fully-labeled matches (150k keypoints) ≈ **$7,500**. Self-labeling via dual-submit (SportAI as teacher) costs SportAI credits, no cash.

---

## 1. Commercial competitors

### SwingVision
- **Single iPhone/iPad**, on-device, Apple Neural Engine, no internet required. Founders ex-Tesla Autopilot + Cisco/LinkedIn. $6M Series A (Tennis Australia/Sony/Techstars). ([prweb](https://www.prweb.com/releases/swingvision-scores-6-million-series-a-to-bring-ai-to-athletes-301960272.html))
- **Patent**: granted for "single-camera line calling AI" / 3D ball trajectory from one device. Public press names the patent but the USPTO filing number was not pulled in this scan. ([tennis.com](https://www.tennis.com/baseline/articles/fair-play-swingvision-puts-its-electronic-line-calling-to-the-test-in-tournament-play), [parentingaces](https://parentingaces.com/articles/swingvision-junior-tournaments/))
- **No public engineering blog / arXiv preprint** — only job listings, LinkedIn, and one Encord webinar landing page. They don't talk about their stack publicly. ([encord](https://encord.com/webinar/swingvision/))
- **Claimed accuracy**: 97% line calls within 10cm of the line — self-reported. ([techinthesun](https://techinthesun.com/swingvision/))
- **Pricing**: free tier 2 hrs/mo; Pro **$14.99/mo or $179.99/yr**; family plan recently added. ([swing.vision/subscribe](https://swing.vision/subscribe))
- **Open-sourced**: nothing of substance located in scan.
- **Failure modes from App Store / review aggregators** ([justuseapp](https://justuseapp.com/en/app/989461317/swingvision-a-i-tennis-app/reviews), [App Store](https://apps.apple.com/us/app/swingvision-tennis-pickleball/id989461317)):
  - Serve speed often off; **groundstroke speed underestimated ~10% vs Hawkeye**.
  - **Can't handle balls that hit the net** before dropping.
  - "99% accurate line calls" excludes shots not near a line (misleading).
  - **Scoring**: 10+ wrong points and 1-2 wrong games per ~20 sets recorded.
  - Shot filter ("only forehands") doesn't work; mis-identifies ball-machine sessions as matches.
  - Apple Watch port "almost unusable" on watchOS 5.

**What this means for T5:** SwingVision's review-revealed weaknesses (net-cord, serve-speed calibration, scoring, filtering) are *exactly the silver-layer business-logic problems we already own* via the gold view architecture — those are SQL problems, not ML problems. The differentiator isn't "beat Tesla-Autopilot alums on ML" — it's "good-enough ML + better aggregation + 1/300th the price." Stop chasing perfection on ball detection; close enough is enough if the rest of the pipeline is solid.

### PlaySight / Hawk-Eye / Wingfield / In-Out / Tenex

| System | Cameras | Price | Latency | Target |
|---|---|---|---|---|
| **Hawk-Eye** | 6-10 fixed broadcast | ~$60k/court ([CNN](https://www.cnn.com/2017/03/21/tennis/tennis-tech-gadget)) | live | Grand Slams |
| **PlaySight** | 5-6 fixed | up to $12.5k install + **$500/mo** ([RacketSource](https://www.racketsource.com/articles/how-much-does-playsight-tennis-cost-6012c40e/)) | live | Elite clubs / academies |
| **Wingfield** | Fixed court box | **€3,600-4,500/yr, 24-mo min** ([wingfield](https://www.wingfield.io/en/products), [Ubitennis](https://www.ubitennis.net/2021/01/tennis-and-data-methods-used-to-collect-information-and-how-much-each-one-cost/2/)) | live | European clubs |
| **In/Out** | Net-post + line devices (**not** single-camera) | $275 starter, multi-device for accuracy ([gadgetsandwearables 2026](https://gadgetsandwearables.com/2026/04/26/in-out-tennis-line-calling/)) | live | Recreational, line calls only |
| **SwingVision** | Single iPhone/iPad | **$15/mo** | near-live | Consumer |
| **Tenex** | not found in scan | — | — | — |
| **Tenniix** | Dual 4K on a *ball machine*, not a court system ([tenniix.ai](https://tenniix.ai/)) | hardware purchase | live | Practice partner |

Note: "Tenex" did not surface. Closest hits: "Tenniix" (AI ball machine, not court analytics) and "Tennis AI" (motion-analysis app). If "Tenex" is a real competitor, give the scan a pointer and re-run.

**What this means for T5:** Screaming gap between SwingVision ($15/mo, single camera, consumer) and PlaySight ($12.5k + $500/mo). The only single-camera competitor in our cost class is SwingVision. PlaySight/Hawk-Eye/Wingfield aren't addressable at $0.05/match — they're a different product. Position T5 as "SwingVision-equivalent on footage you already filmed, no iPhone propped on a fence required."

---

## 2. Academic SOTA for single-camera tennis ball tracking, 2024-2026

### WASB (Widely Applicable Strong Baseline) — BMVC 2023 — **most actionable**
- Paper: https://arxiv.org/abs/2311.05237 · Repo: https://github.com/nttcom/WASB-SBDT · **License: MIT**
- **Tennis F1 = 86.2%** at Step=1, vs TrackNetV2 = 77.2% on the same benchmark.
- **Pretrained tennis weights** via `setup_scripts/setup_weights.sh` → `pretrained_weights/wasb_tennis_best.pth.tar` (Google Drive).
- Sports covered: soccer, tennis, badminton, volleyball, basketball.
- **Model size / T4 FPS: not published in MODEL_ZOO.md** — measure after download. (MODEL_ZOO lists 6 methods × 5 sports but omits sizes and FPS.)

### TrackNetV3 (qaz812345) — **badminton only**
- Repo: https://github.com/qaz812345/TrackNetV3 · MIT · weights on Google Drive.
- Shuttlecock; **does not transfer to tennis without retraining**.
- This is the arch we ported to `ml_pipeline/tracknet_v3.py`. Confirms: no public V3-tennis weights exist that I could find.

### TrackNetV4 — Sept 2024
- Paper: https://arxiv.org/abs/2409.14543 · landing https://tracknetv4.github.io/
- **Plug-in over V2/V3** — motion-attention maps + frame differencing. Reported to improve V2 and V3 F1 on tennis + shuttlecock.
- No tennis weights published.
- Stacking-friendly: applies to whichever TrackNet backbone you keep.

### TOTNet — Aug 2024 — **best occlusion result**
- Paper: https://arxiv.org/abs/2508.09650 · Repo: https://github.com/AugustRushG/TOTNet
- 3D conv + visibility-weighted loss + occlusion-aware augmentation.
- **Fully-occluded frame accuracy 0.63 → 0.80, RMSE 37.30 → 7.19** vs prior SOTA — across tennis, badminton, table tennis benchmarks.
- Code + eval scripts + TTA dataset public.

### TTNet — CVPR 2020 (table tennis, multi-task)
- Paper: https://arxiv.org/abs/2004.09927 · ball detection + segmentation + event spotting in one CNN. **<6ms/frame on a consumer GPU**, 97% event accuracy on its dataset. Reference design for §5.

### Other 2024-2025 notable
- TrackNetV4-tennis reproduction notes: https://hackmd.io/@XDU5dRI5RJOkP6SWYyGUQA/HkQA5OZaJg
- YOLO-based tennis tracking (MDPI): https://www.mdpi.com/2673-4591/134/1/25 — YOLOv12 + MobileNetV2 + Kalman, representative mainstream not breakthrough.
- BlurBall (table tennis blur estimation): https://arxiv.org/html/2509.18387v1.
- "Automated Tennis Player and Ball Tracking with Court Keypoints" (Nov 2025): https://arxiv.org/abs/2511.04126 — YOLOv8 + custom YOLOv5 + ResNet50. **Identical architecture to ours.**
- RacketVision benchmark (Nov 2025): https://arxiv.org/abs/2511.17045 — multi-racket-sport ball+racket+trajectory. New, no production weights yet.

**What this means for T5:** Decision tree: (a) **WASB this week** for easy F1 lift; (b) if 13% coverage doesn't move enough, **TOTNet next** because it directly targets occluded frames; (c) TrackNetV4 attention layer is the cheap stacking move if you stay on TrackNet family. **Skip TrackNetV3 self-training** unless WASB+TOTNet both stall — V3's claimed gains over V2 are already matched/exceeded by WASB.

---

## 3. Tennis-specific datasets

### TrackNet original (NCTU / Tsing Hua)
- https://nol.cs.nctu.edu.tw/ndo3je6av9/ — 10 broadcast videos, **19,835 labeled frames**, 1280×720 @ 30fps. Labels: frame, visibility, X, Y, trajectory pattern.
- Drive: https://drive.google.com/drive/folders/11r0RUaQHX7I3ANkaYG4jOxXK1OYo01Ut
- **License unclear** — page reads academic-release; verify before commercial use.

### Tennis-21 — not located
- Did not surface a clean canonical "Tennis-21" in this scan. Likely the dataset inside WASB's tennis benchmark folder. If you have a specific paper reference, share it and re-scan.

### TOTNet's TTA dataset
- Bundled with TOTNet repo: 9,159 samples, 1,996 occlusion cases. **Table tennis**, but the occlusion-augmentation protocol transfers.

### Not available
- No 2024-2026 large-scale broadcast tennis ball-tracking dataset surfaced that exceeds TrackNet's 19,835 frames in scale. Newer tennis releases are bundled with method repos, not standalone.

**What this means for T5:** Datasets are not the bottleneck — TrackNet's 19,835 frames + WASB's recipe is enough to seed a competitive model. For commercial-clean licensing, label your own (→ §4).

---

## 4. Annotation services + tools

### Pricing (per-label, normalised)

| Service | Bounding box | Keypoint / polygon | Notes |
|---|---|---|---|
| **Roboflow Outsource** | $0.10/bbox | $0.05/keypoint, $0.20/polygon | 1,000 free on Growth ([blog](https://blog.roboflow.com/outsourced-labeling-roboflow/), [pricing](https://roboflow.com/pricing)). Subscription $249-$299/mo on top. |
| **Scale AI** | $0.02-$0.04/bbox typical; $0.03-$1.00 range; enterprise **$93k-$400k+/yr** ([scale.com/pricing](https://scale.com/pricing)) | $0.06+ | Bulk only. Enterprise sales motion. Will not engage on <$20k. |
| **CVAT** | Self-hosted free; CVAT online tiered ([cvat.ai/pricing](https://www.cvat.ai/pricing/cvat-online)) | same | Tool, not labelers. You bring labor. |
| **Labelbox** | Enterprise/sales | — | No public per-frame. AL-focused. |
| **Mechanical Turk** | $0.01-$0.05/task | — | Cheapest, lowest quality. Tennis-ball-on-grass harder than AMT prices imply. |

No platform publishes tennis-aware templates — you build the schema yourself.

### Realistic cost: 5-10 fully labeled matches (1 ball-keypoint × 15,000 frames/match)

| Volume | Roboflow ($0.05/keypoint) | Scale AI (mid: $0.03) | CVAT self-labeled |
|---|---|---|---|
| **5 matches (75k)** | ~$3,750 | ~$2,250 | tool free + ~125-250 hrs labor |
| **10 matches (150k)** | ~$7,500 | ~$4,500 | tool free + ~250-500 hrs labor |

If annotators need a bbox UI (often required for sub-pixel ball precision), Roboflow doubles to ~$7,500 / ~$15,000.

**What this means for T5:** Real cost to fully label 10 matches via Roboflow Outsource is **~$7,500 (keypoint) or ~$15,000 (bbox)**. **Self-labeling via dual-submit (SportAI as teacher per `project_dual_submit_strategy.md`) is functionally free** beyond SportAI credit budget and gives 10× more labeled frames per dollar. **Don't pay for human annotation unless WASB + dual-submit both stall.** The one case worth paying for is **occluded-frame labels** — humans see through occlusion better than SportAI, and that's where TOTNet's gains come from.

---

## 5. Multi-task models (ball + court + player in one network)

### What exists
- **TTNet (CVPR 2020)** — table tennis. Ball + segmentation + event-spotting in one CNN. <6ms/frame on a consumer GPU. https://arxiv.org/abs/2004.09927
- **"Automated Tennis Player and Ball Tracking with Court Keypoints" (Nov 2025)** — three separate models (YOLOv8 + custom YOLOv5 + ResNet50). **Identical architecture to ours.** https://arxiv.org/abs/2511.04126
- **Pose2Trajectory (2024)** — Transformer fuses body-pose with ball trajectory. Multi-*modal*, not multi-*task*. https://arxiv.org/html/2411.04501v1
- **RacketVision (Nov 2025)** — multi-task benchmark, no production weights yet. https://arxiv.org/abs/2511.17045
- General multi-task sports tracking: https://arxiv.org/pdf/2401.09942

### Would unifying buy us anything?

| Property | Unified multi-task | Our current (YOLOv8 + TrackNetV2 + CNN+Hough) |
|---|---|---|
| **Latency** | Shared backbone, fewer GPU passes (TTNet <6ms). | Three passes — but g4dn.xlarge is **not GPU-bound** for us; Batch cost is dominated by serialisation/IO. |
| **Accuracy** | Shared rep can help under-labeled tasks. But Pose2Trajectory found "naive concat of pose features degrades performance" — gains require careful cross-attention design. | Each model specialised. Court strong, ball weak. Multi-task wouldn't fix the *fundamental* ball problem (small + occluded). |
| **Training data efficiency** | Big win **only if labels are joint** (one frame labeled for all tasks). Dual-submit already gives us joint labels. | Each model retrained separately. |
| **Engineering risk** | Single point of failure; hard to A/B individual changes. | Already modular — can swap ball model (WASB) without touching player or court. |

**What this means for T5:** Unified multi-task is **not** the unblock. Our 13% ball-detection coverage is a *ball-model* problem (small + occluded), and unified architectures don't magically solve that — **TOTNet does**, by being purpose-built for occlusion. **Keep the separate-models architecture; it lets us hot-swap ball detector (WASB → TOTNet) without retraining player or court.** Multi-task is only worth revisiting if dual-submit produces joint labels *and* we have an under-labeled task — but ball detection isn't under-labeled, it's under-architected for occlusion. Stay modular.

---

## Sources

- SwingVision pricing: https://swing.vision/subscribe
- SwingVision App Store reviews: https://justuseapp.com/en/app/989461317/swingvision-a-i-tennis-app/reviews
- SwingVision $6M Series A: https://www.prweb.com/releases/swingvision-scores-6-million-series-a-to-bring-ai-to-athletes-301960272.html
- SwingVision patent / tournament play: https://www.tennis.com/baseline/articles/fair-play-swingvision-puts-its-electronic-line-calling-to-the-test-in-tournament-play
- SwingVision review (techinthesun): https://techinthesun.com/swingvision/
- Encord SwingVision webinar landing (no transcript extracted): https://encord.com/webinar/swingvision/
- PlaySight pricing: https://www.racketsource.com/articles/how-much-does-playsight-tennis-cost-6012c40e/
- Hawk-Eye pricing: https://www.cnn.com/2017/03/21/tennis/tennis-tech-gadget
- Wingfield products: https://www.wingfield.io/en/products
- Wingfield pricing summary: https://www.ubitennis.net/2021/01/tennis-and-data-methods-used-to-collect-information-and-how-much-each-one-cost/2/
- In/Out review (2026): https://gadgetsandwearables.com/2026/04/26/in-out-tennis-line-calling/
- Tenniix (closest hit to "Tenex"): https://tenniix.ai/
- WASB repo: https://github.com/nttcom/WASB-SBDT
- WASB MODEL_ZOO.md: https://github.com/nttcom/WASB-SBDT/blob/main/MODEL_ZOO.md
- WASB paper: https://arxiv.org/abs/2311.05237
- TrackNetV3 (badminton, qaz812345): https://github.com/qaz812345/TrackNetV3
- TrackNetV4 paper: https://arxiv.org/abs/2409.14543
- TrackNetV4 site: https://tracknetv4.github.io/
- TOTNet repo: https://github.com/AugustRushG/TOTNet
- TOTNet paper: https://arxiv.org/abs/2508.09650
- TTNet paper: https://arxiv.org/abs/2004.09927
- Tennis player + ball + court (Nov 2025): https://arxiv.org/abs/2511.04126
- RacketVision (Nov 2025): https://arxiv.org/abs/2511.17045
- Pose2Trajectory: https://arxiv.org/html/2411.04501v1
- TrackNet original dataset: https://nol.cs.nctu.edu.tw/ndo3je6av9/
- TrackNet dataset Drive: https://drive.google.com/drive/folders/11r0RUaQHX7I3ANkaYG4jOxXK1OYo01Ut
- Roboflow outsource labeling: https://blog.roboflow.com/outsourced-labeling-roboflow/
- Roboflow pricing: https://roboflow.com/pricing
- Scale AI pricing: https://scale.com/pricing
- CVAT pricing: https://www.cvat.ai/pricing/cvat-online

---

## Scan gaps (flagged for re-scan if pointer provided)

1. **"Tenex"** did not match a real competitor in this scan — closest hits Tenniix (AI ball machine) and Tennis AI (motion analysis). Confirm the name and re-scan if it's a real player.
2. **"Tennis-21"** did not surface as a standalone dataset — likely the dataset bundled in WASB's tennis benchmark folder. Confirm source if it's distinct.
