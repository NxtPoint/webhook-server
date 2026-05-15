# Ten-Fifty5 Support FAQ

This is the **single source of truth** for the support bot. The bot answers
ONLY from this file. Anything not covered here gets escalated to
info@ten-fifty5.com.

**Format**: each entry has a `## section.id` header, then `**Q:**` and the
answer in plain prose. Keep answers ≤ 100 words. Be direct, no marketing
fluff. Where an action is needed, name the exact UI path.

**Status**: Working set — original 5 entries plus ~15 seeded from documented
platform behaviour (account model, credits, coach invite, AI Coach paywall,
soft-delete). Edit or replace as real inbound questions arrive — these are
defaults, not customer research.

---

# Account

## account.cancel
**Q: How do I cancel my subscription?**

Go to your portal → Plans & Pricing tab → "Manage subscription". Cancellation
takes effect at the end of your current billing period; you keep full access
until then. We don't pro-rate refunds for partial months. If you have credits
left when you cancel, they stay on your account in case you come back.

## account.delete
**Q: Can I delete my account entirely?**

Yes — email info@ten-fifty5.com asking for account deletion. We'll respond
within 5 business days and walk you through it. This is a GDPR-compliant
erasure: all your match videos, dashboards, and personal data are permanently
removed.

## account.change_email
**Q: How do I change the email on my account?**

Email info@ten-fifty5.com from your current account email and tell us the new
one. Account email is tied to your billing identity and your match data, so we
make this change manually to avoid orphaning anything.

## account.linked_players
**Q: How do I add a child or another player to my account?**

In your portal go to **My Profile → Linked Players** and add them. Each linked
player gets their own profile (name, age, dominant hand, UTR) but shares your
account's credits. You can switch which player a match belongs to from the
Media Room before processing starts.

---

# Matches

## matches.upload
**Q: How do I upload a new match?**

In the portal, click **Upload Match** in the sidebar. Drag your video file
into the upload area, fill in the match details (date, players, location),
and submit. Uploads run in the background — you can navigate away and we'll
email you when it's ready, usually within 5–15 minutes.

## matches.not_showing
**Q: I uploaded a match but it's not showing in my dashboard. Why?**

Match processing takes 5–15 minutes for a typical match. If it's been over
an hour, the upload may have failed. Check **Upload Match → Status** to see
where it's stuck. If it shows "failed", email us with your task_id and we'll
look into it.

## matches.delete
**Q: How do I delete a match?**

Open the match in **Match Analysis**, click the menu in the top-right, and
choose Delete. The match disappears from your dashboards immediately. The
match credit you spent is **not** refunded — uploading and analysing the
match was the billable event. If you uploaded the wrong file by mistake,
email us and we'll sort it out.

## matches.video_format
**Q: What video formats and lengths are supported?**

MP4 is the safest bet — H.264 video, AAC audio. Most modern phones record in
this format by default. We can usually handle MOV too. For matches, aim for
the full match in one file; we'll auto-trim out dead time. Maximum file size
is around 5 GB. If your video won't upload, email us and we'll diagnose.

## matches.reprocess
**Q: Can I re-run the analysis on a match?**

Open the match, click the menu, and choose **Reprocess**. This re-runs the
silver build using the latest analysis logic — useful if we've shipped a
fix. Reprocessing does **not** charge another match credit (you've already
paid for the analysis); it just refreshes the dashboards.

## matches.trimmed_video
**Q: Where's the trimmed video of my match?**

The trimmed-down video (with dead time between points removed) appears in
the match's **Video** tab once processing finishes. We don't keep the
original full-length upload after trimming — only the trimmed review file
is stored.

---

# Plans & Credits

## credits.how_they_work
**Q: How do credits work?**

A "match credit" lets you upload one match for analysis. A "technique
credit" lets you upload one swing for technique analysis — these are
separate pools. PAYG credits never expire. Monthly subscription credits
reset on your billing date and **don't roll over** — use them or lose them.
Your remaining counts show on the **Plans & Pricing** page.

## credits.free_trial
**Q: What does the free trial include?**

When you sign up you get **1 free match credit** and **5 free technique
credits**, lifetime — they never expire. AI Coach is locked on the free
trial; you can see the interface but you'll need a paid plan to ask
questions. Once you've used your free match, your dashboards stay viewable
forever, even if you don't upgrade.

## credits.no_rollover
**Q: My monthly credits disappeared at the start of the new month. Bug?**

Not a bug — that's how monthly plans work. Unused match credits don't roll
over to the next period; you get a fresh allowance each month. If you need
credits that don't expire, look at the PAYG packs (1, 3, or 5 credits)
instead of a monthly subscription.

