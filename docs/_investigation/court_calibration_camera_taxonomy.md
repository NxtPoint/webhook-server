# Court calibration — camera-class taxonomy + breakage matrix

**Tier:** REFERENCE / investigation
**Dated:** 2026-05-28 (calibration research session)
**Companion to:** `docs/_investigation/court_calibration_silent_degeneracy.md` (root cause + architectural proposal) and `.claude/court_calibration_implementation_kickoff.md` (execution plan).
**Source data:** `.claude/tmp/calib_audit/audit.csv` (60 T5 jobs) + sample frames + live detector reproduction (this session).

---

## ⚠️ Headline: there is no camera diversity in production yet

Every one of the 60 most-recent T5 jobs is **1920×1080 h264** and from **a single uploader** (`tomo.stojakovic@gmail.com`). The product has **not onboarded real consumer-camera variety** (phones, GoPros, other users' tripods). So this taxonomy is **two-thirds forward-looking**: it documents the two real camera classes seen plus the classes we *will* hit, and proposes fixtures to acquire so the calibration bench can actually prove camera-agnosticism before those users arrive.

**Crucially, in the current data the failures do NOT correlate with camera class.** They correlate with the **calibration window** (which frames the lock saw). Every observed camera calibrates correctly when fed a representative window — see the companion doc's reproduction receipts. The breakage matrix below reflects that.

## Camera classes

| Class | Seen in prod? | Example | Lens character | Radial calib? | Notes |
|---|---|---|---|---|---|
| **A — Indoor broadcast-style (MATCHi)** | ✅ (most of corpus) | `a798eff0`, `880dff02`, `f11eed2c` | Wide-ish, mild barrel; fixed mount, court fills frame; scoreboard + sponsor overlays | **Yes** (`mode=radial`, ~11 obs) — actively corrects barrel | The bench-fixture court. Healthy ≈97 % ball-court when window is clean. |
| **B — Outdoor club (standard/wide)** | ✅ (match 4) | `ca475740` (60 fps, "Wilson" courts, green-on-terracotta, dawn) | Wider-than-standard ("extended" lens per Tomo), rectilinear-ish; backlit hazy far court at dawn | **Yes** when keypoints good (13/14, conf 0.93) | CNN works on rally frames; only the opening setup frames fail. Fully calibratable. |
| **C — Phone wide-angle / ultra-wide** | ❌ not yet | — | Moderate barrel, `k1≈-0.1…-0.3`; misleading EXIF on "ultra-wide" mode | Brown-Conrady (k1,k2) likely sufficient | **Coming with onboarding.** Fix E target. Needs a fixture to acquire. |
| **D — GoPro / action fisheye** | ❌ not yet | — | Strong barrel/fisheye, ~120–180° FOV | Needs **fisheye (Kannala-Brandt)** — current radial model insufficient | Fix E auto-escalation target. Needs a fixture. |
| **E — Handheld / panning** | ❌ not yet | — | Any FOV + **motion** (breaks the fixed-camera lock-once assumption) | N/A — violates "calibrate once" | Out of scope for v1; flagged for the temporal-voting design to not assume a static lock forever. |

## Breakage matrix (current production, from `audit.csv`)

| task_id | class | res / fps | ball-court % | silver | court_confidence | locked | Verdict |
|---|---|---|---|---|---|---|---|
| `880dff02`, `a798eff0`, +8 canonical | A | 1080p/25 | **97.2 %** | 160 | 0.86 | VALIDATED | ✅ healthy (clean window) |
| `ad763368`, `3de6e8d5`, `a7164ca7`, `28ad9271`, `eee5e0ed` | A | 1080p/25 | **99.9 %** | 162 | 1.00 | VALIDATED (fallback flag set) | ✅ best — clean window |
| `9378f2dd`, `c645a7ee` | A/B (Dejan) | 1080p/30 | **25.7 %** | 371 | 0.86 | partial | ⚠️ mediocre window |
| `78c32f53` | A | 1080p/25 | **32.7 %** | 110 | 0.86 | partial | ⚠️ mediocre window |
| `1d6feb3a` | A | 1080p/25 | **28.2 %** | 7 | 0.86 | partial | ⚠️ mediocre window |
| `0e929b55`, `bcf7c607`, `4c14c1c4` | A | 1080p/25 | **32.3 %** | 63 | 1.00 | partial | ⚠️ mediocre window |
| `a458a67b`, `c2d7a03c`, `8afdccb9`, `aac1670c` | A | 1080p/25 | **48.9 %** | 3 | 1.00 | partial | ⚠️ mediocre window |
| **`f11eed2c`** | A | 1080p/25 | **0.0 %** | 0 | 0.79 (=11/14) | degenerate lock | ❌ **catastrophic — window trap** (same court as healthy `880dff02`) |
| **`ca475740` (match 4)** | B | 1080p/60 | **0.0 %** | 0 | 0.79 (=11/14) | degenerate Hough `ANY-BEST` | ❌ **catastrophic — window trap** (CNN finds 0/14 on opening frames, 13/14 on rally) |

**Reading:** the spectrum (97 % → 49 % → 32 % → 28 % → 0 %) tracks **calibration-window quality**, not camera class. Class A both tops the table (97–99.9 %) and bottoms it (0 %). The two `conf=0.79` rows are the degenerate locks. Coverage is not monotonic in fps, resolution, or confidence.

## Proposed regression-fixture set (for the new `bench_calib`)

One canonical clip per class, plus the negative "window-trap" case. Each fixture stores: the opening calibration window frames + a few rally frames + known reference-point pixel→court correspondences for projection assertions.

| Fixture | Source | Purpose / assertion |
|---|---|---|
| `fixture_indoor_matchi` | `880dff02` (or existing `a798eff0` bench fixture) | Class A baseline — must lock VALIDATED + radial, project reference points within tolerance, coverage ≥95 %. |
| `fixture_outdoor_club` | `ca475740` rally frames (5–95 %) | Class B — must lock VALIDATED + radial (proves the outdoor green/red dawn court calibrates). |
| **`fixture_window_trap`** | `ca475740` **first 300 frames** | **Negative case** — must NOT lock a degenerate Hough `ANY-BEST`; must keep searching / fail-loud. This is the regression guard for the actual bug. |
| `fixture_phone_wide` | **TO ACQUIRE** (record on a phone ultra-wide, or borrow) | Class C — must estimate Brown-Conrady (k1,k2), lock VALIDATED, project within tolerance. |
| `fixture_gopro_fisheye` | **TO ACQUIRE** (GoPro tennis clip) | Class D — must auto-escalate to fisheye model and lock; proves Fix E camera-agnosticism. |

**Action item for Tomo / onboarding:** to validate "100 % across cameras" we need real Class C/D footage. Cheapest path: record one short tennis clip each on (a) a phone in ultra-wide mode and (b) a GoPro, from a typical 3–5 m side mount. Until then, Fix E can only be validated on the radial path (Class A/B); the fisheye path is unverified.

## Cross-references
- `docs/_investigation/court_calibration_silent_degeneracy.md` — root cause + re-prioritised fix set A–H + architectural proposal.
- `.claude/court_calibration_implementation_kickoff.md` — next-session execution plan.
- `project_t5_apr15_breakthrough.md` (memory) — origin of the radial Brown-Conrady calibration (`camera_calibration.py`).
