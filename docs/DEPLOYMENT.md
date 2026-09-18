# Deployment — AWS Windows Server

This is the operational guide for running the platform on a Windows Server
instance. It covers the twelve things that have to be true for the site to be
reachable and safe, in the order they have to happen.

Everything here is written for a fresh server. Nothing in it changes the
strategy, the sessions or the scanner — it starts the application that already
exists and puts a name and a certificate in front of it.

**Nothing in this document is done for you.** Steps 8–12 in particular describe
DNS, TLS and the reverse proxy, none of which this repository can configure.
Until they are actually completed and verified, the site is **not live** — it is
reachable at best on a bare IP over plain HTTP, which is not a client-facing
deployment.

---

## 0. Before you start

You need:

| | |
|---|---|
| Server | Windows Server 2019/2022, any size that runs MT5 comfortably |
| Broker terminal | MetaTrader 5 installed **and logged in** on the same machine |
| Python | 3.11 or newer, on `PATH` |
| Git | to clone and to update |
| Domain | one you control, with access to its DNS records |
| Certificate | a TLS certificate for that domain (see step 9) |

The MT5 Python API is **single-owner per process**: it binds the whole process
to one terminal. That is why the scanner and the web app are separate processes
(below) and why the platform can never read a *client's* broker balance from the
same terminal. See `trading/broker_accounts.py`.

---

## 1. Lay the code down

```powershell
# Run PowerShell as Administrator.
New-Item -ItemType Directory -Force C:\apps | Out-Null
cd C:\apps
git clone https://github.com/Emetex180/AI_Trading_Bot.git ai-trading-bot
cd ai-trading-bot
```

Use a drive that is not the system drive if you have one.

## 2. Python environment

```powershell
cd C:\apps\ai-trading-bot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`requirements.txt` includes `tzdata`. That is not optional on Windows: it does
not ship a timezone database, and without one `America/New_York` cannot be
resolved, so the app would fall back to a fixed UTC offset and silently lose DST.
The New York clock on every page depends on this.

## 3. Configure `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

The values that matter for the web platform:

```ini
# --- Signing key ------------------------------------------------------------ #
# Required in production. Generate one:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
FLASK_SECRET_KEY=<paste the generated value>

FLASK_HOST=127.0.0.1          # binds loopback; the reverse proxy fronts it
FLASK_PORT=5000
SESSION_COOKIE_SECURE=true    # ONLY once HTTPS works (step 9)
TRUST_PROXY=true              # ONLY if a reverse proxy is in front
SESSION_LIFETIME_HOURS=12
SERVE_THREADS=8

LOGIN_MAX_ATTEMPTS=5
LOGIN_LOCKOUT_MINUTES=15
MIN_PASSWORD_LENGTH=12

# Leave blank; create the first admin from the CLI instead (step 4).
ADMIN_USERNAME=
ADMIN_PASSWORD=
ADMIN_EMAIL=

# Trading safety. MUST stay false unless you have deliberately decided
# otherwise — with it false the executor never calls order_send().
AUTO_TRADING=false
```

Then fill in the MT5 block (`MT5_TERMINAL_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`,
`MT5_SERVER`) and the Telegram block if you want notifications.

`.env` is the only file holding secrets. Do not commit it, do not copy it into
tickets, and do not put it anywhere the web server can serve.

## 4. Create the database and the first account

```powershell
python run.py smoke                 # imports, DB init, /health round-trip
python run.py user add --username <you> --role admin --display-name "Your Name"
```

`user add` prompts for the password twice rather than taking it as an argument,
because a command-line argument is visible in the process list and stays in the
PowerShell history file. There is no public sign-up: every account is created
here or by an admin on the `/admin/clients` page.

The other account commands:

```powershell
python run.py user list
python run.py user passwd  --username <name>
python run.py user disable --username <name>
python run.py user enable  --username <name>
```

The existing database is never reset by any of this. `create_all` only adds
tables that do not exist; if you are upgrading a server that already has
`data\trading.db`, it gains the new users and broker tables with its history
intact.

**Where the data lives.** Left alone, the app uses SQLite at `data\trading.db`,
holding every signal, backtest and audit line. That history is not reproducible —
the engine will not reconstruct a setup it recorded months ago — so it is the one
thing on this server worth backing up. `DATABASE_URL` in `.env` overrides the
location (blank means "use `data\trading.db`"); if you point it elsewhere, that
path is what you back up.

**Back it up before and after any upgrade:**

```powershell
Copy-Item data\trading.db "data\trading.db.$(Get-Date -Format yyyyMMdd-HHmm).bak"
```

Confirm the copy is real before you rely on it — a backup that silently copied
nothing is worse than none:

```powershell
Get-ChildItem data\*.bak | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name, Length
```

