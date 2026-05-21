# Decision Log

Use this file to keep a durable record of key decisions from working sessions.
Commit and push updates so they are available from any computer.

## Entry Template
- Date:
- Context:
- Decision:
- Why:
- Alternatives considered:
- Follow-up actions:
- Owner:
- Status:

---

## 2026-05-21
- Context: Session continuity across computers.
- Decision: Add one-command handoff snapshots and maintain this decision log in-repo.
- Why: Chat history may not be available on every machine; git-tracked notes are durable.
- Alternatives considered: Rely on memory/chat UI history only.
- Follow-up actions:
  1. Run `./scripts/end_session_snapshot.sh` at end of sessions.
  2. Add key decisions here before commit/push.
- Owner: bobmauck
- Status: active

## Assess Gateway reboot impact session
- How to prevent RTC problems

Can we change the Fallback logic somehow? having correct RTC time is essential to the whole system.
Monday 10:32 AM

Yes, and you should.

If RTC correctness is essential, compile-time fallback is too risky for production. Better policy:

Remove automatic compile-time RTC.adjust(...) in normal runtime.
On invalid RTC, enter a TIME_INVALID state:
no uploads, no filename/date decisions
keep collecting raw data to safe buffer/file if possible
Require a trusted time source before resuming:
host SET_TIME (preferred), or
validated NTP (if internet path is intended)
Persist/flag sync status:
rtc_valid, last_sync_source, last_sync_epoch, last_sync_result
Add strict sanity gates:
reject large backward jumps unless explicitly forced
verify GET_TIME stable after set
Expose this in UI:
show “RTC invalid: blocked” in bsm_web so it’s obvious.
That turns silent bad-time operation into a safe fail-closed mode, which is what you want here.

It changes the priority, but not the core recommendation.

Given your flow (reboot/calibration/upload, then host sets RTC, then runs ~24h), the critical risk window is boot before host sync. So:

Keep fail-closed for time-critical actions before sync
Don’t let pre-sync RTC drive day/file naming logic that must be correct.
Treat host SET_TIME after upload as mandatory checkpoint
After sync, set rtc_valid=true for the 24h run window.
On next boot, require resync quickly
If RTC sanity fails at boot, wait for host sync instead of compile-time fallback.
You can still run calibration if it does not depend on absolute date correctness.
Compile-time fallback should be lab-only
Disable in field builds (or gate behind a debug flag).

