# Session Handoff Template

Use this file to preserve state before closing/restarting.  
Fill it in, commit it, and share it in the next chat.

## 1) Session Metadata
- Date:
- Operator:
- Branch:
- Latest commit hash:
- Pushed to remote: `yes/no`

## 2) Environment
- Active network profile (`config/network_profile.json`): `field/home/...`
- Controller host IP:
- Discover IP:
- Isolated or home network:
- Other heavy network traffic present: `yes/no`

## 3) Running Processes (at end of session)
- `bsm_network.py` running: `yes/no`
- `bsm_web.py` running: `yes/no`
- Any second controller host/process active: `yes/no`
- If yes, details:

## 4) Arduino Fleet Snapshot
- Devices expected online:
- Devices actually discovered:
- Any devices with blank `firmware_version`:
- Any device with RTC issues:

## 5) Known Issues / Observations
- Symptom:
- Exact terminal/serial evidence:
- Frequency:
- Suspected cause:
- What was ruled out:

## 6) File Transfer Behavior
- Current selection policy (day/prefix):
- Any repeated `LIST_FILES timeout`:
- Any `Chunk offset mismatch`:
- Any partial files created in `data/files/<short_uid>/`:
- For SD swap, was `data/file_logs/<short_uid>.csv` reset: `yes/no`

## 7) Config and Code Changes Made
- Files changed:
  - 
- Why changed:
- Any temporary diagnostics added:
- Any diagnostics to remove later:

## 8) Commands Used
```bash
# bsm_network start command used
python3 bsm_network.py ...

# bsm_web start command used
python3 bsm_web.py
```

## 9) Logs to Keep
- Saved log files:
  - 
- Important snippets:
  - 

## 10) Next Actions
1. 
2. 
3. 

## 11) Questions for Next Session
- 