## 5. Verify the app before putting a proxy in front of it

```powershell
python run.py serve
```

Expect output like:

```
[serve] listening on 127.0.0.1:5000 with 8 threads
[serve] AUTO_TRADING is off: the platform is alert-only.
```

Check from the server:

```powershell
Invoke-WebRequest http://127.0.0.1:5000/health -UseBasicParsing | Select-Object -ExpandProperty Content
```

`/health` is the only unauthenticated endpoint and returns
`{"status": "ok", "live_running": false}`.

Then open `http://127.0.0.1:5000/` **on the server** and sign in with the admin
account from step 4. If `SESSION_COOKIE_SECURE=true` is already set, sign-in over
plain HTTP will not work — that is correct behaviour; set it back to `false`
until step 9 is done.

If Waitress is not installed, `serve` says so and points at
`pip install -r requirements.txt`.

## 6. Run the scanner and the web app together

These are two processes, and they must be. MT5's Python API binds the whole
process to one terminal, so running the scanner *inside* the web process would
mean a browser request could reach the terminal mid-poll.

| Process | Command | Purpose |
|---|---|---|
| Scanner | `python run.py scan` | the live engine; writes setups and alerts |
| Web | `python run.py serve` | the platform clients sign in to |

Both read the same `.env` and the same database. The web app does **not** scan;
without `scan` running, the dashboard honestly reports the scanner as stopped and
shows the last recorded setups rather than pretending to be live.

Start them from two PowerShell windows while you are testing. For a permanent
install, use the Task Scheduler script (step 7).

## 7. Keep them running (Task Scheduler)

```powershell
# As Administrator
cd C:\apps\ai-trading-bot
powershell -ExecutionPolicy Bypass -File deploy\install-service.ps1 -ProjectPath C:\apps\ai-trading-bot
```

That registers two scheduled tasks — `AITradingBot-Scanner` and
`AITradingBot-Web` — both set to start at boot, restart on failure, and run as a
named account with "Run whether user is logged on or not". The MT5 terminal
itself must be running and logged in before the scanner starts; the script
configures a delayed start for the scanner task to give the terminal time.

To remove them:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-service.ps1 -ProjectPath C:\apps\ai-trading-bot -Uninstall
```

Verify:

```powershell
Get-ScheduledTask -TaskName 'AITradingBot-*' | Get-ScheduledTaskInfo
```

## 8. Reverse proxy (IIS + ARR)

Bind the app to `127.0.0.1` and let IIS be the only thing listening on 80/443.
`deploy\iis-arr-notes.md` has the full walkthrough, including the web.config
rules and the `X-Forwarded-*` headers that `TRUST_PROXY=true` makes the app
honour.

The short version:

1. Install IIS, then ARR and URL Rewrite.
2. Enable proxy in ARR.
3. Create a site bound to `:80` on your hostname.
4. Add a reverse-proxy rule from `(.*)` to `http://127.0.0.1:5000/{R:1}`.
5. Set `TRUST_PROXY=true` in `.env` and restart the web task.

## 9. DNS and HTTPS

**None of this is done for you.**

### DNS

At your DNS provider, create an **A record** for the hostname pointing at the
instance's public (Elastic) IP:

```
Type  A
Name  trading            (or @ for the apex)
Value <the instance's Elastic IP>
TTL   300
```

Verify propagation **before** trying to get a certificate:

```powershell
Resolve-DnsName trading.example.com
```

The answer must be your server's address. A certificate authority will fail
validation until this is right, and "it works on my machine" usually means a
stale resolver cache, not a working record.

### Security group / firewall

Allow inbound **80** and **443** only. Do not open 5000 to the internet — the
app binds to loopback and the proxy is the only thing that should reach it.

```powershell
New-NetFirewallRule -DisplayName "HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
New-NetFirewallRule -DisplayName "HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

### Certificate

Use `win-acme` (recommended on Windows) to obtain and auto-renew a Let's
Encrypt certificate against the IIS site:

```powershell
# Download wacs.exe from https://www.win-acme.com/ first.
.\wacs.exe --target iis --host trading.example.com --installation iis
```

It creates the HTTPS binding, installs the renewal task and can add the
HTTP→HTTPS redirect. Certificates expire in 90 days; the task it schedules
renews them.

Then add an IIS rewrite rule that sends all HTTP traffic to HTTPS — an
unencrypted sign-in form would send the password in clear text.

### Turn on the Secure cookie

Only now:

```ini
SESSION_COOKIE_SECURE=true
```

Restart the web task:

```powershell
Stop-ScheduledTask  -TaskName 'AITradingBot-Web'
Start-ScheduledTask -TaskName 'AITradingBot-Web'
```

## 10. Verify the live site

Run all of these against the **real domain**, not localhost:

```powershell
# 1. DNS resolves to your server
Resolve-DnsName trading.example.com

