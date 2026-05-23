# Resilient Collector Structure

## Runtime Flow

1. Process sheet jobs sequentially.
2. Launch a fresh Chromium browser and isolated context per job.
3. Use `RESILIENT_FORWARD_PROXY` or `PLAYWRIGHT_PROXY` when configured; otherwise run direct.
4. Apply Playwright stealth to the page when available.
5. Try the primary source URL with a 10 second timeout.
6. If the primary source fails, navigate directly to Google SERP with `hl=en`.
7. Extract search snippets only and do not click result links.
8. Pass snippets to Groq for strict numeric parsing.
9. Try Groq keys sequentially as failover.
10. Write successful values back with retry/backoff.
11. Close page, browser context, and browser for every job.
12. Sleep for 6 seconds after each job by default.

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

`DIRECT_SCRAPE_TIMEOUT_MS`

Primary source navigation timeout. Default: `10000`.

`SERP_TIMEOUT_MS`

Google SERP navigation timeout. Default: `10000`.

`RESILIENT_SLEEP_BASELINE_SECONDS`

Delay after each job. Default: `6`.

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

Groq is used only after the primary path and Google snippet collection. It is instructed to use only snippets and return an empty response if the metric is not explicitly present.

Sheet mode processes `alphaspread`, `valueinvesting`, `gurufocus`, `price_target_low`, `price_target_avg`, and `price_target_high`. Failed jobs are left unchanged unless `--write-failures` is passed.
