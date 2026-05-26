# Resilient Collector Structure

## Runtime Flow

1. Process sheet jobs sequentially.
2. Auto-wrap the run in `xvfb-run` when no display is available.
3. Launch a fresh headed Chromium browser and isolated context per job by default.
4. Use `RESILIENT_FORWARD_PROXY` or `PLAYWRIGHT_PROXY` when configured; otherwise run direct.
5. Apply Playwright stealth to the page when available.
6. Try the primary source URL with a 10 second timeout.
7. If the primary source fails, navigate directly to Google SERP with `hl=en`.
8. Extract search snippets only and do not click result links.
9. Pass snippets to Groq for numeric parsing or lenient estimation.
10. Try Groq keys sequentially as failover.
11. Write successful values back with retry/backoff.
12. Close page, browser context, and browser for every job.
13. Sleep for 6 seconds after each job by default.

## Modules

`resilient_collector/proxy_pool.py`

Chooses direct mode or a configured forward proxy route.

`resilient_collector/extractors.py`

Handles GuruFocus primary extraction and Google snippet extraction.

`resilient_collector/groq_fallback.py`

Handles the final Groq API fallback through the OpenAI-compatible chat completions endpoint.

`resilient_collector/orchestrator.py`

Coordinates ticker processing, isolation, fallback routing, teardown, and compliance scheduling.

`run_resilient_sheet.py`

Runs the full sheet-backed collector in foreground or detached background mode.

## Fallback Order

Primary source: source-specific URL for AlphaSpread, ValueInvesting, GuruFocus, or Yahoo price targets.

First fallback: Google SERP snippets from `https://www.google.com/search?...&hl=en`.

Final fallback: Groq parses the provided snippets and returns only a numeric value.

## Environment Variables

`RESILIENT_TICKERS`

Comma-separated ticker list used when CLI tickers are not passed.

`RESILIENT_PROXY_MODE`

Set to `forward` by default. Use `direct` to disable browser proxy routing.

`RESILIENT_FORWARD_PROXY`

Forward proxy URL used by browser contexts. Falls back to `PLAYWRIGHT_PROXY`.

`RESILIENT_USE_XVFB`

Auto-wrap with `xvfb-run` when no display is available. Default: `1`.

`RESILIENT_HEADLESS`

Launch Chromium headless when `1`; headed when `0`. Default: `0`.

`DIRECT_SCRAPE_TIMEOUT_MS`

Primary source navigation timeout. Default: `10000`.

`SERP_TIMEOUT_MS`

Google SERP navigation timeout. Default: `10000`.

`RESILIENT_SLEEP_BASELINE_SECONDS`

Delay after each job. Default: `6`.

`RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY`

Number of ValueInvesting ticker pages per fresh browser identity. Default: `5`.

`RESILIENT_VALUEINVESTING_SLEEP_SECONDS`

Delay after each ValueInvesting ticker in the batched pass (reduces rate limits when reusing one browser). Default: `15`.

`RESILIENT_VALUEINVESTING_SLEEP_JITTER_SECONDS`

Random +/- jitter added to the per-ticker delay. Default: `5` (so sleeps are roughly 10–20s).

`RESILIENT_VALUEINVESTING_POST_ROTATION_SLEEP_SECONDS`

Extra pause after closing a browser identity and before opening the next (every 5 tickers). Default: `12`.

`RESILIENT_VALUEINVESTING_MULTI_PATH`

When `1`, try all three valuation URLs per ticker (uses more free views). Default: `0` (only `dcf-growth-exit-5y`).

`VALUEINVESTING_TABLE_WAIT_MS`

Wait for `table.each_summary` before reading Fair Price. Default: `25000`.

`VALUEINVESTING_CARD_WAIT_MS`

Wait for legacy `.price_square` cards if the summary table is missing. Default: `8000`.

`SHEET_RETRY_ATTEMPTS`

Retry count for sheet reads/writes. Default: `6`.

`GROQ_API_KEY`

Single Groq key used by the final fallback.

`GROQ_KEY_1` and `GROQ_KEY_2`

Preferred failover key names. The collector tries keys in order and never logs the values.

`GROQ_API_KEYS`

Comma-separated, semicolon-separated, or whitespace-separated Groq keys. These are tried after `GROQ_KEY_1` and `GROQ_KEY_2`.

`GROQ_MODEL`

Groq model name. Default: `openai/gpt-oss-120b`.

`GROQ_ENDPOINT`

Groq OpenAI-compatible chat completions endpoint. Default: `https://api.groq.com/openai/v1/chat/completions`.

`GROQ_TIMEOUT_SECONDS`

Groq request timeout. Default: `25`.

Groq fallback order after primary and Google:

1. Parse Google snippets only (no model memory).
2. If still no value, optional direct Groq question (`RESILIENT_GROQ_DIRECT_FALLBACK=1`, default on). This may use model training data and can be stale; logs as `groq_direct_fallback`.

## Run

Dry run missing jobs from the Missing Value sheet:

```bash
python run_resilient_sheet.py --dry-run
```

Process all missing sheet jobs and write successful values back:

```bash
python run_resilient_sheet.py
```

Process a small test batch:

```bash
python run_resilient_sheet.py --limit 3
```

Start as a backend process that survives terminal close:

```bash
python run_resilient_sheet.py --background
```

Start a limited backend test:

```bash
python run_resilient_sheet.py --background --limit 3
```

Watch output:

```bash
tail -f logs/resilient_sheet_run.out
```

Stop the backend process:

```bash
kill $(cat logs/resilient_sheet_run.pid)
```

## Production Notes

Groq is used after the primary path and Google snippet collection. Snippet parsing is evidence-only. If that still fails, direct Groq is a last-resort backup (may be stale). If all fallbacks fail, the value is left unchanged.

Sheet mode processes `alphaspread`, `valueinvesting`, `gurufocus`, `price_target_low`, `price_target_avg`, and `price_target_high`. Failed queued jobs are left unchanged so unavailable values do not overwrite existing sheet data.

### ValueInvesting batching

ValueInvesting jobs run first in one browser-batched pass (shared session, not deferred sheet writes):

1. Reuse a single browser for up to `RESILIENT_VALUEINVESTING_VIEWS_PER_IDENTITY` tickers (default 5).
2. Rotate to a new browser identity (new context, rotated user-agent/viewport) when the budget is used.
3. Read Fair Price from `table.each_summary` (Selected column) before legacy card selectors.
4. On free-view limit / signup wall, rotate identity and retry the same ticker once.
5. Write each successful result to column I immediately after scrape (same as other sources).
