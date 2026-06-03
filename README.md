# PaperFetch

> Fetch recent arXiv papers by category and keyword, then save or send a daily Markdown digest.

## Overview

PaperFetch is a lightweight Python utility for monitoring recent arXiv papers. It queries the arXiv API by category and keyword, filters papers within a recent time window, formats the matched papers into CSV or Markdown, and can send the result by email.

This project is useful for researchers, students, and engineers who want a simple scheduled paper digest for topics such as computer vision, NLP, AI, or a custom research keyword.

## Features

* Query arXiv papers by category, keyword, and recent-day window.
* Generate local CSV or Markdown paper digests.
* Send daily Markdown digests by email through SMTP.
* Support multiple arXiv categories such as `cs.CV`, `cs.CL`, and `cs.AI`.
* Support scheduled server execution through `run.sh`.

## Project Structure

```bash
.
|-- PaperFetch.py                  # Basic arXiv fetch script; saves matched papers to CSV
|-- PaperFetch_daily.py            # Daily arXiv digest script; saves Markdown and can send email
|-- PaperFrech_daily_keyword.py    # Keyword-focused daily digest script; sends email directly
|-- run.sh                         # Server-side shell wrapper for scheduled execution
|-- conda.yaml                     # Conda environment definition
|-- requirements.txt               # Python package dependencies
|-- LICENSE                        # Project license
`-- README.md                      # Project documentation
```

Runtime directories are expected by the scripts but are not tracked by Git:

```bash
config/
savefile/
log/
cache/
```

## Requirements

* Python 3.12, as specified in `conda.yaml`
* Conda or another Python environment manager
* Network access to the arXiv API
* SMTP email account if using email delivery
* No CUDA or GPU is required

Main Python packages:

* `feedparser`
* `pandas`
* `yagmail`
* `omegaconf`
* `requests`

## Installation

```bash
git clone https://github.com/Ocean-kang/PaperFetch.git
cd PaperFetch
```

Create the Conda environment:

```bash
conda env create -f conda.yaml
conda activate paperfetch
```

Install Python packages:

```bash
pip install -r requirements.txt
```

Create runtime directories:

```bash
mkdir -p config savefile log cache
```

## Data Preparation

This project does not require a local dataset. It fetches metadata from the arXiv API at runtime.

Expected local runtime layout:

```bash
.
|-- config/
|   `-- MyEmail.yaml
|-- savefile/
`-- log/
```

## Quick Start

For a simple CSV export:

```bash
python PaperFetch.py
```

For the keyword-focused daily digest without sending email:

```bash
python PaperFrech_daily_keyword.py --dry-run --no-email --days 1 --max-results 20
```

For server deployment through the provided shell script:

```bash
bash run.sh
```

## Usage

### Step 1: Configure Email

Create `config/MyEmail.yaml`:

```yaml
sender_email: "your_email@example.com"
sender_pass: "your_smtp_authorization_code"
receiver_email: "receiver@example.com"
```

For QQ Mail, `sender_pass` is usually the SMTP authorization code, not the login password.

### Step 2: Edit Search Settings

Edit the constants at the top of `PaperFrech_daily_keyword.py`:

```python
CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]
KEYWORDS = ["open vocabulary semantic segmentation", "open-vocabulary semantic segmentation"]
DAYS = 1
MAX_RESULTS = 100
```

### Step 3: Run the Digest Script

```bash
python PaperFrech_daily_keyword.py
```

### Step 4: Schedule on a Server

The included `run.sh` assumes this deployment path:

```bash
/root/code/PaperFetch
```

It also assumes Conda is installed under:

```bash
/root/miniconda3
```

Run manually:

```bash
bash run.sh
```

Example cron entry:

```bash
0 9 * * * /bin/bash -lc '/root/code/PaperFetch/run.sh' >> /root/code/PaperFetch/cron.log 2>&1
```

Do not keep a test schedule such as `* * * * *` for PaperFetch in production. The `run.sh`
wrapper also uses `flock` so an accidental high-frequency cron entry will not run concurrent
jobs.

Useful deployment checks:

```bash
crontab -l
tail -100 /root/code/PaperFetch/cron.log
tail -200 /root/code/PaperFetch/log/run.log
```

## Entry Scripts

| Script | Purpose | Output |
| ------ | ------- | ------ |
| `PaperFetch.py` | Fetch recent arXiv papers for a single query/category setting | CSV file under `savefile/` |
| `PaperFetch_daily.py` | Fetch recent papers from multiple categories, generate Markdown, and optionally send email | Markdown file under `savefile/`; email if enabled |
| `PaperFrech_daily_keyword.py` | Fetch papers using one combined query, deduplicate, and send at most one Markdown digest email | Email digest |
| `run.sh` | Server wrapper for scheduled execution of `PaperFrech_daily_keyword.py` | Log file under `log/run.log` |

## Command Line Arguments

`PaperFrech_daily_keyword.py` supports command-line arguments for safe testing and production runs.

| Argument | Type | Default | Description |
| -------- | ---: | ------: | ----------- |
| `--days` | int | `1` | Recent UTC days to query through `submittedDate`. |
| `--max-results` | int | `100` | Maximum arXiv results to request. Values above 100 are capped at 100. |
| `--dry-run` | flag | off | Fetch, filter, and generate the report, but do not send email. |
| `--no-email` | flag | off | Disable email sending for this run. |
| `--no-cache` | flag | off | Skip reading and writing the daily arXiv cache. |
| `--log-level` | str | `INFO` | Python logging level, such as `INFO` or `DEBUG`. |

Safe manual test:

```bash
python PaperFrech_daily_keyword.py --dry-run --no-email --days 1 --max-results 20
```

## Configuration

### Email Configuration

Path:

```bash
config/MyEmail.yaml
```

Example:

```yaml
sender_email: "your_email@example.com"
sender_pass: "your_smtp_authorization_code"
receiver_email: "receiver@example.com"
```

### arXiv Search Configuration

In `PaperFrech_daily_keyword.py`:

```python
CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]
KEYWORDS = ["open vocabulary semantic segmentation", "open-vocabulary semantic segmentation"]
DAYS = 1
MAX_RESULTS = 100
REQUEST_TIMEOUT = 30
SMTP_HOST = "smtp.qq.com"
```

Meaning:

| Name | Type | Description |
| ---- | ---- | ----------- |
| `CATEGORIES` | list | arXiv categories to query |
| `KEYWORDS` | list | Keywords or phrases to match in title/abstract |
| `DAYS` | int | Number of recent days to keep |
| `MAX_RESULTS` | int | Maximum number of arXiv results requested for the combined query |
| `REQUEST_TIMEOUT` | int | HTTP request timeout in seconds |
| `SMTP_HOST` | str | SMTP server host |

## Outputs

Depending on the script, outputs may include:

```bash
savefile/
|-- arxiv_<category>_<YYYYMMDD>.csv
`-- arxiv_daily_<YYYY-MM-DD>.md

log/
`-- run.log

