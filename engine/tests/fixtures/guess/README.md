# guess@1: guess the number

A throwaway task that exercises the core loop (registry, episode runner, transcripts, executor,
server) with almost no task logic.

| | |
|---|---|
| Ref | `guess@1` |
| Entry point | `crucible.envs` → `guess = "crucible.envs.guess"` |
| Step budget | 10 guesses |
| Seeds | any integer; the seed fixes the hidden number in 1..100 |

## Tools

| Tool | Kind | Effect |
|---|---|---|
| `guess(n)` | action | `higher`, `lower` or `correct`; `n` must be 1..100 |
| `give_up` | end | ends the episode unsolved |

## Scoring

Solved: `1 - (guesses - 1) / 10`. Not solved: 0.

## Policies

| Policy | What it does | Golden mean, seeds 0–9 |
|---|---|---|
| `reference` | binary search; at most 7 guesses | 0.530 |
| `do_nothing` | generic; stops at once | 0.000 |

## Layout

- `task.py`: the task. `policies/reference.py`: binary search.
- `tests/`: reference solves seeds 0–49, invalid calls, determinism (hypothesis).
