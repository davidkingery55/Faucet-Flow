# FaucetFlow

**Automated cryptocurrency faucet claimer** — drives a real browser (Chrome or Edge)
via Selenium to visit configured faucet sites, fill in wallet addresses, submit claims,
and log results.

## Features

- 🔄 **Real browser automation** — uses your actual Chrome/Edge profile so saved
  logins, wallet extensions (MetaMask, etc.), and session cookies are available.
- ⏱️ **24-hour cooldown enforcement** — tracks cooldowns per faucet/wallet-index
  pair in a local JSON state file using UNIX millisecond timestamps.
- 🛡️ **Resilient** — every Selenium operation is wrapped in try/except with
  configurable retries and exponential backoff.
- 🔌 **Connect wallet support** — faucets that use browser wallet connections
  (e.g. MetaMask) are supported via the `connect_wallet_button` field.
- 📋 **Dry-run mode** — preview what would be claimed without opening a browser.
- 🎯 **Targeted runs** — process a single faucet with `--faucet NAME`.
- 📝 **Full logging** — logs to both console and `faucetflow.log`.
- 🛑 **Graceful shutdown** — Ctrl+C saves state and quits cleanly.

## Prerequisites

- **Python 3.9+**
- **Google Chrome** or **Microsoft Edge** installed
- **WebDriver** matching your browser version (see below)
- (Optional) A browser profile with your wallet extensions and faucet site logins

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url>
cd faucetflow

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate     # Linux/macOS
venv\Scripts\activate        # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Download the WebDriver for your browser
#    Chrome:  https://chromedriver.chromium.org/
#    Edge:    https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/
#    Place the driver in the project directory or set DRIVER_PATH in .env.

# 5. Configure your environment
cp .env.example .env
# Edit .env with your wallet addresses, browser settings, and driver path

# 6. Run!
python main.py
```

## Manual WebDriver Setup

FaucetFlow does **not** auto-download WebDrivers. You must download the driver
manually and configure `DRIVER_PATH` in `.env`:

1. Check your browser version: `chrome://version` or `edge://version`
2. Download the matching driver:
   - **Chrome:** https://chromedriver.chromium.org/downloads
   - **Edge:** https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/
3. Extract the executable (`chromedriver` / `msedgedriver`) to a known location
4. Set `DRIVER_PATH` in `.env` to the full path of the executable

Example:
```
DRIVER_PATH=./msedgedriver.exe          # Windows, in project dir
DRIVER_PATH=/usr/local/bin/chromedriver # Linux
```

## Configuration (.env)

Copy `.env.example` to `.env` and edit:

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_ADDRESSES` | *(required)* | Comma-separated wallet addresses. E.g.: `0xAddr1,0xAddr2`. Each address is referenced by its 0-based index. |
| `BROWSER` | `edge` | Which browser: `chrome` or `edge` |
| `HEADLESS` | `false` | Run without a visible window? (`true`/`false`) |
| `DRIVER_PATH` | *(empty)* | Path to the chromedriver/msgedgedriver executable |
| `BROWSER_DATA_DIR` | *(empty)* | Path to browser user data directory |
| `BROWSER_PROFILE` | `Default` | Profile folder name within the user data dir |
| `MAX_THREADS` | `1` | Number of concurrent threads (keep at 1) |
| `RETRY_COUNT` | `0` | Retries per faucet claim on failure |
| `RETRY_BACKOFF_SECONDS` | `5` | Base backoff seconds (multiplied by attempt number) |
| `DEFAULT_WAIT_TIMEOUT` | `60` | Max seconds to wait for success/error message after submit |
| `CLOSE_AD_ATTEMPTS` | `1` | Number of attempts to close popup ads |
| `CLOSE_AD_RETRY_DELAY` | `5` | Delay in seconds between ad-close attempts |

### Finding Your Browser Profile Path

**Chrome:**
- **Linux:** `/home/YOURUSER/.config/google-chrome`
- **macOS:** `/Users/YOURUSER/Library/Application Support/Google/Chrome`
- **Windows:** `C:\Users\YOURUSER\AppData\Local\Google\Chrome\User Data`

**Edge:**
- **Linux:** `/home/YOURUSER/.config/microsoft-edge`
- **macOS:** `/Users/YOURUSER/Library/Application Support/Microsoft Edge`
- **Windows:** `C:\Users\YOURUSER\AppData\Local\Microsoft\Edge\User Data`

> **Tip:** Close the browser completely before running FaucetFlow — a
> browser profile can only be used by one process at a time.

To find which profile directory to use, visit `chrome://version` (Chrome) or
`edge://version` (Edge) in your browser and look for "Profile Path".

## Usage

```bash
# Run all eligible faucets
python main.py

# Preview without opening a browser
python main.py --dry-run

# Process only a specific faucet
python main.py --faucet FreeBitcoin
python main.py --faucet FaucetCrypto --dry-run
```

## Faucet Definitions (faucets.json)

Faucets are defined in `faucets.json` as a **dict keyed by faucet name** (not an array).
Keys starting with `_` are treated as comments and ignored.

Each entry:

