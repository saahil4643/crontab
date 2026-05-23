# Resilient Sheet Collector

Runs the Missing Value sheet fill using one Python entrypoint:

```bash
python run_resilient_sheet.py
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
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
