# SportAI cannot analyse HEVC — and the ingest OOM it caused (2026-08-08)

**Status: DIAGNOSED + GATED.** Two separate faults surfaced from one alert.
The vendor fault (HEVC) is **open and needs raising with SportAI**; the platform
faults (OOM headroom, no post-analysis gate) are fixed.

Trigger: `Web Service nextpoint-ingest-worker exceeded its memory limit`, and
task `42280d38` (`stoker.neil@gmail.com`, `tennis_singles`) stamped
`ingest died repeatedly (4 attempts) with no terminal state` by the SA orphan
sweep's poison-guard.

---

## ★ Finding 1 — SportAI produces nothing usable from HEVC/H.265

Measured over **every** payload in `s3://nextpoint-prod-uploads/raw-json/` (10):

| task | codec | fps (avg) | Mbps | min | players | rallies | bounces | ball conf | final conf |
|---|---|---|---|---|---|---|---|---|---|
| e4a74645 | h264 | 24.92 (25.03) | 2.2 | 53 | 3 | 101 | 1039 | 0.403 | **0.699** |
| 299013b3 | h264 | 60.0 (60.0) | 0.8 | 48 | 3 | 96 | 764 | 0.292 | **0.627** |
| 052786b4 | h264 | 25.0 (25.0) | 0.7 | 10 | 3 | 27 | 168 | 0.295 | **0.629** |
| c8b77210 | h264 | 25.0 (25.0) | 0.7 | 10 | 4 | 27 | 168 | 0.295 | **0.621** |
| 079d2c62 | h264 | 25.0 (25.0) | 0.7 | 10 | 4 | 24 | 162 | 0.298 | **0.650** |
| df594aea | h264 | 29.97 (30.0) | 15.3 | 74 | 3 | 9 | 982 | 0.293 | **0.618** |
| f7223270 | **hevc** | 30.0 (29.99) | 14.1 | 37 | 7 | **2** | 155 | 0.307 | **0.526** |
| 36625d04 | **hevc** | 30.0 (30.0) | 5.0 | 122 | 13 | **1** | 155 | 0.220 | **0.465** |
| 42280d38 | **hevc** | 30.0 (30.08) | 15.0 | 57 | **74** | **0** | **0** | 0.394 | **0.451** |
| 6abd37ca | **hevc** | 60.0 (59.91) | 20.2 | 33 | 2 | **0** | **0** | 0.169 | **0.408** |

**6/6 H.264 matches work. 0/4 HEVC matches work.** SportAI's own
`final_confidences.final` separates them with **no overlap** (h264 0.618–0.699,
hevc 0.408–0.526).

### It is not the footage, and not the bitrate

On `42280d38` the two real players were tracked across **82,511 and 84,822
frames at ~0.71 mean pose confidence** — full-match, high-quality player
tracking. Only the **ball detector** returned nothing (`ball_positions: 0`). The
other 72 "players" are phantoms with counts of 3, 8, 13, 17 frames.

Bitrate is ruled out: `df594aea` is **15.3 Mbps H.264** and works fine, while
`36625d04` is 5.0 Mbps HEVC and does not.

This is why the customer's video looks perfect on inspection — it *is* perfect.

### Why this matters commercially

iPhones record HEVC by default under Settings → Camera → Formats → **High
Efficiency**. Any customer who hasn't switched to "Most Compatible" is likely
uploading HEVC. **Open action: raise with SportAI as a vendor bug**, and
consider warning at upload time in the Media Room.

---

## ★ Finding 2 — a failed analysis was billed and announced as ready

A failed SportAI analysis returns **HTTP 200 with a well-formed payload**. Before
the gate below, that meant: ingest "succeeded" → silver built ~nothing → no trim
→ customer emailed "your video is ready" → **credit consumed** for an empty
dashboard.

`VIDEO_QUALITY_CHECK_ENABLED` gates the video *before* analysis. Nothing checked
what came back.

**Two matches hit this**: `42280d38` and `6abd37ca` (uploaded 2026-08-08 13:00).
`6abd37ca` was never flagged by anything — it has only 2 phantom players, so it
did not OOM, so nothing noticed. `42280d38` was only noticed because it crashed.

### The gate (`ingest_quality/`, commit `40d4fbe`)

Runs as **STEP 1c** in `ingest_worker_app._do_ingest` — *after* the raw archive,
so a rejected payload is retained for diagnosis.

- **REJECT** on the one unambiguous signal: **0 rallies AND 0 floor bounces** —
  no point structure for silver to derive from. Healthy matches measured ≥ 9
  rallies and ≥ 162 bounces; the two rejects have 0/0.
  - Deliberately **not** keyed on `ball_positions`: `6abd37ca` has 496 of them
    with 0 rallies and 0 bounces and is just as empty.
  - Deliberately **not** rate-based (rallies per minute): `df594aea` reports 9
    rallies over 74 minutes yet has **100 hand-verified real points**, so a rate
    rule would reject a match we hold ground truth for.
  - Deliberately **not** keyed on codec: HEVC is the *cause*, but the decision is
    made on the **output**, so a future SportAI fix needs no code change here.
