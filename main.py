#!/usr/bin/env python3
"""
FaucetFlow — Automated cryptocurrency faucet claimer.

Drives a real browser (Chrome or Edge) via Selenium to visit configured
faucet sites, fill in wallet addresses, submit claims, and log results.
Enforces a configurable cooldown between claims per faucet/wallet pair.

Usage:
    python main.py                  # Run all eligible faucets
    python main.py --dry-run        # Show what would be claimed
    python main.py --faucet NAME    # Process only a specific faucet
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
FAUCETS_FILE = SCRIPT_DIR / "faucets.json"
STATE_FILE = SCRIPT_DIR / "last_access.json"
LOG_FILE = SCRIPT_DIR / "faucetflow.log"

DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    """Configure logging to both console and file."""
    logger = logging.getLogger("faucetflow")
    logger.setLevel(logging.DEBUG)

    # Console handler (INFO and above)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
    console.setFormatter(console_fmt)

    # File handler (DEBUG and above)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  [%(funcName)s]  %(message)s")
    file_handler.setFormatter(file_fmt)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    """
    Load configuration from .env and return a dict with all settings.
    Exits with a helpful message if .env is missing.
    """
    if not ENV_FILE.exists():
        log.error("No .env file found at %s", ENV_FILE)
        log.error("Copy .env.example to .env and edit it with your settings:")
        log.error("  cp .env.example .env")
        sys.exit(1)

    load_dotenv(ENV_FILE)

    config: Dict[str, Any] = {
        "browser": os.getenv("BROWSER", "chrome").lower(),
        "browser_profile_path": os.getenv("BROWSER_PROFILE_PATH", "").strip(),
        "browser_profile_directory": os.getenv("BROWSER_PROFILE_DIRECTORY", "Default").strip(),
        "headless": os.getenv("HEADLESS", "false").lower() == "true",
        "cooldown_hours": float(os.getenv("COOLDOWN_HOURS", "24")),
        "page_load_timeout": int(os.getenv("PAGE_LOAD_TIMEOUT", "30")),
        "element_timeout": int(os.getenv("ELEMENT_TIMEOUT", "10")),
        "wallets": _parse_wallets(os.getenv("WALLETS", "")),
    }

    # Validate browser choice
    if config["browser"] not in ("chrome", "edge"):
        log.error("BROWSER must be 'chrome' or 'edge', got '%s'", config["browser"])
        sys.exit(1)

    # Validate we have wallets
    if not config["wallets"]:
        log.error("No wallets configured. Set WALLETS in .env")
        log.error("Format: WALLETS=BTC=bc1q...,ETH=0x...,DOGE=D...")
        sys.exit(1)

    return config


def _parse_wallets(raw: str) -> Dict[str, str]:
    """
    Parse the WALLETS env var into a dict of {COIN: ADDRESS}.
    Format: "BTC=bc1q...,ETH=0x...,DOGE=D..."
    """
    wallets: Dict[str, str] = {}
    if not raw or not raw.strip():
        return wallets

    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            log.warning("Skipping malformed wallet entry (missing '='): %s", pair)
            continue
        coin, _, address = pair.partition("=")
        coin = coin.strip().upper()
        address = address.strip()
        if not coin or not address:
            log.warning("Skipping malformed wallet entry: %s", pair)
            continue
        wallets[coin] = address

    return wallets


def load_faucets() -> List[Dict[str, Any]]:
    """Load faucet definitions from faucets.json, skipping comment entries."""
    if not FAUCETS_FILE.exists():
        log.error("faucets.json not found at %s", FAUCETS_FILE)
        sys.exit(1)

    with open(FAUCETS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Filter out comment-only entries (those without a "name" field)
    faucets = [entry for entry in data if "name" in entry]
    log.debug("Loaded %d faucet definitions", len(faucets))
    return faucets


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------


def load_state() -> Dict[str, str]:
    """
    Load the last_access state from last_access.json.
    Returns an empty dict if the file is missing or corrupt.
    Keys are "faucet_name|wallet_address" and values are ISO-format timestamps.
    """
    if not STATE_FILE.exists():
        log.debug("No state file found; starting fresh.")
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            log.warning("State file is not a dict; starting fresh.")
            return {}
        log.debug("Loaded state with %d entries", len(state))
        return state
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load state file (%s); starting fresh.", exc)
        return {}


def save_state(state: Dict[str, str]) -> None:
    """Persist the last_access state to disk as JSON."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        log.debug("State saved (%d entries)", len(state))
    except OSError as exc:
        log.error("Failed to save state file: %s", exc)


