# Setup guide (Windows, beginner-friendly)

Do these once, in order. About 30–40 minutes. Steps marked 🔑 produce a secret: put it in `.env`, never in chat.

---

## 1. Install the tools
1. **Python 3.11 or newer**: https://www.python.org/downloads/ → run the installer →
   **tick "Add python.exe to PATH"** on the first screen → Install.
2. **VS Code**: https://code.visualstudio.com/
3. In VS Code: Extensions (Ctrl+Shift+X) → install **Python** (Microsoft) and **Claude Code** (Anthropic).
   Sign in to Claude Code with your Claude account when it asks.
4. Check it worked. Open a terminal in VS Code (Ctrl+`) and run:
   ```powershell
   python --version
   ```
   You should see `Python 3.11.x` or higher.

## 2. Open the project
1. Unzip `sprinto-outbound.zip` somewhere simple, e.g. `C:\Users\<you>\Projects\sprinto-outbound`.
2. VS Code → File → Open Folder → choose that folder.

## 3. Create the virtual environment (a private box for this project's packages)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
- If PowerShell says *"running scripts is disabled"*, run this once, then activate again:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (answer `Y`).
- You'll see `(.venv)` at the start of the prompt when it's active. Activate it every time you open a new terminal.

## 4. Create your `.env`
```powershell
Copy-Item .env.example .env
```
Open `.env` in VS Code and fill in `OWNER_EMAIL` (your Gmail). Fill the keys below as you get them.

## 5. 🔑 Gemini API key
1. Go to https://aistudio.google.com/apikey → **Create API key**.
2. Paste it into `.env` as `GEMINI_API_KEY=...`
3. Later, once Claude Code has built `scripts\list_models.py`, run it and copy a current *flash* model name into `GEMINI_MODEL=`.
   - Free-tier note: Google may use free-tier prompts to improve its products. That's fine for public website data; don't put anything private in prompts.

## 6. 🔑 Hunter.io API key
1. Sign up at https://hunter.io (free plan).
2. Go to https://hunter.io/api-keys → copy your key → `.env` as `HUNTER_API_KEY=...`
3. You get **25 searches a month**. The pipeline caches results and only searches for qualified companies, so you won't burn them.

## 7. 🔑 Gmail API (OAuth). The longest step; go slowly.
1. Go to https://console.cloud.google.com/ and sign in with **the same Gmail as `OWNER_EMAIL`**.
2. Top bar → project dropdown → **New Project** → name `sprinto-outbound` → Create → make sure it's selected.
3. Search bar → **Gmail API** → **Enable**.
4. Left menu → **Google Auth Platform** (older UI calls it **OAuth consent screen**) → **Get started**:
   - App name: `sprinto-outbound` · Support email: your Gmail
   - Audience: **External**
   - Contact email: your Gmail → Create.
5. **Audience** tab → **Test users** → **Add users** → add your own Gmail. *(Skipping this causes "access blocked".)*
6. **Clients** tab (older UI: Credentials → Create credentials → OAuth client ID) → **Create client**:
   - Application type: **Desktop app** · Name: `outbound-desktop` → Create.
   - **Download JSON** → rename the file to `credentials.json` → put it in the project root folder.
7. Later, once Claude Code has built it, run `python scripts\gmail_auth.py`. A browser opens:
   - You'll see "Google hasn't verified this app". That's expected for your own test app. Click **Continue**.
   - Approve the **send email** permission only. A `token.json` file appears in the folder.

## 8. Check everything
```powershell
python scripts\check_setup.py
```
It prints ✅/❌ for each key, the remaining Hunter searches, and whether Gmail is authorised, without printing any secret.

## 9. 🔑 Vercel + Neon (the public website)
1. https://vercel.com/new → sign in with GitHub → import **`sprinto-outbound`** → **Deploy** (defaults are fine).
2. In the Vercel project: **Storage → Create Database → Neon** → follow the steps → **Connect** to this project.
   This adds `DATABASE_URL` automatically. (Optional: copy the same value into your local `.env` so the CLI
   and the website share results.)
3. **Settings → Environment Variables**, add (same values as your `.env`):
   `GEMINI_API_KEY`, `GEMINI_MODEL`, `HUNTER_API_KEY`, `OWNER_EMAIL`, `SENDER_NAME`, `SENDER_TITLE`.
4. For sending from the website (after step 7 above): open `token.json` in VS Code and copy
   `client_id` → `GMAIL_CLIENT_ID`, `client_secret` → `GMAIL_CLIENT_SECRET`, `refresh_token` → `GMAIL_REFRESH_TOKEN`.
   Add `SEND_ENABLED` = `true` only when you're ready.
5. **Deployments → ⋯ → Redeploy** (variables only apply after a redeploy).
6. Open `https://<your-project>.vercel.app/api/health`: every value should be `ok` / `configured`.

## Common problems
| Symptom | Fix |
|---|---|
| `python` not recognised | Reinstall Python with "Add to PATH" ticked, then restart VS Code |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Gemini `429` / quota | Wait a minute; free tier has per-minute limits. The code retries automatically |
| Gmail `access_denied` / "app blocked" | Add yourself as a Test user (step 7.5) |
| Gmail `invalid_grant` | Delete `token.json` and run `scripts\gmail_auth.py` again |
| Hunter `pagination_error` | `limit` must be ≤ 10 on free plan (already enforced in code) |
