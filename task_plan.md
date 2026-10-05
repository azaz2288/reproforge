# ReproForge working plan

## Objective
Implement the project plan in `PROJECT_PLAN.md`, starting with a reliable M1 execution kernel. Do not claim later milestones complete or fabricate effort.

## Current phase
M2 verified-prefix resume implementation complete locally; release verification in progress. Remaining plan scope stays explicit.

2026-10-06 maintenance: v0.4.1 immutable-object publication hardening implemented after two failing regressions. Verify temporary bytes before publication, exclusive hard link, validate concurrent duplicates without overwriting/removing other objects.31 local tests passed; independent package and same-SHA CI verification in progress. Timeout descendant handling remains open; no change to that guarantee.

## Phases
- Project discovery and scope: complete.
- Architecture and acceptance criteria: complete in `PROJECT_PLAN.md`.
- M1 implementation: complete for initial vertical slice; hardening and test expansion remain.
- M1 verification: complete for the initial vertical slice; 9 local tests, example run/verify, isolated wheel installation and Windows/Ubuntu CI passed. Further hardening remains in the roadmap.
- Public release: complete as an explicit M1 work-in-progress at https://github.com/azaz2288/reproforge. The five-milestone plan is not claimed complete.
- M2 safe reuse: experimental slice implemented; cache defaults off, requires task `cache=true` and `run --reuse`, derives key from plan/command/input hashes/runtime metadata, rejects corrupt entries or objects. Locks, crash recovery, broader environment capture and full M2 acceptance remain pending.
- M2 concurrency/recovery slice: implemented OS-backed per-project writer lock and explicit interruption marking for abandoned run ledgers. Local tests pass; no partial-task resume yet.
- M3 quality gates: built-in CSV quality and temporal split checks emit machine-readable reports, fail closed and block downstream tasks. Independent `verify` recomputes gate evidence from stored input. Plugin API is not implemented.
- M4 visualization slice: loopback-only read-only dashboard lists runs, DAG dependencies, artifact hashes, failures and run differences. Multi-user access control is not implemented.
- M5 scale/handoff: repeatable synthetic CSV benchmark, recovery exercise and pinned UCI Iris public-data case implemented; multi-gigabyte scale validation remains pending.
- Current release check: 23 local tests pass; final published commit `637c005` passed Windows/Ubuntu CI run 36606745888. Working tree clean before this planning update.
- M2 resume acceptance: `run --resume RUN_ID` accepts only a verified failed/interrupted source ledger with matching plan/environment; reuses the longest successful prefix only after live declared inputs still match; executes remaining tasks in a new ledger. A changed input or corrupt source must fail closed. Ordinary `--reuse` cache remains independent.

## Decision log
| Decision | Rationale |
|---|---|
| Local-first Python 3.12+ | Offline usability and rapid cross-platform validation. |
| JSON DAG with explicit file contracts | Reviewable, portable and testable. |
| Content-addressed storage and atomic run ledger | Detect tampering and preserve provenance. |
| No cache in M1 | Incorrect reuse is more dangerous than slow execution. |
| No security-sandbox claim | Subprocesses retain user privileges despite working-directory isolation. |

## Errors
| Error | Attempt | Resolution |
|---|---:|---|
| `git status` reported “not a git repository” after the first local wheel build | 1 | Expected before repository initialization; initialize only after code/tests are reviewed. |
| M2 test for an incomplete ledger raised `Malformed recorded environment` | 1 | The hand-written fixture omitted the new environment field; add it, then rerun the suite. |
| README update patch missed an exact sentence | 1 | Re-read the current README and apply smaller, exact-context edits. |
| UCI download preview used `Substring` on a byte array | 1 | Treat response as bytes; decode explicitly before inspection. |
