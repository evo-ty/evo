# EVO HUB — Thread startup error fix

## Root cause

The uploaded Telebot application was failing while creating its first worker thread. The runner applied `RLIMIT_NPROC=128` to each child process. On a shared Unix user, `RLIMIT_NPROC` is counted across the user’s existing processes and threads, not only the uploaded server. Because the host already had more than that number of tasks, even `num_threads=1` could fail with `RuntimeError: can't start new thread`.

## Changes

The runner no longer applies `RLIMIT_NPROC` to shared host processes. It still keeps the CPU and open-file best-effort limits, while process-count isolation is reserved for Docker/cgroups where it is scoped per server. This prevents the host-level user process count from blocking an uploaded application’s first thread.

The runner also clears stale process tracking before a start or restart, checks whether the child exits immediately, resets the database PID on failure, and returns a clear Console error instead of claiming that a dead process started successfully.

The uploaded Telebot application uses `num_threads=1` to reduce its own worker footprint in the constrained hosting environment.

## Verification

Python compilation succeeded for the runner and uploaded application. A smoke test using the same child-process setup and the remaining CPU/open-file limits showed `poll=None` after startup and printed the bot startup message without `RuntimeError: can't start new thread`. The test process was stopped intentionally after verification.

## Credential safety

The uploaded source contains a Telegram bot token. It should be revoked and regenerated in BotFather, then supplied through a per-server environment variable rather than committed in `main.py`. The token is intentionally not repeated in this report.