cache/
`-- arxiv_<YYYY-MM-DD>_<query-hash>.json
```

Output details:

* `PaperFetch.py` writes CSV files to `savefile/`.
* `PaperFetch_daily.py` writes Markdown reports to `savefile/`.
* `PaperFrech_daily_keyword.py` sends the Markdown digest by email unless `--dry-run` or `--no-email` is used.
* `run.sh` appends runtime logs to `log/run.log`.
* `cron.log` should only show whether cron invoked `run.sh`; the main execution detail is in `log/run.log`.

## Reproduce Results

This is not a machine learning experiment or paper reproduction repository. There are no training, inference, evaluation, dataset preparation, or benchmark reproduction steps in the current codebase.

To reproduce the current daily digest behavior:

```bash
conda env create -f conda.yaml
conda activate paperfetch
pip install -r requirements.txt
mkdir -p config savefile log
```

Create `config/MyEmail.yaml`, edit the search settings, then run:

```bash
python PaperFrech_daily_keyword.py
```

Or run the server wrapper:

```bash
bash run.sh
```

## Production Verification

Check cron is not running a per-minute PaperFetch test job:

```bash
crontab -l
```

Run a no-email test:

```bash
cd /root/code/PaperFetch
/root/miniconda3/envs/paperfetch/bin/python PaperFrech_daily_keyword.py --dry-run --no-email --days 1 --max-results 20
echo $?
```

