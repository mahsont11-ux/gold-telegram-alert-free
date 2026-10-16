Gold Telegram Alert Free v1.0
================================
Rule-based notifier (NOT GPT, NOT AI model, NOT autopilot).

INPUT: Yahoo Finance delayed COMEX GC=F futures proxy (NOT XAUUSD/Alpari).
SCAN: closed 15-minute bars with H1/H4 trend context.
OUTPUT: Telegram BUY/SELL alerts with GC=F futures price levels, not broker order levels.
DEPLOYMENT: public GitHub Actions workflow, scheduled every ~15m, best effort.

SETUP:
1) In Telegram open @BotFather and /newbot. Send /start to your new bot.
2) Create a public GitHub repo. Upload bot.py and state.json.
3) Create .github/workflows/gold-alert.yml with the supplied YAML.
4) GitHub repo Settings > Secrets and variables > Actions: add TELEGRAM_BOT_TOKEN.
5) Actions > Gold Telegram Alerts > Run workflow > mode=setup.
   Your bot sends CHAT ID PRIVATELY to your Telegram chat, not public GitHub logs.
6) Add the numeric ID as secret TELEGRAM_CHAT_ID.
7) Run mode=test, then status. Scheduled scan sends only new confirmed BUY/SELL.

NO guaranteed 24/7 delivery; Yahoo Finance endpoints can be delayed/restricted; GitHub
schedule runs may be delayed or dropped. Public repo workflow scheduling can stop after
60d inactivity. No real money or execution is handled. Inspect broker prices independently.

Offline tests: python -m unittest discover -s tests -v