## credits.topup
**Q: I'm out of credits but I'm in the middle of a tournament. Can I top up?**

Yes — go to **Plans & Pricing** and pick a PAYG pack (1, 3, or 5 match
credits). Credits land on your account immediately, no waiting period. They
never expire, so anything you don't use this week stays for next time.

---

# AI Coach

## ai_coach.what_is
**Q: What is the AI Coach?**

The AI Coach analyses your match data and gives you direct, statistic-backed
coaching advice. It generates three "insight cards" automatically for every
match (key strengths, weaknesses, tactical tips), and you can also ask
freeform questions about your own play. It only coaches *you* — it won't
discuss the opponent's weaknesses.

## ai_coach.locked
**Q: Why is the AI Coach locked for me?**

AI Coach is included with every paid plan but **not** on the free trial.
You'll see the Coach tab and a sample of what it does, but the Ask button
is disabled. Upgrade to any paid plan (PAYG or monthly) and AI Coach
unlocks immediately for that account — no separate add-on required.

## ai_coach.daily_limit
**Q: I keep hitting a daily limit on the AI Coach. Why?**

There's a per-day cap to keep costs reasonable: 5 freeform questions per
match per day, and 20 freeform questions across all your matches per day.
The auto-generated insight cards on each match don't count toward this.
Limits reset at midnight UTC.

## ai_coach.opponent_questions
**Q: I asked about my opponent and the AI Coach refused to answer. Why?**

By design — the Coach exists to help you improve, so it focuses only on
what *you* can control. Asking "how do I beat them" or "what are their
weaknesses" gets a polite redirect. Ask about your serve, your error
patterns, your shot selection, your decision-making — that's where it's
strongest.

---

# Technique Analysis

## technique.what_is
**Q: What is Technique Analysis?**

Technique Analysis breaks down a single swing — your forehand, serve, etc.
— into biomechanical scores: kinetic chain timing, peak speeds, joint
angles, and where you land vs. coaching benchmarks. Upload a short video
(3–10 seconds) of one swing from a stable angle. Best results come from a
side-on view at about 1.5–2 metres distance.

## technique.swing_types
**Q: Which swing types are supported?**

Currently: forehand drive/topspin/slice, backhand drive/topspin/slice,
three serve types (flat, slice, kick), forehand and backhand volleys, and
overhead. Pickleball is detected by the API but not officially supported
yet. Pick the closest match in the upload form — the dropdown is in the
technique upload step.

## technique.credits_separate
**Q: I have match credits but the upload says I'm out of technique credits. Why?**

Match credits and technique credits are separate pools. The free trial
gives you 1 match + 5 techniques. Paid plans include unlimited technique
analysis on top of your match credits, so if you've upgraded and still see
this, refresh the page or email us.

---

# Coach Invites

## coaches.invite
**Q: How do I invite my coach?**

In your portal, go to the **Invite Coach** tab on your dashboard. Enter your
coach's email and click invite. They'll get an email with a one-click accept
link. Once they accept, they can view your match analysis and footage in
read-only mode. You can revoke their access any time from the same tab.

## coaches.what_they_see
**Q: What can my coach actually see?**

Everything you see in your dashboards — match analytics, placement
heatmaps, AI Coach insights, technique reports — for the players you've
linked them to. They cannot upload matches on your behalf, change your
account settings, or see your billing. Read-only access.

## coaches.revoke
**Q: How do I revoke a coach's access?**

In the **Invite Coach** tab, find the coach in the list and click Revoke.
Their access disappears immediately. If you change your mind, re-inviting
the same email sends them a fresh invite link — they don't lose their
account history.

## coaches.coach_pro_cap
**Q: I'm a coach and I'm getting a "Coach Pro required" message. What's that?**

Coaches get their first linked player free, forever. To work with more than
one player you need a Coach Pro subscription. The link in the message takes
you straight to the upgrade. Existing accepted invites are grandfathered —
the limit only kicks in on new invites once you're at the free cap.

---

# Troubleshooting

## trouble.upload_stuck
**Q: My upload bar is stuck or the page froze. What now?**

Refresh the page — uploads resume automatically if the file already
finished transferring. Most "stuck" cases are network blips during a slow
transfer. If a refresh doesn't help, try a different browser (Chrome is
most reliable) and a wired connection if you have one. Persistent failure
on the same file: email us with the file size and your browser.

## trouble.email_not_received
**Q: I didn't get the "your match is ready" email. Now what?**

Check spam — emails come from `noreply@ten-fifty5.com`. Otherwise the match
is probably ready anyway: open your portal and look at the Locker Room
dashboard. If the match shows up there, the analysis is done and the email
just got delayed or filtered. If it's not there after an hour, email us
with your task_id.
