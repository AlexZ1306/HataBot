# HataBot

HataBot is a small local utility for monitoring apartment rental search results and notifying you about genuinely new listings.

Current MVP:
- Avito search monitoring
- Cian search monitoring
- Telegram notifications
- SQLite remembered state
- silent first-run bootstrap
- Windows Task Scheduler friendly scripts
- provider/notifier architecture for future `cian`, `domclick`, `n1`

## What It Does

HataBot treats your saved search URLs as the source of truth, periodically checks the search results, remembers what it has already seen, suppresses repost duplicates by content fingerprint, and sends Telegram notifications only for new relevant listings.

## Quick Start

1. Create a virtual environment:

```powershell
python -m venv .venv
```

2. Install dependencies:

```powershell
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -e .[dev]
```

3. Create your env file:

```powershell
Copy-Item .env.example .env
```

4. Fill in `HATABOT_TELEGRAM_BOT_TOKEN` and `HATABOT_TELEGRAM_CHAT_ID` in `.env`.

5. Review `config/config.yaml`.

6. Verify configuration and Telegram access:

```powershell
.\.venv\Scripts\python -m hata_bot doctor
```

7. Send a test message:

```powershell
.\.venv\Scripts\python -m hata_bot test-telegram
```

8. Run one monitoring pass:

```powershell
.\scripts\run_once.ps1
```

The first successful run seeds the baseline silently and does not spam existing listings.

9. Optional: enable the interactive Telegram button listener:

```powershell
.\scripts\install_bot_listener_task.ps1
```

Then open your bot in Telegram and send `/start`.

## Daily Usage

- Run manually: `.\scripts\run_once.ps1`
- Install scheduled task every 10 minutes: `.\scripts\install_task.ps1`
- Run a specific source: `.\.venv\Scripts\python -m hata_bot run --source avito_nsk_family`
- Run a specific source: `.\.venv\Scripts\python -m hata_bot run --source cian_nsk_family`
- Run Telegram button listener now: `.\scripts\run_bot_listener.ps1`
- Install Telegram button listener at logon: `.\scripts\install_bot_listener_task.ps1`

## Project Layout

- `src/hata_bot/` - app code
- `config/config.yaml` - active configuration
- `config/config.example.yaml` - example configuration
- `scripts/` - Windows helper scripts
- `tests/` - unit and smoke tests

## Notes

- The current Avito implementation reads the search listing page only and does not open each listing separately.
- The current Cian implementation uses a real local Chrome/Edge window via DevTools Protocol because direct HTTP access is blocked by Cian WAF. The window starts minimized and off-screen.
- Cian can be filtered further in code using `required_districts` and `exclude_text_patterns` from `config/config.yaml`.
- If Avito returns a suspicious response such as CAPTCHA, too few cards, or broken HTML, HataBot refuses to overwrite state.
- If Cian returns WAF/VPN blocks or too few usable cards, HataBot refuses to overwrite state.
- Notifications are sent one by one in Telegram.
- The interactive Telegram bot starts with a source picker and then gives source-specific buttons: `Проверить сейчас`, `Последнее объявление`, `Последние 3 объявления`, and `Меню`.
