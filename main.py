#!/usr/bin/env python3
"""
FaucetFlow — Automated cryptocurrency faucet claimer.

Drives a real browser (Chrome or Edge) via Selenium to visit configured
faucet sites, fill in wallet addresses, submit claims, and log results.
Enforces a 24-hour cooldown between claims per faucet/wallet pair.

Usage:
    python main.py                  # Run all eligible faucets
    python main.py --dry-run        # Show what would be claimed
    python main.py --faucet NAME    # Process only a specific faucet
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
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

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
FAUCETS_FILE = SCRIPT_DIR / "faucets.json"
STATE_FILE = SCRIPT_DIR / "last_access.json"
LOG_FILE = SCRIPT_DIR / "faucetflow.log"

COOLDOWN_MS = 24 * 60 * 60 * 1000  # 24 hours in milliseconds

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
    console_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
    )
    console.setFormatter(console_fmt)

    # File handler (DEBUG and above)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  [%(funcName)s]  %(message)s"
    )
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

    wallets_raw = os.getenv("WALLET_ADDRESSES", "")
    wallets = [w.strip() for w in wallets_raw.split(",") if w.strip()]

    config: Dict[str, Any] = {
        "wallets": wallets,
        "max_threads": int(os.getenv("MAX_THREADS", "1")),
        "headless": os.getenv("HEADLESS", "false").lower() == "true",
        "retry_count": int(os.getenv("RETRY_COUNT", "0")),
        "retry_backoff_seconds": float(os.getenv("RETRY_BACKOFF_SECONDS", "5")),
        "default_wait_timeout": int(os.getenv("DEFAULT_WAIT_TIMEOUT", "60")),
        "close_ad_attempts": int(os.getenv("CLOSE_AD_ATTEMPTS", "1")),
        "close_ad_retry_delay": float(os.getenv("CLOSE_AD_RETRY_DELAY", "5")),
        "browser": os.getenv("BROWSER", "edge").lower(),
        "driver_path": os.getenv("DRIVER_PATH", "").strip(),
        "browser_data_dir": os.getenv("BROWSER_DATA_DIR", "").strip(),
        "browser_profile": os.getenv("BROWSER_PROFILE", "Default").strip(),
    }

    # Validate browser choice
    if config["browser"] not in ("chrome", "edge"):
        log.error("BROWSER must be 'chrome' or 'edge', got '%s'", config["browser"])
        sys.exit(1)

    if not config["wallets"]:
        log.error("No wallets configured. Set WALLET_ADDRESSES in .env")
        log.error("Format: WALLET_ADDRESSES=0xAddr1,0xAddr2")
        sys.exit(1)

    return config


def load_faucets() -> Dict[str, Dict[str, Any]]:
    """
    Load faucet definitions from faucets.json.

    Returns a dict keyed by faucet name. Filters out comment-only entries
    (those whose key starts with '_').

    Validates that no faucet has both wallet_address_field and
    connect_wallet_button set.
    """
    if not FAUCETS_FILE.exists():
        log.error("faucets.json not found at %s", FAUCETS_FILE)
        sys.exit(1)

    with open(FAUCETS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    faucets: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        # Skip comment / metadata entries
        if key.startswith("_"):
            continue
        if not isinstance(value, dict):
            continue

        # Mutual exclusivity check
        wallet_field = value.get("wallet_address_field")
        connect_btn = value.get("connect_wallet_button")
        if wallet_field and connect_btn:
            log.error(
                "Faucet '%s' has both wallet_address_field and connect_wallet_button "
                "set. They are mutually exclusive — only one may be non-null.",
                key,
            )
            sys.exit(1)
        if not wallet_field and not connect_btn:
            log.error(
                "Faucet '%s' must have either wallet_address_field or "
                "connect_wallet_button set (exactly one).",
                key,
            )
            sys.exit(1)

        faucets[key] = value

    log.debug("Loaded %d faucet definitions", len(faucets))
    return faucets


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------


def load_state() -> Dict[str, Dict[str, Any]]:
    """
    Load the last_access state from last_access.json.

    Returns an empty dict if the file is missing or corrupt.
    Keys: "faucet_name:wallet_index"
    Values: {"last_access_time": <unix_millis>, "message": "<result>"}
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


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    """Persist the last_access state to disk as JSON."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        log.debug("State saved (%d entries)", len(state))
    except OSError as exc:
        log.error("Failed to save state file: %s", exc)


def state_key(faucet_name: str, wallet_index: int) -> str:
    """Create a deterministic state key for a faucet+wallet_index pair."""
    return f"{faucet_name}:{wallet_index}"


def is_on_cooldown(faucet_name: str, wallet_index: int, state: Dict[str, Dict[str, Any]]) -> bool:
    """Check if the given faucet+wallet_index pair is still within the 24-hour cooldown."""
    key = state_key(faucet_name, wallet_index)
    entry = state.get(key)
    if not entry:
        return False

    last_time = entry.get("last_access_time", 0)
    now_ms = int(time.time() * 1000)
    return (now_ms - last_time) < COOLDOWN_MS


def cooldown_remaining_ms(faucet_name: str, wallet_index: int, state: Dict[str, Dict[str, Any]]) -> int:
    """Return remaining cooldown time in milliseconds (0 if none)."""
    key = state_key(faucet_name, wallet_index)
    entry = state.get(key)
    if not entry:
        return 0

    last_time = entry.get("last_access_time", 0)
    now_ms = int(time.time() * 1000)
    elapsed = now_ms - last_time
    remaining = COOLDOWN_MS - elapsed
    return max(0, remaining)


def cooldown_remaining_str(remaining_ms: int) -> str:
    """Format remaining cooldown milliseconds into a human-readable string."""
    if remaining_ms <= 0:
        return "0m"
    total_seconds = remaining_ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Selector Resolution
# ---------------------------------------------------------------------------


def resolve_selector(selector_string: str) -> Tuple[str, str]:
    """
    Convert a plain selector string into a (By.*, value) tuple.

    If the string starts with 'xpath:', the remainder is used as an XPath
    expression. Otherwise the full string is used as a CSS selector.
    """
    if selector_string.startswith("xpath:"):
        return (By.XPATH, selector_string[6:])
    return (By.CSS_SELECTOR, selector_string)


# ---------------------------------------------------------------------------
# Browser Setup
# ---------------------------------------------------------------------------


def create_driver(config: Dict[str, Any]) -> webdriver.Remote:
    """
    Create and return a Selenium WebDriver instance for Chrome or Edge.

    Uses the DRIVER_PATH env var to set the Service executable path directly.
    Loads the user's browser profile if configured so saved logins and
    wallet extensions are available.
    """
    browser = config["browser"]
    driver_path = config["driver_path"]
    data_dir = config["browser_data_dir"]
    profile = config["browser_profile"]

    if browser == "chrome":
        options = ChromeOptions()
        _apply_common_options(options, config)
        service_kwargs = {}
        if driver_path:
            service_kwargs["executable_path"] = driver_path
        service = ChromeService(**service_kwargs)
        driver = webdriver.Chrome(service=service, options=options)

    elif browser == "edge":
        options = EdgeOptions()
        _apply_common_options(options, config)
        service_kwargs = {}
        if driver_path:
            service_kwargs["executable_path"] = driver_path
        service = EdgeService(**service_kwargs)
        driver = webdriver.Edge(service=service, options=options)

    else:
        raise ValueError(f"Unsupported browser: {browser}")

    return driver


def _apply_common_options(options: Any, config: Dict[str, Any]) -> None:
    """Apply options common to both Chrome and Edge."""
    if config["headless"]:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    # User profile
    data_dir = config["browser_data_dir"]
    if data_dir:
        options.add_argument(f"--user-data-dir={data_dir}")
        profile = config.get("browser_profile", "Default")
        if profile:
            options.add_argument(f"--profile-directory={profile}")
        log.info("Using browser profile: %s (profile: %s)", data_dir, profile)
    else:
        log.info("No browser profile configured; using fresh temporary profile.")

    # Anti-detection and stability flags
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Disable various prompts
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    options.add_experimental_option("prefs", prefs)


# ---------------------------------------------------------------------------
# Ad / Popup Dismissal
# ---------------------------------------------------------------------------


def close_ads(driver: webdriver.Remote, close_button_selector: Optional[str],
              attempts: int, retry_delay: float) -> int:
    """
    Attempt to close popup ads using the close button selector.

    Tries up to `attempts` times with `retry_delay` seconds between attempts.
    Returns the number of ads dismissed.
    """
    if not close_button_selector:
        return 0

    dismissed = 0
    for attempt in range(attempts):
        try:
            by, value = resolve_selector(close_button_selector)
            element = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((by, value))
            )
            element.click()
            dismissed += 1
            log.debug("Dismissed ad (attempt %d/%d)", attempt + 1, attempts)
            time.sleep(0.5)  # Let animation finish
        except (TimeoutException, NoSuchElementException):
            log.debug("No ad found (attempt %d/%d)", attempt + 1, attempts)
            break
        except (ElementClickInterceptedException, ElementNotInteractableException,
                StaleElementReferenceException):
            log.debug("Ad close click intercepted (attempt %d/%d)", attempt + 1, attempts)
            time.sleep(retry_delay)

    return dismissed


# ---------------------------------------------------------------------------
# Claim Logic
# ---------------------------------------------------------------------------


def process_all_faucets(
    faucets: Dict[str, Dict[str, Any]],
    config: Dict[str, Any],
    state: Dict[str, Dict[str, Any]],
    target_faucet: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """
    Process all faucets (or a single target) and return results.

    Each faucet is processed with its own browser session.
    """
    all_results: List[Dict[str, Any]] = []
    wallets: List[str] = config["wallets"]

    for faucet_name, faucet_def in faucets.items():
        if target_faucet and faucet_name != target_faucet:
            continue

        log.info("—" * 40)
        log.info("Processing: %s", faucet_name)

        results = process_single_faucet(
            faucet_name, faucet_def, wallets, config, state, dry_run
        )
        all_results.extend(results)

    return all_results


def process_single_faucet(
    faucet_name: str,
    faucet_def: Dict[str, Any],
    wallets: List[str],
    config: Dict[str, Any],
    state: Dict[str, Dict[str, Any]],
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Process all eligible wallet indices for a single faucet."""
    results: List[Dict[str, Any]] = []

    # Determine which wallet indices to use
    wallet_indexes = faucet_def.get("wallet_indexes")
    if wallet_indexes is not None:
        target_indices = [i for i in wallet_indexes if 0 <= i < len(wallets)]
    else:
        target_indices = list(range(len(wallets)))

    if not target_indices:
        log.info("  No wallet indices configured for this faucet.")
        return results

    # Separate eligible vs cooldown
    eligible: List[int] = []
    for wi in target_indices:
        if is_on_cooldown(faucet_name, wi, state):
            remaining_ms = cooldown_remaining_ms(faucet_name, wi, state)
            remaining_str = cooldown_remaining_str(remaining_ms)
            address = wallets[wi]
            log.info(
                "  [Wallet %d] %s — SKIP (cooldown, %s remaining)",
                wi, address, remaining_str,
            )
            results.append({
                "faucet": faucet_name,
                "wallet_index": wi,
                "wallet": address,
                "success": False,
                "error": f"On cooldown ({remaining_str} remaining)",
            })
        else:
            eligible.append(wi)

    if not eligible:
        log.info("  No eligible wallets (all on cooldown).")
        return results

    # Dry-run: just report what would happen
    if dry_run:
        for wi in eligible:
            address = wallets[wi]
            log.info(
                "  [DRY RUN] Would claim wallet %d (%s) for %s at %s",
                wi, address, faucet_name, faucet_def["url"],
            )
            results.append({
                "faucet": faucet_name,
                "wallet_index": wi,
                "wallet": address,
                "success": None,
                "error": "dry-run",
            })
        return results

    # Launch browser once for this faucet
    driver = None
    try:
        driver = create_driver(config)
        log.info("  Browser launched.")

        # Navigate to faucet
        url = faucet_def["url"]
        log.info("  Navigating to %s", url)
        driver.get(url)
        time.sleep(3)  # Let page settle

        # Dismiss initial popups
        close_ad_selector = faucet_def.get("close_ad_button")
        dismissed = close_ads(
            driver, close_ad_selector,
            config["close_ad_attempts"], config["close_ad_retry_delay"],
        )
        if dismissed:
            log.info("  Dismissed %d popup ad(s).", dismissed)

        # Process each eligible wallet
        for wi in eligible:
            address = wallets[wi]
            result = claim_with_retry(
                driver, faucet_name, faucet_def, wi, address, config
            )
            results.append(result)

            # Update state on success
            if result.get("success"):
                key = state_key(faucet_name, wi)
                state[key] = {
                    "last_access_time": int(time.time() * 1000),
                    "message": f"SUCCESS: {result.get('message', 'claimed')}",
                }

            # Brief pause between wallets on the same page
            time.sleep(1)

    except WebDriverException as exc:
        log.error("  Browser error: %s", exc)
        for wi in eligible:
            results.append({
                "faucet": faucet_name,
                "wallet_index": wi,
                "wallet": wallets[wi],
                "success": False,
                "error": f"Browser error: {exc}",
            })
    except Exception as exc:
        log.error("  Unexpected error: %s", exc, exc_info=True)
        for wi in eligible:
            results.append({
                "faucet": faucet_name,
                "wallet_index": wi,
                "wallet": wallets[wi],
                "success": False,
                "error": f"Unexpected error: {exc}",
            })
    finally:
        if driver:
            try:
                driver.quit()
                log.info("  Browser closed.")
            except Exception:
                pass

    return results


