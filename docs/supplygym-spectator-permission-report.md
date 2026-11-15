# SupplyGym spectator feed permission failure

Observed on 15 November 2026 while running PR 37 locally with Ubuntu 24.04 under WSL2, Docker, Harbor, and Sonnet 5.5.

## Failure

The agent's first successful-looking purchasing request failed with:

```text
Permission denied: '/tmp/spectator-events.jsonl'
```

Subsequent tool calls returned `Episode is closed`. The live event file remained empty, Harbor reported zero reward, and the runner reported `missing_verified_result`. The viewer stayed at “Waiting for the first completed week”.

This was an infrastructure failure. It should be excluded from agent performance comparisons.

## Cause and portability

The runner pre-created a host-owned event file and bind-mounted it into the sidecar at `/tmp/spectator-events.jsonl`. The sidecar runs as UID 10001, while the host file belonged to UID 1000. The host file already had mode 0666.

The evidence is consistent with Linux protections on opening another user's existing file with creation flags inside a sticky, world-writable directory such as `/tmp`. The spectator uses Python append mode, which includes a creation flag. This kernel setting was not inspected directly, so the exact mechanism remains an inference.

Other Linux or WSL installations using the same mount and ownership arrangement could encounter this. Frequency has not been measured. Changing ordinary file permissions alone may be insufficient.

## Local fix and verification

In the demo runner's `configure_spectator`, change both the bind-mount target and `SPECTATOR_PATH` to:

```text
/app/spectator-events.jsonl
```

The mount remains confined to the simulator sidecar. The host output file and the viewer contract are unchanged.

After this change, the scripted Harbor check observed week 1 while the job was running, completed all 53 weeks, and produced 54 spectator records including the final event. Harbor verified annual operating surplus of $149,262.70 and reward 0.748591. The corrected Sonnet rerun requires separate verification.

## Requested development changes

- Apply the mount-path correction to PR 37 and update the corresponding path assertions.
- Exercise the live check on Linux with differing host and container UIDs.
- Check spectator writability before accepting agent decisions, and surface initialization failures as infrastructure errors.
- Let the viewer show a terminal infrastructure failure when a run ends before its first weekly record. This requires a host-side status signal because a failed feed writer cannot report its own error through that file.

Reference: [PR 37](https://github.com/vaartnou/crucible/pull/37).