Run the wrapper manually:

```bash
cd /root/code/PaperFetch
/bin/bash /root/code/PaperFetch/run.sh
echo $?
tail -200 /root/code/PaperFetch/log/run.log
```

Run two wrapper processes at the same time to verify locking. The second process should log:

```text
Another PaperFetch job is running, skip.
```

## Examples

Fetch and email papers related to open-vocabulary semantic segmentation:

```bash
python PaperFrech_daily_keyword.py
```

Fetch a basic CSV digest:

```bash
python PaperFetch.py
```

Run the scheduled server command manually:

```bash
bash run.sh
```

## Troubleshooting

### 1. Missing Email Configuration

Error example:

```bash
FileNotFoundError: [Errno 2] No such file or directory: './config/MyEmail.yaml'
```

Solution:

```bash
mkdir -p config
```

Then create:

```bash
config/MyEmail.yaml
```

with:

```yaml
sender_email: "your_email@example.com"
sender_pass: "your_smtp_authorization_code"
receiver_email: "receiver@example.com"
```

### 2. Missing Output Directories

Error example:

```bash
OSError: Cannot save file into a non-existent directory: 'savefile'
```

Solution:

```bash
mkdir -p savefile log
```

### 3. Email Login Fails

Possible reasons:

* SMTP service is not enabled for the sender email account.
* `sender_pass` is the account password instead of an SMTP authorization code.
* The SMTP host does not match the email provider.

For QQ Mail, check whether SMTP is enabled and use the generated authorization code.

### 4. Daily Digest Sometimes Looks Empty

Possible reasons:

* arXiv API request failed or returned an invalid feed.
* Network connection from the server to arXiv was unstable.
* Keywords are too strict and do not match title/abstract text.
* The selected categories do not contain matching papers in the configured time window.

Check the log:

```bash
tail -n 100 log/run.log
```

### 5. `run.sh` Path Does Not Exist

The script currently uses absolute server paths:

```bash
/root/code/PaperFetch
/root/miniconda3
```

If your server uses different paths, edit `run.sh` before scheduling it.

## FAQ

**Q1: Does this project download PDF files?**

A: No. The current scripts fetch arXiv metadata such as title, authors, abstract, date, category, and link.

**Q2: Does this project require a GPU?**

A: No. It only performs API requests, text filtering, report generation, and email sending.

**Q3: Where should I change the keywords?**

A: Edit `KEYWORDS` in `PaperFrech_daily_keyword.py`.

**Q4: Where should I change the arXiv categories?**

A: Edit `CATEGORIES` in `PaperFrech_daily_keyword.py`.

**Q5: Can I run it every day automatically?**

A: Yes. Use `run.sh` with cron after adjusting the absolute paths to match your server.

## Roadmap

* [ ] Add a command-line interface for categories, keywords, days, and output options.
* [ ] Add a checked-in `config/MyEmail.example.yaml`.
* [ ] Add unit tests for URL construction, keyword matching, deduplication, and Markdown generation.
* [ ] Refactor duplicate logic from the three Python scripts into reusable modules.
* [ ] Add optional local Markdown saving for `PaperFrech_daily_keyword.py`.

## Citation

TODO: No associated paper or citation information is provided in the current repository.

## License

This project is licensed under the MIT License. See `LICENSE` for details.

## Acknowledgements

This project uses the following open-source tools and services:

* arXiv API
* feedparser
* pandas
* yagmail
* OmegaConf