# 2. HTTPS has a valid chain and the right name
curl.exe -vI https://trading.example.com/ 2>&1 | Select-String -Pattern 'subject|issuer|HTTP/'

# 3. Plain HTTP redirects to HTTPS
curl.exe -sI http://trading.example.com/ | Select-String -Pattern 'Location|HTTP/'

# 4. The liveness probe answers
curl.exe -s https://trading.example.com/health

# 5. An unauthenticated request to a protected page is refused, not served
curl.exe -sI https://trading.example.com/dashboard | Select-String -Pattern 'HTTP/|Location'

# 6. The cookie is Secure and HttpOnly
curl.exe -sI https://trading.example.com/login | Select-String -Pattern 'Set-Cookie'
```

Then, in a browser:

- Sign in as the client account → you land on `/dashboard`.
- While signed in as a **client**, open `/admin` → **403**. Not a redirect, not
  an empty page: a flat refusal.
- Open `/console` as a client → **403**.
- Sign in as the admin → `/console` and `/admin` both load.
- `/dashboard` shows the New York clock and the session window; both match the
  wall clock in New York.
- A client page's HTML contains no `MT5_`, no `TELEGRAM_`, no `LLM_`, no API key
  and no `.env` value.

Report the domain as live only after 1–6 pass.

## 11. Day-to-day operations

### Safe restart

The scanner writes to the database continuously. Stop it before the web app, and
start it after:

```powershell
Stop-ScheduledTask  -TaskName 'AITradingBot-Web'
Stop-ScheduledTask  -TaskName 'AITradingBot-Scanner'
# ... work ...
Start-ScheduledTask -TaskName 'AITradingBot-Scanner'
Start-ScheduledTask -TaskName 'AITradingBot-Web'
```

An in-flight setup is lost on a stop — the engine's intermediate state lives in
memory, not in the database. That is by design: only a confirmed, risk-approved
setup is persisted, so nothing half-formed is ever written.

### Logs

There is **no log file by default**. The application writes its own events to the
database, not to disk, and Task Scheduler discards a task's console output — so
do not go looking for a `.log` that was never created.

The record that does exist is in the database, and it is the one that matters:
scanner start/stop, every strategy decision, and every admin action are stored
with their New York timestamps and visible on `/admin/activity` and `/analysis`.
That survives a restart, which a console log would not.

To capture process output anyway — useful when the scanner dies at startup and
you need to see why — run a task's command by hand in the project directory,
where it prints to the console:

```powershell
cd C:\apps\ai-trading-bot
.\.venv\Scripts\python.exe run.py scan
```

For a durable file, redirect the scheduled task yourself in Task Scheduler
(*Actions* → edit the action → append `>> logs\scanner.log 2>&1`) after creating
the directory. Nothing rotates that file, so if you do this, schedule the
rotation too — an unbounded log will eventually fill the disk.

Task Scheduler also keeps its own history, which will tell you that a task
started, exited with a code, and restarted:

```powershell
Get-ScheduledTask -TaskName 'AITradingBot-*' | Get-ScheduledTaskInfo
```

### Updating from GitHub

```powershell
cd C:\apps\ai-trading-bot
Copy-Item data\trading.db "data\trading.db.$(Get-Date -Format yyyyMMdd-HHmm).bak"
Stop-ScheduledTask -TaskName 'AITradingBot-Web'
Stop-ScheduledTask -TaskName 'AITradingBot-Scanner'

git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m pytest tests/ -q          # all green before you restart
Start-ScheduledTask -TaskName 'AITradingBot-Scanner'
Start-ScheduledTask -TaskName 'AITradingBot-Web'
```

Never run `git pull` with the scanner running: the process holds the SQLite file
and an update that changes a model can leave a running process writing to a
schema it no longer matches.

### Adding clients

Sign in as an admin → `/admin/clients` → **Create an account**. Or:

```powershell
python run.py user add --username <name>
```

## 12. What is deliberately not enabled

- **`AUTO_TRADING` is `false`.** The scanner detects, records and notifies; it
  does not place orders. Turning it on is a deliberate act in `.env`, and the
  platform shows a red banner on every page when it is on.
- **No client broker balances.** The Accounts page shows *not connected* for
  every link. That is a statement about the platform, not about a client's
  money: no provider is registered, and MT5's single-terminal binding means a
  per-client read cannot share the scanner's terminal. A zero is never shown in
  place of an unread balance.
- **No public sign-up.** Accounts exist because an admin made them.
- **No fabricated analytics.** If the engine produced no value, the page shows
  an em dash or says the feed is empty. It never prints a zero, a placeholder or
  a written commentary the engine did not generate.
