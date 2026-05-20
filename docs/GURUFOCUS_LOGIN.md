# GuruFocus Stock DCF Page — login (optional)

Fair Value on https://www.gurufocus.com/stock/AAPL/dcf is filled by a **client-side DCF model** and may take **30–90 seconds** to appear even **without login**. The scraper waits up to ~2 minutes (poll + `wait_for_function`).

If Fair Value stays empty after that, GuruFocus may require a **subscription / login** (save session below).

## Save a logged-in browser session (one time)

```bash
playwright codegen https://www.gurufocus.com/stock/AAPL/dcf \
  --save-storage=gurufocus_auth.json
```

Log in in the browser window, then close codegen. Place `gurufocus_auth.json` in the project root (gitignored).

## Test with session

```bash
python scripts/capture_json.py --source gurufocus --ticker AAPL
```

Look for **Fair Value (live DOM)** in the output.

## Production

`run.py` loads the same `gurufocus_auth.json` automatically when the file exists.
