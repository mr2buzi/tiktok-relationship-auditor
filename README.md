# TikTok Relationship Auditor

A Python and Playwright command-line tool for auditing the relationship between
a TikTok account's Following and Followers lists. It creates reproducible
reports and can optionally process a reviewed batch of non-mutual accounts.

## Features

- Reusable browser session stored in a local persistent profile
- Dynamic discovery of TikTok's scrollable relationship-list container
- Deduplicated and sorted username exports
- Comparison against the relationship counts displayed by TikTok
- JSON diagnostics and screenshots when scraping fails
- Read-only behavior by default
- Optional bounded unfollow and follow-up message passes
- Per-account action success and failure logs

## Workflow

```text
Following list ----+
                   +--> Following - Followers --> non_mutuals.txt
Followers list ----+                              scrape_report.json
                                                        |
                                 optional unfollow pass -+
                                                        |
                                  optional message pass -+
```

The program launches a visible Brave or Chromium window with a dedicated local
profile. After a manual login window, it opens both relationship lists and
collects profile links until the scrollable list remains stable at its bottom.

The default command only creates reports. Account changes occur only when
`--unfollow` is explicitly supplied. When `--message` is also provided,
messaging runs as a second pass over accounts whose unfollow was verified.

## Requirements

- Python 3.9+
- Playwright for Python
- Brave, or Playwright Chromium
- A TikTok account you are authorized to manage

Install the dependency and browser:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

Read-only audit:

```powershell
python .\tiktok_bot_v2.py --username your_username
```

Process all collected non-mutual accounts:

```powershell
python .\tiktok_bot_v2.py --username your_username --unfollow
```

Limit the batch:

```powershell
python .\tiktok_bot_v2.py --username your_username --unfollow --max-unfollows 10
```

Run a message pass after verified unfollows:

```powershell
python .\tiktok_bot_v2.py --username your_username --unfollow --max-unfollows 10 --message "Your message"
```

Run `python .\tiktok_bot_v2.py --help` to see every option.

## Authentication

The script provides a 60-second manual authentication window. TikTok QR or
direct TikTok login is recommended because third-party OAuth providers may
reject sign-in from a browser controlled by testing software.

Session cookies and site state are stored in the ignored
`tiktok_profile_v2/` directory. Credentials are never requested by the Python
program or written to the repository. Treat the profile directory as sensitive
and never commit or share it.

## Generated output

Files are written to the ignored `output_v2/` directory:

| File | Description |
| --- | --- |
| `following.txt` | Collected Following usernames |
| `followers.txt` | Collected Followers usernames |
| `non_mutuals.txt` | Accounts present only in Following |
| `scrape_report.json` | Counts, timing, validation state, and errors |
| `unfollowed.txt` | Verified unfollow successes |
| `unfollow_failures.json` | Unfollow failures and reasons |
| `messaged.txt` | Verified message submissions |
| `message_failures.json` | Message failures and reasons |
| `*_failure.png` | Screenshots captured when list scraping fails |

## Accuracy and safeguards

TikTok is a frequently changing, virtualized web application. This project
avoids fixed child-index selectors, retains usernames across virtualized
scrolls, and requires multiple stable passes at the bottom before considering a
list complete. It records displayed counts for comparison.

Validation is diagnostic rather than a guarantee. TikTok can hide restricted
accounts, rate-limit requests, change its markup, or return partial lists.
Review `scrape_report.json` and spot-check results before using an action mode.

## Responsible use

Use this project only with accounts you own or are explicitly authorized to
manage. Automated actions may be limited by TikTok's terms and anti-abuse
systems. Prefer small batches, inspect every report, avoid unsolicited or
repetitive messages, and stop when TikTok presents a challenge or rate limit.

## Development

Run a syntax check:

```powershell
python -m py_compile .\tiktok_bot_v2.py
```

No credentials, account usernames, generated reports, or browser-session data
belong in source control.

## License

Released under the [MIT License](LICENSE).
