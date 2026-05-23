# Resilient Sheet Collector

Runs the Missing Value sheet fill using one Python entrypoint:

```bash
python run_resilient_sheet.py
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
sudo apt install xvfb
```

Place Google credentials at:

```bash
/usr/local/share/keys.json
```

Add Groq keys and optional proxy settings to `.env`:

```bash
GROQ_KEY_1=your_first_key
GROQ_KEY_2=your_second_key
RESILIENT_FORWARD_PROXY=http://127.0.0.1:10809
RESILIENT_USE_XVFB=1
RESILIENT_HEADLESS=0
GROQ_LENIENT_FALLBACK=1
```

## Run

Dry run:

```bash
python run_resilient_sheet.py --dry-run --limit 3
```

Foreground run:

```bash
python run_resilient_sheet.py
```

Background run:

```bash
python run_resilient_sheet.py --background
```

The runner automatically uses `xvfb-run` when no display is available. Use `--no-xvfb` only if you intentionally want no virtual display.

Watch logs:

```bash
tail -f logs/resilient_sheet_run.out
```

Stop background run:

```bash
kill $(cat logs/resilient_sheet_run.pid)
```

## Sources

The collector processes missing cells for:

- Alpha Spread
- ValueInvesting
- GuruFocus
- Price Target Low
- Price Target Average
- Price Target High
