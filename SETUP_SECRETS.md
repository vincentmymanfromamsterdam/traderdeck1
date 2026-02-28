# How to Add Your Carnivore Credentials to GitHub Secrets

Your password is stored **encrypted** in GitHub — not in code, not visible in the repo.
GitHub Actions decrypts it only at runtime in a secure environment.

---

## Step-by-Step

### 1. Go to your repository on GitHub

Navigate to: `https://github.com/YOUR_USERNAME/traderdeck`

### 2. Open Settings → Secrets

Click **Settings** (top tab) → **Secrets and variables** (left sidebar) → **Actions**

### 3. Add your Carnivore email

1. Click **New repository secret**
2. Name: `CARNIVORE_EMAIL`
3. Value: your Carnivore login email (e.g. `you@gmail.com`)
4. Click **Add secret**

### 4. Add your Carnivore password

1. Click **New repository secret** again
2. Name: `CARNIVORE_PASSWORD`
3. Value: your Carnivore password
4. Click **Add secret**

### 5. Test the scraper

1. Go to **Actions** tab → **Daily Market Data Refresh**
2. Click **Run workflow** → **Run workflow**
3. Watch the logs — you'll see the scraper log in and extract portfolio data
4. Once it completes, reload your dashboard

---

## Security Notes

- Secrets are **never** shown in logs (GitHub masks them with `***`)
- Secrets are **never** committed to your repo
- Only **you** can see/edit them (not even collaborators by default)
- If you ever change your Carnivore password, update the secret in the same place

---

## Troubleshooting

**"Could not find email input field"**
→ Carnivore may have changed their login page. Check the Actions log and share it — we can update the scraper selectors.

**Scraper logs in but finds 0 tables**
→ The dashboard might use a non-standard layout. Check `data/carnivore_debug_dump.txt` in your repo after the run — share it and we'll refine the scraper.

**Action shows green but portfolios are empty**
→ Check `data/carnivore_portfolios.json` in your repo to see what was captured.

---

## Testing Locally (optional)

```bash
# Install dependencies
pip install playwright
python -m playwright install chromium

# Run scraper with your credentials
CARNIVORE_EMAIL=you@email.com CARNIVORE_PASSWORD=yourpass python scrape_carnivore.py

# Check output
cat data/carnivore_portfolios.json
```