- **WARN** (still ingests): suspect codec, `final` confidence below
  `INGEST_SANITY_MIN_FINAL_CONF` (0.55, sitting inside the observed gap), or a
  phantom-player storm (> 10 players).
- **Fails open**: only judges a payload it recognises. `"rallies": []` (ran,
  found none) and a *missing* `rallies` key (shape we don't understand) are
  distinguished, so a SportAI key rename cannot reject every match at once.
  `assess()` catches everything and returns OK on error.
- On reject: stamps `last_status='failed'` + `ingest_error`, emails ops, and
  returns **before** bronze, silver, trim, the billing sync and the customer
  notify — so an unanalysable match is neither billed nor announced.

Rollback `INGEST_SANITY_GATE_ENABLED=0` (still assesses and logs).
Check: `.venv/Scripts/python -m ingest_quality.tests.test_gate`.

---

## ★ Finding 3 — compressed size is not a proxy for ingest memory

The OOM was **not** caused by a big payload. It was caused by a *degenerate* one.

| task | gz | JSON | parsed dict | parse peak |
|---|---|---|---|---|
| `299013b3` (ingested fine) | 11.0 MB | 39.1 MB (×3.5) | 181 MB | 221 MB |
| `42280d38` (OOM ×4) | **5.8 MB** | 78.9 MB (**×13.5**) | **558 MB** | **637 MB** |

**The failing payload is half the compressed size of one that succeeded.**

Cause: 74 phantom players each carry a `location_heatmap` (424-row float grid).
Across all 74 that is **15.6 M cells, 97.7 % of them `0.0`** = **504 MB parsed =
90 % of the payload**. Runs of `0.0,0.0,0.0` gzip to almost nothing, but each
becomes a 24-byte Python float plus an 8-byte pointer. **The worse the tracking,
the better it compresses and the more RAM it costs.**

At 637 MB the parse alone exceeded the whole 512 MB `starter` instance, so it
died in `json.load` at STEP 1. **The fix was upgrading the ingest worker to
`standard` (2 GB)** — done 2026-08-08.

### Shipped alongside, but did NOT fix this task (commit `bf986b0`)

Three whole-payload re-serializations, all running while the parsed dict was
still live:

| site | cost |
|---|---|
| `raw_archive.archive_raw` — `gzip.compress(json.dumps(p).encode())` | +78.2 MB |
| `ingest_bronze._compute_session_uid` — full `json.dumps` purely to hash it | — |
| `ingest_bronze._persist_raw` — `json.dumps`, then `.encode("utf-8")` **twice** (sha, then gzip) | +111.4 MB |

All three now stream over `JSONEncoder.iterencode`: worst peak above the live
dict **111.4 MB → 13.4 MB**, worker traced peak **293 MB → 195 MB**. Verified
byte-identical on the real 39 MB payload — `payload_sha256`, `payload_len`,
gzip round-trip and **`session_uid`** all match (sha256 is a streaming
construction, so session identity is unchanged). `payload_len` stays a
*character* count, guarded with a non-ASCII case.

Real headroom for every match — but it runs *after* the parse that killed this
one. Don't record it as the fix.

> **Sub-lesson:** the archive step advertised itself as best-effort/never-fatal
> inside a `try/except`. An OOM is **SIGKILL, not an exception** — the caller
> could never contain it. A best-effort step must not be an allocation spike.

---

## Recovery

The poison-guard stamps `last_status='failed'`, so **`/ops/sweep-sa-orphans`
will not retry it** (it re-hits the `attempts >= SWEEP_SA_MAX_ATTEMPTS` branch).
Use the manual bypass, which ignores the attempt count and re-resolves a fresh
SportAI `result_url`:

```bash
curl -X POST https://api.nextpointtennis.com/ops/ingest-task \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"task_id":"<task_id>","mode":"worker"}'
```

## Open items

- **Raise HEVC with SportAI.** 4/4 broken is a vendor bug, not a tuning problem.
- **Warn on HEVC at upload** in the Media Room (or transcode before submit).
- **Refund `42280d38` + `6abd37ca`** — both billed for empty analyses. (Tomo.)
- Both predate the gate; re-running them will now reject rather than re-bill.

## How to reproduce any of this

```bash
aws s3 sync s3://nextpoint-prod-uploads/raw-json/ ./all/
# codec/quality table + gate verdicts:
python - <<'PY'
import gzip, json, glob, os, sys
sys.path.insert(0, ".")
from ingest_quality import assess, should_reject
for p in sorted(glob.glob("all/*")):
    raw = gzip.open(p,'rb').read() if p.endswith(".gz") else open(p,'rb').read()
    d = json.loads(raw); v = assess(d)
    print(os.path.basename(p)[:8], v.stats,
          "REJECT" if should_reject(v) else ("warn" if v.warnings else "pass"))
PY
```

Memory-profile any payload with `tracemalloc` around `json.loads` — **never**
infer memory from `aws s3 ls`.
