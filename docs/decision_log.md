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