def claim_with_retry(
    driver: webdriver.Remote,
    faucet_name: str,
    faucet_def: Dict[str, Any],
    wallet_index: int,
    address: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Attempt a single claim with retry logic.

    On failure, retries up to RETRY_COUNT times with exponential backoff:
    RETRY_BACKOFF_SECONDS * attempt_number.
    """
    retry_count = config["retry_count"]
    backoff_base = config["retry_backoff_seconds"]

    last_error: Optional[str] = None

    for attempt in range(retry_count + 1):  # 0..retry_count inclusive
        if attempt > 0:
            wait = backoff_base * attempt
            log.info(
                "  [Wallet %d] Retry %d/%d — waiting %.1fs...",
                wallet_index, attempt, retry_count, wait,
            )
            time.sleep(wait)

        result = attempt_single_claim(
            driver, faucet_name, faucet_def, wallet_index, address, config
        )

        if result.get("success"):
            return result

        last_error = result.get("error", "unknown error")

    # All attempts exhausted
    return {
        "faucet": faucet_name,
        "wallet_index": wallet_index,
        "wallet": address,
        "success": False,
        "error": f"All {retry_count + 1} attempts failed. Last error: {last_error}",
    }


def attempt_single_claim(
    driver: webdriver.Remote,
    faucet_name: str,
    faucet_def: Dict[str, Any],
    wallet_index: int,
    address: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Execute a single claim attempt (no retries — that's handled upstream).

    Steps:
    1. Fill wallet address OR click connect_wallet_button
    2. Tick checkbox if configured
    3. Click submit
    4. Wait for success or error message element to appear
    5. Return result
    """
    result: Dict[str, Any] = {
        "faucet": faucet_name,
        "wallet_index": wallet_index,
        "wallet": address,
        "success": False,
        "error": None,
        "message": None,
    }

    try:
        # Step 1: Fill wallet address OR connect wallet
        connect_btn = faucet_def.get("connect_wallet_button")
        wallet_field = faucet_def.get("wallet_address_field")

        if connect_btn:
            # Click connect wallet button (e.g. MetaMask)
            by, value = resolve_selector(connect_btn)
            try:
                btn = WebDriverWait(driver, config["default_wait_timeout"]).until(
                    EC.element_to_be_clickable((by, value))
                )
                btn.click()
                log.debug("  [Wallet %d] Clicked connect wallet button.", wallet_index)
                time.sleep(2)  # Wait for wallet popup / connection
            except (TimeoutException, NoSuchElementException,
                    ElementClickInterceptedException) as exc:
                result["error"] = f"Connect wallet button not found/clickable: {connect_btn} — {exc}"
                log.warning("  [Wallet %d] %s", wallet_index, result["error"])
                return result
        elif wallet_field:
            by, value = resolve_selector(wallet_field)
            try:
                field = WebDriverWait(driver, config["default_wait_timeout"]).until(
                    EC.presence_of_element_located((by, value))
                )
                field.clear()
                field.send_keys(address)
                log.debug("  [Wallet %d] Entered wallet address.", wallet_index)
            except (TimeoutException, NoSuchElementException,
                    ElementNotInteractableException) as exc:
                result["error"] = f"Wallet field not found: {wallet_field} — {exc}"
                log.warning("  [Wallet %d] %s", wallet_index, result["error"])
                return result

        # Step 2: Tick checkbox (optional)
        checkbox_sel = faucet_def.get("checkbox")
        if checkbox_sel:
            by, value = resolve_selector(checkbox_sel)
            try:
                cb = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((by, value))
                )
                if not cb.is_selected():
                    try:
                        cb.click()
                    except (ElementClickInterceptedException, ElementNotInteractableException):
                        driver.execute_script("arguments[0].click();", cb)
                log.debug("  [Wallet %d] Ticked checkbox.", wallet_index)
            except (TimeoutException, NoSuchElementException):
                log.debug("  [Wallet %d] Checkbox not found (continuing).", wallet_index)

        # Brief wait before submit
        time.sleep(1)

        # Step 3: Click submit
        submit_sel = faucet_def.get("submit_button")
        if not submit_sel:
            result["error"] = "No submit_button defined in faucet config"
            log.warning("  [Wallet %d] %s", wallet_index, result["error"])
            return result

        by, value = resolve_selector(submit_sel)
        try:
            btn = WebDriverWait(driver, config["default_wait_timeout"]).until(
                EC.element_to_be_clickable((by, value))
            )
            btn.click()
            log.debug("  [Wallet %d] Clicked submit.", wallet_index)
        except (TimeoutException, NoSuchElementException,
                ElementClickInterceptedException, ElementNotInteractableException) as exc:
            result["error"] = f"Submit button not found/clickable: {submit_sel} — {exc}"
            log.warning("  [Wallet %d] %s", wallet_index, result["error"])
            return result

        # Step 4: Wait for success or error message element
        outcome = wait_for_outcome(driver, faucet_def, config["default_wait_timeout"])
        if outcome == "success":
            result["success"] = True
            result["message"] = "claim submitted successfully"
            log.info("  [Wallet %d] %s — ✓ SUCCESS", wallet_index, address)
        elif outcome == "error":
            result["success"] = False
            result["error"] = "Error message detected after submit"
            log.warning("  [Wallet %d] %s — ERROR detected", wallet_index, address)
        else:
            # timeout — neither appeared; optimistic success
            result["success"] = True
            result["message"] = "no confirmation element (assuming OK)"
            log.info("  [Wallet %d] %s — Submitted (no confirmation, assuming OK)", wallet_index, address)

    except Exception as exc:
        result["error"] = str(exc)
        log.error("  [Wallet %d] %s — Exception: %s", wallet_index, address, exc)

    return result


