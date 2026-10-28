# Bershka stock tracker

A small Python watcher for Bershka Türkiye. It checks selected colours and sizes, remembers the last observation, and can send a Telegram message when stock returns or a price drops.

I originally ran this on a Linux server. Preparing the public version turned up two problems: removed products came back as error objects inside an HTTP 200 response, and some current products had their sizes nested under `bundleProductSummaries`. Both cases are handled here.

## Run a check

Python 3.11 or newer:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python bershka.py --config config.example.json --dry-run --no-notify
```

The example points to a public product. Product IDs and availability change; replace it with an item you want to track. In a product URL such as `c0p244572190.html?colorId=800`, the product ID is `244572190` and the colour ID is `800`.

For your own list, copy `config.example.json` to `config.local.json` and edit it. An empty `sizes` or `colors` list includes every size or colour returned by the API.

```sh
python bershka.py                 # check once and save the observation
python bershka.py --watch         # repeat at the configured interval
python bershka.py --dry-run       # no messages and no state changes
```

The default interval is ten minutes. `notify_on` accepts `restock`, `price_drop`, `sold_out` and `price_rise`; the example enables the first two.

## Telegram

Credentials come from environment variables. They do not belong in the product configuration or source code.

PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN = "<your bot token>"
$env:TELEGRAM_CHAT_ID = "<your chat id>"
python bershka.py --watch
```

Bash:

```sh
export TELEGRAM_BOT_TOKEN='<your bot token>'
export TELEGRAM_CHAT_ID='<your chat id>'
python bershka.py --watch
```

Without those variables, changes are printed to the terminal. `--no-notify` disables Telegram even when the variables are present. Tests never contact Telegram.

## State and failure handling

- The first check establishes a baseline. It does not announce every available size.
- Prices stay in integer minor units until display. A missing price cannot become a zero-price alert.
- Removed products and empty size selections leave their last known state intact.
- Network failures and HTTP 429/5xx responses get bounded retries. A failed check is reported as a failure, not as “out of stock”.
- State is replaced atomically. If Telegram delivery fails, the previous state is kept so the next check can retry. A partial multi-message delivery can produce duplicates on retry.

Local configuration, state and logs are ignored by Git. The original server credentials and shopping history are not part of this repository.

## Tests

```sh
python -m unittest -v
```

The tests cover product error objects, nested bundles, size filters, change detection, state preservation, notification failures and dry runs. They run offline. A live stock query is a separate manual check; the undocumented storefront endpoint can change.

This project is independent of Bershka. It only reads product data and sends optional notifications; it does not place orders.