```json
{
    "FreeBitcoin": {
        "url": "https://freebitco.in",
        "wallet_address_field": "#btcAddress",
        "connect_wallet_button": null,
        "checkbox": "input[type='checkbox']",
        "submit_button": "#free_play_form_button",
        "close_ad_button": ".close-button",
        "error_message_html": ".alert-danger",
        "success_message_html": ".alert-success",
        "wallet_indexes": [0]
    }
}
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Faucet page URL |
| `wallet_address_field` | Conditional | CSS/XPath selector for the wallet address input field |
| `connect_wallet_button` | Conditional | CSS/XPath selector for a "Connect Wallet" button (e.g. MetaMask) |
| `checkbox` | No | Single checkbox selector to tick before submitting |
| `submit_button` | Yes | CSS/XPath selector for the submit/claim button |
| `close_ad_button` | No | Selector for popup/ad close button |
| `error_message_html` | No | Selector for error message element (checked after submit) |
| `success_message_html` | No | Selector for success message element (checked after submit) |
| `wallet_indexes` | No | Array of 0-based wallet indices to use. Omit to use all wallets. |

> **⚠️ IMPORTANT:** Faucet websites change their HTML frequently. All selectors
> are best-effort and WILL need adjustment over time. If a faucet stops working,
> inspect the page live and update the selectors.

### Mutual Exclusivity

`wallet_address_field` and `connect_wallet_button` are **mutually exclusive**.
Exactly one must be non-null. If both are set (or neither), FaucetFlow exits
with an error at startup.

- **`wallet_address_field`**: The script types the wallet address into the field.
- **`connect_wallet_button`**: The script clicks the button to trigger a browser
  wallet connection (e.g. MetaMask popup). The user must have the wallet extension
  installed in their browser profile.

### Selector Format

Selectors are **plain strings**:
- By default, treated as **CSS selectors** (e.g. `"#id"`, `".class"`, `"input[name='foo']"`)
- Prefix with `xpath:` for XPath expressions (e.g. `"xpath://button[contains(text(),'Claim')]"`)

```json
{
    "wallet_address_field": "#btcAddress",
    "submit_button": "button[type='submit']",
    "close_ad_button": "xpath://button[contains(text(),'Close')]"
}
```

### Adding a New Faucet

1. Visit the faucet site in your browser
2. Right-click the wallet address field → Inspect — note the `id`, `name`, or CSS class
3. Right-click the submit button → Inspect — note its selector
4. Add a new entry to `faucets.json` with those selectors
5. Run `python main.py --faucet YourFaucetName --dry-run` to test
6. Run without `--dry-run` when ready

## How It Works

```
1. Load config from .env (wallet addresses, browser settings)
2. Load faucet definitions from faucets.json (validate mutual exclusivity)
3. Load cooldown state from last_access.json
4. For each faucet:
   a. Determine eligible wallet indices (skip those on cooldown)
   b. Launch Chrome/Edge with your profile
   c. Navigate to faucet URL
   d. Attempt to dismiss popup ads (CLOSE_AD_ATTEMPTS with retry delay)
   e. For each eligible wallet index:
      - Fill wallet address OR click connect wallet button
      - Tick checkbox if configured
      - Click submit button
      - Wait up to DEFAULT_WAIT_TIMEOUT for success or error message
      - On failure, retry up to RETRY_COUNT times with exponential backoff
      - Log result, update cooldown state
5. Print summary of all claims
```

## State File

`last_access.json` tracks the last claim time per faucet+wallet-index pair.
It's automatically created on first run and updated after each claim.

```json
{
  "FreeBitcoin:0": {
    "last_access_time": 1753987200000,
    "message": "SUCCESS: claim submitted successfully"
  },
  "FaucetCrypto:1": {
    "last_access_time": 1753987260000,
    "message": "SUCCESS: claim submitted successfully"
  }
}
```

- Keys: `"faucet_name:wallet_index"` (0-based wallet index)
- `last_access_time`: UNIX timestamp in **milliseconds**
- Cooldown: 24 hours (comparing current time against `last_access_time`)

If the file is missing or corrupted, FaucetFlow starts fresh (no cooldowns).

## File Overview

| File | Purpose |
|------|---------|
| `main.py` | Core automation script |
| `faucets.json` | Faucet site definitions (URLs, selectors) |
| `.env.example` | Configuration template |
| `.env` | Your configuration (never commit this!) |
| `last_access.json` | Cooldown state (auto-generated, never commit) |
| `faucetflow.log` | Detailed debug log (auto-generated) |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Files excluded from version control |

## Troubleshooting

### "No .env file found"
Copy `.env.example` to `.env` and edit it with your settings:
```bash
cp .env.example .env
```

### Browser doesn't launch
- Make sure Chrome or Edge is installed
- Check that `DRIVER_PATH` points to the correct WebDriver executable
- Match driver version to your browser version
- Check that `BROWSER_DATA_DIR` points to a valid user data directory
- Close all browser windows before running (profile lock conflict)

### "Wallet field not found"
Faucet sites change their HTML. Update the selectors in `faucets.json`:
1. Open the faucet site in your browser
2. Right-click the wallet address field → Inspect
3. Copy the `id` attribute or a unique CSS selector
4. Update `faucets.json` with the new selector

### Faucet site blocks automated browsers
Some sites detect automation. Try these mitigations:
- Set `HEADLESS=false` in `.env`
- Use a well-established browser profile (old profiles look more "human")
- FaucetFlow already sets anti-detection flags (`--disable-blink-features=AutomationControlled`, etc.)

## License

Internal tool — no license specified.
