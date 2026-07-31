# FaucetFlow

**Automated cryptocurrency faucet claimer** — drives a real browser (Chrome or Edge)
via Selenium to visit configured faucet sites, fill in wallet addresses, submit claims,
and log results.

## Features

- 🔄 **Real browser automation** — uses your actual Chrome/Edge profile so saved
  logins, wallet extensions (MetaMask, etc.), and session cookies are available.
- ⏱️ **Cooldown enforcement** — tracks 24-hour (configurable) cooldowns per
  faucet/wallet pair in a local JSON state file.
- 🛡️ **Resilient** — every Selenium operation is wrapped in try/except; faucet
  sites are flaky and the script handles that gracefully.
- 📋 **Dry-run mode** — preview what would be claimed without opening a browser.
- 🎯 **Targeted runs** — process a single faucet with `--faucet NAME`.
- 📝 **Full logging** — logs to both console and `faucetflow.log`.

## Prerequisites

- **Python 3.9+**
- **Google Chrome** or **Microsoft Edge** installed
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

# 4. Configure your environment
cp .env.example .env
# Edit .env with your wallet addresses and browser settings

# 5. Run!
python main.py
```

## Configuration (.env)

Copy `.env.example` to `.env` and edit:

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLETS` | *(required)* | Comma-separated `COIN=ADDRESS` pairs. E.g.: `BTC=bc1q...,ETH=0x...,DOGE=D...` |
| `BROWSER` | `chrome` | Which browser: `chrome` or `edge` |
| `BROWSER_PROFILE_PATH` | *(empty)* | Path to browser user data directory (see below) |
| `BROWSER_PROFILE_DIRECTORY` | `Default` | Profile folder name within the user data dir |
| `HEADLESS` | `false` | Run without a visible window? (`true`/`false`) |
| `COOLDOWN_HOURS` | `24` | Hours between claims for the same faucet+wallet |
| `PAGE_LOAD_TIMEOUT` | `30` | Seconds to wait for page loads |
| `ELEMENT_TIMEOUT` | `10` | Seconds to wait for elements to appear |

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

Faucets are defined in `faucets.json` as a JSON array. Each entry:

```json
{
  "name": "FreeBitcoin",
  "url": "https://freebitco.in",
  "wallet_field": { "type": "css", "value": "#btcAddress" },
  "submit_button": { "type": "css", "value": "#free_play_form_button" },
  "checkboxes": [
    { "type": "css", "value": "input[type='checkbox']" }
  ],
  "popup_selectors": [
    { "type": "css", "value": ".close-button" },
    { "type": "xpath", "value": "//button[contains(text(),'Close')]" }
  ],
  "pre_submit_wait": 2,
  "post_submit_wait": 5,
  "success_indicators": ["success", "claimed", "congratulations"]
}
```

### Selector Types

| Type | Example |
|------|---------|
| `css` | `"#btcAddress"`, `"input[name='address']"`, `".claim-btn"` |
| `xpath` | `"//button[contains(text(),'Claim')]"` |
| `id` | `"btcAddress"` (without the `#`) |

### Optional Fields

- `checkboxes` — list of checkbox selectors to tick before submitting
- `popup_selectors` — selectors for popup/dialog close buttons (tried in order)
- `pre_submit_wait` — seconds to wait after filling the form (default: 2)
- `post_submit_wait` — seconds to wait after clicking submit (default: 3)
- `success_indicators` — text patterns to look for on the page after claiming

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
2. Load faucet definitions from faucets.json
3. Load cooldown state from last_access.json
4. For each faucet:
   a. Check if any wallet is past cooldown → skip if not
   b. Launch Chrome/Edge with your profile
   c. Navigate to faucet URL
   d. Dismiss any popups
   e. For each eligible wallet:
      - Fill wallet address field
      - Tick required checkboxes
      - Click submit button
      - Check for success indicators
      - Log result, update cooldown state
5. Print summary of all claims
```

## State File

`last_access.json` tracks the last claim time per faucet+wallet pair.
It's automatically created on first run and updated after each claim.

```json
{
  "FreeBitcoin|bc1q...": "2026-07-31T12:00:00",
  "FaucetCrypto|0xabc...": "2026-07-31T13:30:00"
}
```

If the file is missing or corrupted, FaucetFlow starts fresh (no cooldowns).

## Troubleshooting

### "No .env file found"
Copy `.env.example` to `.env` and edit it with your settings:
```bash
cp .env.example .env
```

### Browser doesn't launch
- Make sure Chrome or Edge is installed
- Check that `BROWSER_PROFILE_PATH` points to a valid user data directory
- Close all browser windows before running (profile lock conflict)

### "Wallet field not found"
Faucet sites change their HTML. Update the selectors in `faucets.json`:
1. Open the faucet site in your browser
2. Right-click the wallet address field → Inspect
3. Copy the `id` attribute or a unique CSS selector
4. Update `faucets.json` with the new selector

### "Submit button not found"
Same as above — the button's ID/class may have changed. Inspect and update.

### Faucet site blocks automated browsers
Some sites detect automation. Try these mitigations:
- Set `HEADLESS=false` in `.env`
- Use a well-established browser profile (old profiles look more "human")
- Add a longer `pre_submit_wait` in the faucet definition

### WebDriver errors
The script uses `webdriver-manager` to auto-download the correct ChromeDriver/EdgeDriver.
If you're behind a proxy, set `HTTPS_PROXY` / `HTTP_PROXY` environment variables.

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

## License

Internal tool — no license specified.
