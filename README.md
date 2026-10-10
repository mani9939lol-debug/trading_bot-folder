# Trading Bot (paper trading, 5-strategy voting)

Simulated trades with fake money using public price data. No exchange account
or exchange API key is needed.

## Files
- `schema.sql`    database tables (run once in Supabase)
- `strategies.py` the five strategies that vote
- `bot.py`        voting, simulated trades, stop-loss, loss limits, panic
- `main.py`       web server that keeps the bot running
- `requirements.txt`

## Setup

1. **Database.** Supabase -> SQL Editor -> New query -> paste all of
   `schema.sql` -> Run.
2. **Secret key.** Supabase -> Project Settings -> API Keys -> copy the
   **secret** key (starts with `sb_secret_`). Keep it private: only paste it
   into Render, never into GitHub or Lovable.
3. **GitHub.** Create a new repository, then "Add file -> Upload files" and
   upload everything in this folder.
4. **Render.** New + -> Web Service -> pick the repository.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Instance type: Free
   - Environment variables:
     - `SUPABASE_URL` = your Project URL
     - `SUPABASE_SECRET_KEY` = the secret key from step 2
5. **Keep it awake.** Free Render servers sleep after about 15 minutes without
   visitors, which would stop the bot. Create a free UptimeRobot monitor that
   pings `https://YOUR-APP.onrender.com/health` every 5 minutes.
6. **Check it works.** In Supabase -> Table Editor -> `bot_settings`, the
   `last_heartbeat` value should update every few seconds.
7. **Start trading (fake money).** In `bot_settings`, change `status` from
   `paused` to `running`. The Lovable dashboard will do this with a button.

## Safety behaviour
- Starts **paused**. Nothing trades until you set `running`.
- One open position per coin, sized as a % of fake equity.
- Every position gets a stop-loss and take-profit.
- Daily loss limit and max drawdown limit pause the bot automatically.
- Panic: set `close_all_requested` to true. The bot closes everything and
  pauses within about 15 seconds.
- Resuming after a pause resets the loss baselines.

## Lock down your database
After creating your own dashboard login, turn off public sign-ups:
Supabase -> Authentication -> Sign In / Providers -> disable "Allow new users
to sign up". Otherwise anyone who finds your dashboard could register and see
your data.
