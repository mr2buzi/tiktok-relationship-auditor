"""Scrape TikTok relationships and optionally unfollow reviewed non-mutuals.

Examples:
    python tiktok_bot_v2.py --username your_username
    python tiktok_bot_v2.py --username your_username --unfollow --max-unfollows 10

The default mode is read-only. Supplying --unfollow authorizes the requested
batch without another action confirmation, including when scrape validation
reports a warning. If --message is supplied, messaging runs as a second pass
over accounts whose unfollow action was verified.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from playwright.sync_api import (
    BrowserContext,
    ElementHandle,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)


BASE_URL = "https://www.tiktok.com"
DEFAULT_BRAVE_PATH = Path(
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
)
DEFAULT_PROFILE_DIR = Path(__file__).with_name("tiktok_profile_v2")
OUTPUT_DIR = Path(__file__).with_name("output_v2")
STABLE_PASSES_REQUIRED = 5
LOGIN_WAIT_SECONDS = 60


@dataclass
class ScrapeResult:
    list_name: str
    usernames: list[str]
    expected_count: int | None
    reached_end: bool
    elapsed_seconds: float
    error: str | None = None

    @property
    def valid(self) -> bool:
        if self.error or not self.reached_end:
            return False
        if self.expected_count is None:
            return bool(self.usernames)
        # TikTok may hide suspended/deleted accounts, so allow a small gap.
        tolerance = max(3, int(self.expected_count * 0.02))
        return len(self.usernames) >= self.expected_count - tolerance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape followers/following and find accounts not following back."
    )
    parser.add_argument("--username", required=True, help="Your TikTok username (no @).")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help="Persistent browser profile directory.",
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "brave", "chromium"),
        default="auto",
        help="Browser executable to use (default: auto).",
    )
    parser.add_argument(
        "--scroll-timeout",
        type=int,
        default=600,
        help="Maximum seconds to scrape each list (default: 600).",
    )
    parser.add_argument(
        "--unfollow",
        action="store_true",
        help="Offer to unfollow the validated non-mutual list.",
    )
    parser.add_argument(
        "--max-unfollows",
        type=int,
        default=0,
        help="Optional batch limit; 0 processes every validated non-mutual (default: 0).",
    )
    parser.add_argument(
        "--message",
        help="Message to send to every account successfully unfollowed.",
    )
    args = parser.parse_args()
    args.username = args.username.strip().lstrip("@").lower()
    if not args.username or "/" in args.username:
        parser.error("--username must be a TikTok username, without @ or a URL")
    if args.scroll_timeout < 30:
        parser.error("--scroll-timeout must be at least 30 seconds")
    if args.max_unfollows < 0:
        parser.error("--max-unfollows cannot be negative")
    if args.message is not None and not args.unfollow:
        parser.error("--message requires --unfollow")
    if args.message is not None and not args.message.strip():
        parser.error("--message cannot be empty")
    return args


def launch_context(playwright, args: argparse.Namespace) -> BrowserContext:
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    common = {
        "user_data_dir": str(args.profile_dir.resolve()),
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
    }

    if args.browser in ("auto", "brave") and DEFAULT_BRAVE_PATH.is_file():
        try:
            print(f"Opening Brave with profile: {args.profile_dir}")
            return playwright.chromium.launch_persistent_context(
                executable_path=str(DEFAULT_BRAVE_PATH), **common
            )
        except Exception as exc:
            if args.browser == "brave":
                raise RuntimeError(f"Could not launch Brave: {exc}") from exc
            print(f"Brave launch failed ({exc}); trying Playwright Chromium.")
    elif args.browser == "brave":
        raise FileNotFoundError(f"Brave was not found at {DEFAULT_BRAVE_PATH}")

    print(f"Opening Playwright Chromium with profile: {args.profile_dir}")
    return playwright.chromium.launch_persistent_context(**common)


def profile_url(username: str) -> str:
    return f"{BASE_URL}/@{username}"


def ensure_logged_in(page: Page, username: str) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    print(
        f"Log in and complete any TikTok verification in the browser. "
        f"Continuing in {LOGIN_WAIT_SECONDS} seconds."
    )
    for remaining in range(LOGIN_WAIT_SECONDS, 0, -1):
        print(f"\rLogin time remaining: {remaining:2d} seconds", end="", flush=True)
        time.sleep(1)
    print("\rLogin time complete. Continuing...       ")
    page.goto(profile_url(username), wait_until="domcontentloaded", timeout=60_000)
    login_controls = page.get_by_role("button", name=re.compile(r"^log in$", re.I))
    login_dialog = page.get_by_text("Log in to TikTok", exact=True)
    if (
        (login_controls.count() and login_controls.first.is_visible())
        or (login_dialog.count() and login_dialog.first.is_visible())
    ):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUTPUT_DIR / "login_required.png"), full_page=True)
        raise RuntimeError(
            "TikTok is still logged out after the 60-second login window. Log in "
            "in the opened browser during that window; the persistent profile will "
            "save the authenticated session for later runs."
        )
    try:
        page.locator("span[data-e2e='following'], span[data-e2e='followers']").first.wait_for(
            state="visible", timeout=20_000
        )
    except PlaywrightTimeout as exc:
        raise RuntimeError(
            "The profile relationship controls were not found. Check the username, "
            "login state, CAPTCHA, and whether TikTok changed its page markup."
        ) from exc


def parse_compact_count(text: str) -> int | None:
    match = re.search(r"([\d,.]+)\s*([KMB]?)", text.strip(), re.IGNORECASE)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(value * multiplier[match.group(2).upper()])


def displayed_count(page: Page, list_name: str) -> int | None:
    locator = page.locator(f"span[data-e2e='{list_name}']").first
    for candidate in (locator, locator.locator("xpath=..")):
        try:
            count = parse_compact_count(candidate.inner_text(timeout=2_000))
            if count is not None:
                return count
        except (PlaywrightTimeout, ValueError):
            pass
    return None


def extract_usernames(container: ElementHandle, own_username: str) -> set[str]:
    hrefs = container.evaluate(
        "el => [...el.querySelectorAll(\"a[href*='/@']\")].map(a => a.href)"
    )
    found: set[str] = set()
    for href in hrefs:
        match = re.search(r"/@([^/?#]+)", href)
        if match:
            username = match.group(1).strip().lower()
            if username and username != own_username.lower():
                found.add(username)
    return found


def find_scroll_container(page: Page) -> ElementHandle:
    page.wait_for_function(
        """() => {
            const links = [...document.querySelectorAll("a[href*='/@']")]
                .filter(a => {
                    const r = a.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            return links.some(link => {
                let node = link.parentElement;
                while (node && node !== document.body) {
                    const style = getComputedStyle(node);
                    if (/(auto|scroll)/.test(style.overflowY) &&
                        node.scrollHeight > node.clientHeight + 5) return true;
                    node = node.parentElement;
                }
                return false;
            });
        }""",
        timeout=20_000,
    )
    handle = page.evaluate_handle(
        """() => {
            const candidates = new Map();
            const links = [...document.querySelectorAll("a[href*='/@']")]
                .filter(a => {
                    const r = a.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
            for (const link of links) {
                let node = link.parentElement;
                while (node && node !== document.body) {
                    const style = getComputedStyle(node);
                    if (/(auto|scroll)/.test(style.overflowY) &&
                        node.scrollHeight > node.clientHeight + 5) {
                        candidates.set(node, (candidates.get(node) || 0) + 1);
                        break;
                    }
                    node = node.parentElement;
                }
            }
            return [...candidates.entries()]
                .sort((a, b) => b[1] - a[1])[0]?.[0] || null;
        }"""
    ).as_element()
    if handle is None:
        raise RuntimeError("Could not find a scrollable ancestor for the user list.")
    return handle


def close_dialog(page: Page) -> None:
    if page.locator("div[role='dialog']").count():
        page.keyboard.press("Escape")
        try:
            page.locator("div[role='dialog']").last.wait_for(state="hidden", timeout=5_000)
        except PlaywrightTimeout:
            # A profile navigation guarantees a clean state if Escape is ignored.
            pass


def scrape_list(
    page: Page,
    own_username: str,
    list_name: str,
    timeout_seconds: int,
) -> ScrapeResult:
    started = time.monotonic()
    expected = displayed_count(page, list_name)
    names: set[str] = set()
    reached_end = False

    try:
        close_dialog(page)
        control = page.locator(f"span[data-e2e='{list_name}']").first
        control.click(timeout=15_000)
        # Zero-count lists may open an empty dialog without profile links.
        if expected == 0:
            page.wait_for_timeout(1_000)
            reached_end = True
        else:
            scroll_container = find_scroll_container(page)
            stable_passes = 0
            previous_count = -1

            while time.monotonic() - started < timeout_seconds:
                names.update(extract_usernames(scroll_container, own_username))
                state = scroll_container.evaluate(
                    """el => ({
                        top: el.scrollTop,
                        height: el.scrollHeight,
                        client: el.clientHeight
                    })"""
                )
                at_bottom = state["top"] + state["client"] >= state["height"] - 5

                if len(names) == previous_count and at_bottom:
                    stable_passes += 1
                else:
                    stable_passes = 0
                previous_count = len(names)

                print(
                    f"\r{list_name.title()}: {len(names)}"
                    + (f" / about {expected}" if expected is not None else ""),
                    end="",
                    flush=True,
                )

                if stable_passes >= STABLE_PASSES_REQUIRED:
                    reached_end = True
                    break

                scroll_container.evaluate(
                    "el => el.scrollTo({top: el.scrollHeight, behavior: 'instant'})"
                )
                page.wait_for_timeout(1_500)

            names.update(extract_usernames(scroll_container, own_username))
            print()

        return ScrapeResult(
            list_name=list_name,
            usernames=sorted(names),
            expected_count=expected,
            reached_end=reached_end,
            elapsed_seconds=round(time.monotonic() - started, 1),
        )
    except Exception as exc:
        print()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(
                path=str(OUTPUT_DIR / f"{list_name}_failure.png"), full_page=True
            )
        except Exception:
            pass
        return ScrapeResult(
            list_name=list_name,
            usernames=sorted(names),
            expected_count=expected,
            reached_end=False,
            elapsed_seconds=round(time.monotonic() - started, 1),
            error=str(exc),
        )
    finally:
        close_dialog(page)


def write_lines(path: Path, values: Iterable[str]) -> None:
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def save_results(following: ScrapeResult, followers: ScrapeResult) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_lines(OUTPUT_DIR / "following.txt", following.usernames)
    write_lines(OUTPUT_DIR / "followers.txt", followers.usernames)

    non_mutuals = sorted(set(following.usernames) - set(followers.usernames))
    report_path = OUTPUT_DIR / "non_mutuals.txt"
    write_lines(report_path, non_mutuals)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "following": asdict(following),
        "followers": asdict(followers),
        "non_mutual_count": len(non_mutuals),
        "fully_validated": following.valid and followers.valid,
    }
    (OUTPUT_DIR / "scrape_report.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    return report_path


def currently_following(page: Page) -> Locator | None:
    candidates = page.locator("button")
    for index in range(candidates.count()):
        button = candidates.nth(index)
        try:
            if button.inner_text(timeout=1_000).strip().casefold() == "following":
                return button
        except (PlaywrightTimeout, ValueError):
            continue
    return None


def send_profile_message(page: Page, message: str) -> tuple[bool, str]:
    message_button = page.get_by_role("button", name=re.compile(r"^message$", re.I))
    if message_button.count() == 0:
        message_button = page.locator("button[data-e2e='message-button']")
    if message_button.count() == 0 or not message_button.first.is_visible():
        return False, "Message button is not available after unfollowing."

    message_button.first.click(timeout=10_000)
    page.wait_for_timeout(1_500)
    editors = page.locator(
        "[contenteditable='true'][role='textbox'], "
        "div[contenteditable='true'], textarea, input[type='text']"
    )
    editor = None
    for index in range(editors.count()):
        candidate = editors.nth(index)
        try:
            if candidate.is_visible() and candidate.is_editable():
                editor = candidate
                break
        except Exception:
            continue
    if editor is None:
        return False, "No visible editable message box was found."

    editor.click()
    editor.fill(message)
    editor.press("Enter")
    page.wait_for_timeout(1_000)
    # A cleared composer is the strongest generic confirmation available in
    # TikTok's changing UI. If it retains the text, do not claim success.
    try:
        remaining = editor.input_value(timeout=1_000)
    except Exception:
        remaining = editor.inner_text(timeout=1_000)
    if remaining.strip() == message.strip():
        return False, "The composer still contains the text after Enter."
    return True, "sent"


def unfollow_users(page: Page, usernames: list[str], limit: int) -> list[str]:
    targets = usernames if limit == 0 else usernames[:limit]
    print(f"\nUnfollow pass: processing {len(targets)} accounts.")

    completed: list[str] = []
    failed: dict[str, str] = {}
    for position, username in enumerate(targets, start=1):
        try:
            print(f"[{position}/{len(targets)}] @{username}")
            page.goto(profile_url(username), wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
            button = currently_following(page)
            if button is None:
                failed[username] = "A button labelled exactly 'Following' was not found."
                continue
            button.click(timeout=10_000)
            page.wait_for_timeout(1_500)

            # Some TikTok versions show a confirmation dialog.
            confirm = page.get_by_role("button", name=re.compile(r"^unfollow$", re.I))
            if confirm.count() and confirm.last.is_visible():
                confirm.last.click(timeout=5_000)
                page.wait_for_timeout(1_500)

            if currently_following(page) is not None:
                failed[username] = "The page still shows Following after the click."
            else:
                completed.append(username)
            time.sleep(2.5)
        except Exception as exc:
            failed[username] = str(exc)

    write_lines(OUTPUT_DIR / "unfollowed.txt", completed)
    (OUTPUT_DIR / "unfollow_failures.json").write_text(
        json.dumps(failed, indent=2), encoding="utf-8"
    )
    print(f"Unfollowed {len(completed)}; failed or skipped {len(failed)}.")
    return completed


def message_users(page: Page, usernames: list[str], message: str) -> None:
    print(f"\nMessage pass: revisiting {len(usernames)} successfully unfollowed accounts.")
    messaged: list[str] = []
    message_failures: dict[str, str] = {}
    for position, username in enumerate(usernames, start=1):
        try:
            print(f"[{position}/{len(usernames)}] Messaging @{username}")
            page.goto(profile_url(username), wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_500)
            sent, detail = send_profile_message(page, message)
            if sent:
                messaged.append(username)
            else:
                message_failures[username] = detail
            time.sleep(2.5)
        except Exception as exc:
            message_failures[username] = str(exc)

    write_lines(OUTPUT_DIR / "messaged.txt", messaged)
    (OUTPUT_DIR / "message_failures.json").write_text(
        json.dumps(message_failures, indent=2), encoding="utf-8"
    )
    print(f"Messaged {len(messaged)}; message failures {len(message_failures)}.")


def main() -> int:
    args = parse_args()
    with sync_playwright() as playwright:
        context = launch_context(playwright, args)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_logged_in(page, args.username)

            following = scrape_list(
                page, args.username, "following", args.scroll_timeout
            )
            page.goto(profile_url(args.username), wait_until="domcontentloaded")
            followers = scrape_list(
                page, args.username, "followers", args.scroll_timeout
            )
            report_path = save_results(following, followers)
            non_mutuals = sorted(
                set(following.usernames) - set(followers.usernames)
            )

            print("\nResults")
            print(f"  Following collected: {len(following.usernames)}")
            print(f"  Followers collected: {len(followers.usernames)}")
            print(f"  Not following back: {len(non_mutuals)}")
            print(f"  Following validation: {'PASS' if following.valid else 'FAIL'}")
            print(f"  Followers validation: {'PASS' if followers.valid else 'FAIL'}")
            print(f"  Output: {report_path}")

            if not (following.valid and followers.valid):
                print(
                    "Warning: scrape validation failed. Partial data was saved and may "
                    "produce an inaccurate non-mutual list."
                )
                if following.error:
                    print(f"  Following error: {following.error}")
                if followers.error:
                    print(f"  Followers error: {followers.error}")
                if not args.unfollow:
                    return 2
                print("--unfollow was supplied, so the requested batch will run anyway.")

            if args.unfollow and non_mutuals:
                unfollowed = unfollow_users(page, non_mutuals, args.max_unfollows)
                if args.message and unfollowed:
                    message_users(page, unfollowed, args.message)
            elif args.unfollow:
                print("There are no collected non-mutual accounts to unfollow.")
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
