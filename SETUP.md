# News Digest — Setup Guide
## Plain-English instructions for howdyalf-droid

---

## What you'll end up with

A webpage at `https://howdyalf-droid.github.io/newsdigest` that you can set as your browser homepage. It refreshes automatically at 7am and 1pm AEDT every day with real news from The Guardian, ABC News, BBC, NPR, and WSJ headlines. No ads. No algorithm you didn't build.

Total setup time: about 20 minutes.

---

## Step 1 — Get your free Guardian API key

1. Go to https://open-platform.theguardian.com/access/
2. Click **Register for developer access**
3. Fill in the form — use your email, name, reason can just say "personal news digest"
4. They'll email you a key immediately. It looks like: `abc12345-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
5. Keep this somewhere safe — you'll paste it into GitHub in Step 3

---

## Step 2 — Create the GitHub repository

1. Go to https://github.com and log in as howdyalf-droid
2. Click the **+** button (top right) → **New repository**
3. Name it: `newsdigest`
4. Make sure it's set to **Public** (required for free GitHub Pages)
5. Check the box for **Add a README file**
6. Click **Create repository**

---

## Step 3 — Upload the project files

You have a folder called `newsdigest` with these files inside:
```
newsdigest/
  config.yaml
  requirements.txt
  scripts/
    build.py
  .github/
    workflows/
      refresh.yml
  docs/
    index.html
```

Upload them to GitHub:

1. In your new `newsdigest` repository, click **Add file** → **Upload files**
2. Drag the entire `newsdigest` folder contents into the upload area
3. Scroll down, click **Commit changes**

> **Note:** GitHub's web uploader can be fiddly with nested folders. If it doesn't work well, let me know and I'll walk you through using GitHub Desktop (a simple app) instead — it's a 2-minute install.

---

## Step 4 — Add your Guardian API key securely

Your API key is stored as a "secret" in GitHub — it never appears in your code.

1. In your `newsdigest` repository, click **Settings** (tab at the top)
2. In the left sidebar, click **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `GUARDIAN_API_KEY`
5. Value: paste your Guardian API key from Step 1
6. Click **Add secret**

That's it — the key is now stored securely and will be used automatically every time the digest refreshes.

---

## Step 5 — Turn on GitHub Pages

This makes your digest accessible as a real webpage.

1. In your repository, click **Settings**
2. In the left sidebar, click **Pages**
3. Under **Source**, select **Deploy from a branch**
4. Branch: select **main**, folder: select **/docs**
5. Click **Save**
6. Wait 2–3 minutes, then your site will be live at:
   `https://howdyalf-droid.github.io/newsdigest`

---

## Step 6 — Test your first build

Let's make sure everything works before waiting for the 7am schedule.

1. In your repository, click the **Actions** tab
2. Click **Refresh News Digest** in the left sidebar
3. Click **Run workflow** → **Run workflow** (the green button)
4. Watch it run — it takes about 1–2 minutes
5. When it shows a green ✓, go to your digest URL and refresh

If it shows a red ✗, click on it and I can help you diagnose the issue.

---

## Step 7 — Set as your browser homepage

**In Vivaldi:**
1. Settings → Appearance → Startup
2. Set **Homepage** to `https://howdyalf-droid.github.io/newsdigest`
3. Set **Start with** to "Homepage"

**On iPhone Safari:**
There's no true homepage setting in Safari, but you can:
- Bookmark the page and put it as the first bookmark in your favourites bar
- Or add it to your Home Screen (Share → Add to Home Screen) for one-tap access

---

## Ongoing maintenance — the only two files you'll ever touch

### `config.yaml` — changing topics, sources, appearance

Edit this file directly in GitHub:
1. Go to your repository → click `config.yaml`
2. Click the pencil icon (Edit)
3. Make your changes
4. Click **Commit changes**

The next scheduled refresh will pick up your changes automatically.

**Examples:**

*Add BBC Technology as a source:*
```yaml
sources:
  - name: "BBC Technology"
    url: "https://feeds.bbci.co.uk/news/technology/rss.xml"
    country: "UK"
```

*Track an ongoing story:*
```yaml
following:
  - "Erin Patterson mushroom trial"
  - "federal election 2025"
```

*Switch to dark theme:*
```yaml
appearance:
  theme: "dark"
```

*Change accent colour to something warmer:*
```yaml
appearance:
  accent: "#8B4513"
```

---

## Adding your Overcast podcasts (when you're ready)

1. Open Overcast on your iPhone
2. Go to Settings → Export OPML
3. This downloads a file listing all your podcast feeds
4. Send the file to yourself and upload it to your GitHub repository
5. Let me know and I'll update the build script to include a podcast section

---

## Troubleshooting

**The page isn't updating**
→ Check the Actions tab for any red ✗ failures. Click on the failed run to see the error message, then send it to me.

**A topic has no articles**
→ The keywords may need tuning. Open `config.yaml`, find the topic, and add more keyword variations.

**A source has disappeared**
→ RSS feeds occasionally change URLs. Check the source's website for their current feed URL.

**I want to add my Anthropic API key for better summaries**
→ Add a second secret in Step 4 named `ANTHROPIC_API_KEY` with your key from console.anthropic.com. The script will automatically start using Claude for summarisation.

---

## Quick reference card

| What you want to do | Where to do it |
|---|---|
| Add/remove topics | Edit `config.yaml` → topics section |
| Add a new news source | Edit `config.yaml` → sources section |
| Track an ongoing story | Edit `config.yaml` → following section |
| Change colours/fonts | Edit `config.yaml` → appearance section |
| Change stories per topic | Edit `config.yaml` → digest.stories_per_topic |
| Manually trigger a refresh | GitHub → Actions → Run workflow |
| Change refresh times | Edit `.github/workflows/refresh.yml` → cron lines |

---

*Questions or something not working? Paste the error message and I'll fix it.*