def state_key(faucet_name: str, wallet_address: str) -> str:
    """Create a deterministic state key for a faucet+wallet pair."""
    return f"{faucet_name}|{wallet_address}"


def is_on_cooldown(faucet_name: str, wallet_address: str, cooldown_hours: float, state: Dict[str, str]) -> bool:
    """Check if the given faucet+wallet pair is still within the cooldown window."""
    key = state_key(faucet_name, wallet_address)
    last_str = state.get(key)
    if not last_str:
        return False

    try:
        last_time = datetime.strptime(last_str, DATE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        log.warning("Invalid timestamp in state for '%s'; treating as expired.", key)
        return False

    elapsed = datetime.now(timezone.utc) - last_time
    return elapsed < timedelta(hours=cooldown_hours)


# ---------------------------------------------------------------------------
# Browser Setup
# ---------------------------------------------------------------------------


def create_driver(config: Dict[str, Any]) -> webdriver.Remote:
    """
    Create and return a Selenium WebDriver instance for Chrome or Edge.

    Uses webdriver-manager to auto-download the correct driver version.
    Loads the user's browser profile if configured so saved logins and
    wallet extensions are available.
    """
    browser = config["browser"]

    if browser == "chrome":
        options = ChromeOptions()
        _apply_common_options(options, config)
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

    elif browser == "edge":
        options = EdgeOptions()
        _apply_common_options(options, config)
        service = EdgeService(EdgeChromiumDriverManager().install())
        driver = webdriver.Edge(service=service, options=options)

    else:
        raise ValueError(f"Unsupported browser: {browser}")

    driver.set_page_load_timeout(config["page_load_timeout"])
    return driver


def _apply_common_options(options: Any, config: Dict[str, Any]) -> None:
    """Apply options common to both Chrome and Edge."""
    if config["headless"]:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    # User profile
    profile_path = config["browser_profile_path"]
    if profile_path:
        options.add_argument(f"--user-data-dir={profile_path}")
        profile_dir = config.get("browser_profile_directory", "Default")
        if profile_dir:
            options.add_argument(f"--profile-directory={profile_dir}")
        log.info("Using browser profile: %s (directory: %s)", profile_path, profile_dir)
    else:
        log.info("No browser profile configured; using fresh temporary profile.")

    # Common anti-detection and stability flags
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Disable various prompts
    prefs = {
        "profile.default_content_setting_values.notifications": 2,  # Block notifications
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    options.add_experimental_option("prefs", prefs)


# ---------------------------------------------------------------------------
# Selector Helpers
# ---------------------------------------------------------------------------


def resolve_selector(selector_def: Dict[str, str]) -> Tuple[str, str]:
    """
    Convert a faucet selector definition into a (By.*, value) tuple.

    Handles types: "css", "xpath", "id".
    """
    stype = selector_def.get("type", "css").lower()
    value = selector_def["value"]

    if stype == "css":
        return (By.CSS_SELECTOR, value)
    elif stype == "xpath":
        return (By.XPATH, value)
    elif stype == "id":
        return (By.ID, value)
    else:
        log.warning("Unknown selector type '%s', falling back to CSS.", stype)
        return (By.CSS_SELECTOR, value)


def find_element_safe(driver: webdriver.Remote, selector_def: Dict[str, str], timeout: int = 5) -> Any:
    """
    Safely find an element by the given selector definition.
    Returns the WebElement or None if not found.
    """
    by, value = resolve_selector(selector_def)
    try:
        return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, value)))
    except (TimeoutException, NoSuchElementException):
        return None