def wait_for_outcome(
    driver: webdriver.Remote,
    faucet_def: Dict[str, Any],
    timeout: int,
) -> Optional[str]:
    """
    Wait up to `timeout` seconds for either a success or error message element
    to appear on the page.

    Returns 'success', 'error', or None (timeout — neither appeared).
    """
    success_sel = faucet_def.get("success_message_html")
    error_sel = faucet_def.get("error_message_html")

    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check for success
        if success_sel:
            try:
                by, value = resolve_selector(success_sel)
                driver.find_element(by, value)
                log.debug("  Success element found: %s", success_sel)
                return "success"
            except NoSuchElementException:
                pass

        # Check for error
        if error_sel:
            try:
                by, value = resolve_selector(error_sel)
                driver.find_element(by, value)
                log.debug("  Error element found: %s", error_sel)
                return "error"
            except NoSuchElementException:
                pass

        time.sleep(0.5)

    # Neither appeared within timeout
    log.debug("  Outcome wait timed out after %ds.", timeout)
    return None


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
            print(f"    [{r['faucet']}] Wallet {r['wallet_index']} ({r['wallet']}) "
                  f"— {r.get('error', 'unknown error')}")

    if successes:
        print(f"\n  {len(successes)} claim(s) processed successfully.")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Signal Handler
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _signal_handler(signum: int, frame: Any) -> None:
    """Set the shutdown flag on SIGINT."""
    global _shutdown_requested
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point for FaucetFlow."""
    global _shutdown_requested

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

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
        help="Process only the named faucet (matches faucet name in faucets.json)",
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
    log.info("Cooldown: 24 hours")
    log.info("Wallets: %d configured", len(config["wallets"]))
    for i, addr in enumerate(config["wallets"]):
        log.info("  [%d] %s", i, addr)
    log.info("Faucets: %d loaded", len(faucets))
    log.info("Retries: %d (backoff: %.0fs base)", config["retry_count"], config["retry_backoff_seconds"])

    if args.faucet:
        if args.faucet not in faucets:
            log.error("No faucet found with name '%s'", args.faucet)
            log.info("Available faucets: %s", ", ".join(sorted(faucets.keys())))
            sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN MODE — no browser will be launched")

    # Process faucets
    all_results: List[Dict[str, Any]] = []

    try:
        all_results = process_all_faucets(
            faucets, config, state,
            target_faucet=args.faucet,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        _shutdown_requested = True

    if _shutdown_requested:
        log.info("Shutting down gracefully...")

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