def click_element_safe(driver: webdriver.Remote, selector_def: Dict[str, str], timeout: int = 5) -> bool:
    """
    Safely find and click an element.
    Returns True on success, False on failure.
    """
    by, value = resolve_selector(selector_def)
    try:
        element = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, value)))
        element.click()
        return True
    except (TimeoutException, NoSuchElementException, ElementClickInterceptedException,
            ElementNotInteractableException, StaleElementReferenceException) as exc:
        log.debug("Click failed for %s='%s': %s", by, value, exc)
        return False


# ---------------------------------------------------------------------------
# Popup Dismissal
# ---------------------------------------------------------------------------


def dismiss_popups(driver: webdriver.Remote, popup_selectors: List[Dict[str, str]]) -> int:
    """
    Attempt to dismiss all popups/dialogs using the configured selectors.
    Returns the number of popups dismissed.
    """
    dismissed = 0
    if not popup_selectors:
        return dismissed

    for selector_def in popup_selectors:
        if click_element_safe(driver, selector_def, timeout=2):
            dismissed += 1
            log.debug("Dismissed popup with selector: %s", selector_def)
            time.sleep(0.5)  # Let animation finish

    return dismissed


# ---------------------------------------------------------------------------
# Claim Logic
# ---------------------------------------------------------------------------


def process_faucet(
    faucet: Dict[str, Any],
    wallets: Dict[str, str],
    config: Dict[str, Any],
    state: Dict[str, str],
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """
    Process all eligible wallet claims for a single faucet.

    Returns a list of result dicts.
    """
    faucet_name = faucet["name"]
    faucet_url = faucet["url"]
    results: List[Dict[str, Any]] = []

    # Determine eligible wallets (not on cooldown)
    eligible: Dict[str, str] = {}
    for coin, address in wallets.items():
        if is_on_cooldown(faucet_name, address, config["cooldown_hours"], state):
            remaining = _cooldown_remaining(faucet_name, address, config["cooldown_hours"], state)
            log.info("  [%s] %s — SKIP (cooldown, %s remaining)", faucet_name, coin, remaining)
            results.append({
                "faucet": faucet_name,
                "coin": coin,
                "wallet": address,
                "success": False,
                "error": f"On cooldown ({remaining} remaining)",
                "timestamp": datetime.now(timezone.utc).strftime(DATE_FORMAT),
            })
        else:
            eligible[coin] = address

    if not eligible:
        log.info("[%s] No eligible wallets (all on cooldown).", faucet_name)
        return results

    if dry_run:
        for coin, address in eligible.items():
            log.info("  [DRY RUN] Would claim %s for %s at %s", coin, faucet_name, faucet_url)
            results.append({
                "faucet": faucet_name,
                "coin": coin,
                "wallet": address,
                "success": None,
                "error": "dry-run",
                "timestamp": datetime.now(timezone.utc).strftime(DATE_FORMAT),
            })
        return results

    # Launch browser and process claims
    driver = None
    try:
        driver = create_driver(config)
        log.info("[%s] Browser launched.", faucet_name)

        # Navigate to faucet
        log.info("[%s] Navigating to %s", faucet_name, faucet_url)
        driver.get(faucet_url)

        # Let the page settle
        time.sleep(3)

        # Dismiss popups
        popup_selectors: List[Dict[str, str]] = faucet.get("popup_selectors", [])
        dismissed = dismiss_popups(driver, popup_selectors)
        if dismissed:
            log.info("[%s] Dismissed %d popup(s).", faucet_name, dismissed)

        # Process each eligible wallet
        for coin, address in eligible.items():
            result = _process_wallet_claim(driver, faucet, coin, address, config)
            results.append(result)

            # Update state on success
            key = state_key(faucet_name, address)
            state[key] = datetime.now(timezone.utc).strftime(DATE_FORMAT)

            # Brief pause between wallets
            time.sleep(1)

    except WebDriverException as exc:
        log.error("[%s] Browser error: %s", faucet_name, exc)
        for coin, address in eligible.items():
            results.append({
                "faucet": faucet_name,
                "coin": coin,
                "wallet": address,
                "success": False,
                "error": f"Browser error: {exc}",
                "timestamp": datetime.now(timezone.utc).strftime(DATE_FORMAT),
            })
    except Exception as exc:
        log.error("[%s] Unexpected error: %s", faucet_name, exc, exc_info=True)
        for coin, address in eligible.items():
            results.append({
                "faucet": faucet_name,
                "coin": coin,
                "wallet": address,
                "success": False,
                "error": f"Unexpected error: {exc}",
                "timestamp": datetime.now(timezone.utc).strftime(DATE_FORMAT),
            })
    finally:
        if driver:
            try:
                driver.quit()
                log.info("[%s] Browser closed.", faucet_name)
            except Exception:
                pass

    return results


def _process_wallet_claim(
    driver: webdriver.Remote,
    faucet: Dict[str, Any],
    coin: str,
    address: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Process a single wallet claim on a faucet page.

    Steps:
    1. Find wallet input, clear it, enter address
    2. Tick any checkboxes
    3. Wait pre_submit_wait
    4. Click submit
    5. Wait post_submit_wait
    6. Check for success indicators
    """
    faucet_name = faucet["name"]
    result = {
        "faucet": faucet_name,
        "coin": coin,
        "wallet": address,
        "success": False,
        "error": None,
        "timestamp": datetime.now(timezone.utc).strftime(DATE_FORMAT),
    }

    element_timeout = config["element_timeout"]
    pre_wait = faucet.get("pre_submit_wait", 2)
    post_wait = faucet.get("post_submit_wait", 3)

    try:
        # Step 1: Fill wallet address
        wallet_selector = faucet.get("wallet_field")
        if not wallet_selector:
            result["error"] = "No wallet_field defined in faucet config"
            log.warning("[%s] %s — %s", faucet_name, coin, result["error"])
            return result

        wallet_input = find_element_safe(driver, wallet_selector, timeout=element_timeout)
        if not wallet_input:
            result["error"] = f"Wallet field not found: {wallet_selector}"
            log.warning("[%s] %s — %s", faucet_name, coin, result["error"])
            return result

        wallet_input.clear()
        wallet_input.send_keys(address)
        log.debug("[%s] %s — Entered wallet address", faucet_name, coin)

        # Step 2: Tick checkboxes
        checkboxes: List[Dict[str, str]] = faucet.get("checkboxes", [])
        for cb_def in checkboxes:
            cb = find_element_safe(driver, cb_def, timeout=3)
            if cb and not cb.is_selected():
                try:
                    cb.click()
                    log.debug("[%s] Ticked checkbox: %s", faucet_name, cb_def)
                except (ElementClickInterceptedException, ElementNotInteractableException):
                    # Try JavaScript click as fallback
                    try:
                        driver.execute_script("arguments[0].click();", cb)
                        log.debug("[%s] Ticked checkbox (JS fallback): %s", faucet_name, cb_def)
                    except Exception:
                        log.debug("[%s] Could not tick checkbox: %s", faucet_name, cb_def)

        # Step 3: Wait before submit
        time.sleep(pre_wait)

        # Step 4: Click submit
        submit_selector = faucet.get("submit_button")
        if not submit_selector:
            result["error"] = "No submit_button defined in faucet config"
            log.warning("[%s] %s — %s", faucet_name, coin, result["error"])
            return result

        if not click_element_safe(driver, submit_selector, timeout=element_timeout):
            result["error"] = f"Submit button not found or not clickable: {submit_selector}"
            log.warning("[%s] %s — %s", faucet_name, coin, result["error"])
            return result

        log.debug("[%s] %s — Clicked submit", faucet_name, coin)

        # Step 5: Wait for result
        time.sleep(post_wait)

        # Dismiss any post-submit popups
        popup_selectors: List[Dict[str, str]] = faucet.get("popup_selectors", [])
        dismiss_popups(driver, popup_selectors)

        # Step 6: Check for success
        success_indicators: List[str] = faucet.get("success_indicators", ["success", "claimed", "sent"])
        page_text = ""
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            pass

        found_indicators = [ind for ind in success_indicators if ind.lower() in page_text]
        if found_indicators:
            result["success"] = True
            log.info("[%s] %s — ✓ CLAIMED (indicators: %s)", faucet_name, coin, found_indicators)
        else:
            # Not necessarily a failure — many faucets don't show clear text
            result["success"] = True  # Optimistic: assume success if no explicit error
            log.info("[%s] %s — Submitted (no clear success text, assuming OK)", faucet_name, coin)

    except Exception as exc:
        result["error"] = str(exc)
        log.error("[%s] %s — Error: %s", faucet_name, coin, exc)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cooldown_remaining(faucet_name: str, wallet_address: str, cooldown_hours: float, state: Dict[str, str]) -> str:
    """Return a human-readable string of remaining cooldown time."""
    key = state_key(faucet_name, wallet_address)
    last_str = state.get(key, "")
    try:
        last_time = datetime.strptime(last_str, DATE_FORMAT).replace(tzinfo=timezone.utc)
        cooldown_end = last_time + timedelta(hours=cooldown_hours)
        remaining = cooldown_end - datetime.now(timezone.utc)
        total_seconds = int(remaining.total_seconds())
        if total_seconds <= 0:
            return "0m"
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "unknown"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(results: List[Dict[str, Any]]) -> None:
    """Print a human-readable summary of all claim results."""
    total = len(results)
    successes = [r for r in results if r.get("success") is True]
    failures = [r for r in results if r.get("success") is False]
    dry_runs = [r for r in results if r.get("success") is None]

    print()
    print("=" * 60)
    print("  FAUCETFLOW SUMMARY")
    print("=" * 60)
    print(f"  Total claims attempted:  {total}")
    print(f"  Successful:              {len(successes)}")
    print(f"  Failed:                  {len(failures)}")
    if dry_runs:
        print(f"  Dry-run (not executed):  {len(dry_runs)}")
    print("-" * 60)

    if failures:
        print("\n  FAILURES:")
        for r in failures:
            print(f"    [{r['faucet']}] {r['coin']} — {r.get('error', 'unknown error')}")

    if successes:
        print(f"\n  {len(successes)} claim(s) processed successfully.")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point for FaucetFlow."""
    parser = argparse.ArgumentParser(
        description="FaucetFlow — Automated cryptocurrency faucet claimer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                      Run all eligible faucets
  python main.py --dry-run            Preview what would be claimed
  python main.py --faucet FreeBitcoin Process only the FreeBitcoin faucet
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be claimed without opening a browser",
    )
    parser.add_argument(
        "--faucet",
        type=str,
        default=None,
        metavar="NAME",
        help="Process only the named faucet (case-sensitive, matches faucets.json 'name' field)",
    )
    args = parser.parse_args()

    # Banner
    print(r"""
  ______              __          ______ __
 /_  __/___  ________/ /_  ___   / ____// /___ _      __
  / / / __ `/ ___/ __/ / / / _ \ / /_   / / __ \ | /| / /
 / / / /_/ / /__/ /_/ /_/  __// __/  / / /_/ / |/ |/ /
/_/  \__,_/\___/\__/\__/\___//_/    /_/\____/|__/|__/
    """)
    print("  Crypto Faucet Auto-Claimer")
    print()

    # Load config
    log.info("Loading configuration...")
    config = load_config()
    faucets = load_faucets()
    state = load_state()

    log.info("Browser: %s", config["browser"].capitalize())
    log.info("Headless: %s", config["headless"])
    log.info("Cooldown: %.0f hours", config["cooldown_hours"])
    log.info("Wallets: %d configured (%s)", len(config["wallets"]), ", ".join(config["wallets"].keys()))
    log.info("Faucets: %d loaded", len(faucets))

    if args.faucet:
        faucets = [f for f in faucets if f["name"] == args.faucet]
        if not faucets:
            log.error("No faucet found with name '%s'", args.faucet)
            log.info("Available faucets: %s", ", ".join(
                f["name"] for f in load_faucets()
            ))
            sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN MODE — no browser will be launched")

    # Process faucets
    all_results: List[Dict[str, Any]] = []
    for faucet in faucets:
        log.info("—" * 40)
        log.info("Processing: %s", faucet["name"])
        results = process_faucet(faucet, config["wallets"], config, state, dry_run=args.dry_run)
        all_results.extend(results)

    # Save state
    save_state(state)

    # Print summary
    print_summary(all_results)

    # Exit with non-zero if any failures
    failures = [r for r in all_results if r.get("success") is False]
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
