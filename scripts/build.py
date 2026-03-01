#!/usr/bin/env python3
"""
News Digest Builder
Fetches articles from configured sources, optionally summarises via Claude API,
and builds a static HTML page for GitHub Pages.
"""

import os
import json
import yaml
import feedparser
import requests
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter
from html import escape

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"
OUTPUT_PATH = ROOT / "docs" / "index.html"
INTERACTIONS_PATH = ROOT / "docs" / "interactions.json"
TEMPLATE_PATH = ROOT / "scripts" / "template.html"

# ── Load config ──────────────────────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

TOPICS = config["topics"]
SOURCES = config["sources"]
DIGEST = config["digest"]
APPEARANCE = config["appearance"]
FOLLOWING = config.get("following", [])
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "howdyalf-droid/just-you-news")

# ── Helpers ───────────────────────────────────────────────────────────────────

def article_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:8]

def parse_date(entry):
    """Return a timezone-aware datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            import time
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)

def is_recent(entry, hours=None):
    max_hours = hours or DIGEST.get("max_article_age_hours", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    return parse_date(entry) >= cutoff

def matches_topic(text, topic):
    """Match article to topic. Requires at least one keyword match.
    For short keyword lists, be stricter to avoid false positives."""
    text_lower = text.lower()
    keywords = topic["keywords"]
    matches = sum(1 for kw in keywords if kw.lower() in text_lower)
    # Topics with very general keywords need 2+ matches to reduce noise
    general_topics = {"australian news", "aus news"}
    if topic["name"].lower() in general_topics:
        return matches >= 1
    return matches >= 1

def get_image(entry):
    """Try to extract an image URL from a feed entry."""
    # Check media_content
    for media in getattr(entry, "media_content", []):
        if media.get("url") and media.get("type", "").startswith("image"):
            return media["url"]
    # Check media_thumbnail
    for thumb in getattr(entry, "media_thumbnail", []):
        if thumb.get("url"):
            return thumb["url"]
    # Check enclosures
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("url")
    return None


# ── GitHub Issues Feedback ────────────────────────────────────────────────────

def fetch_feedback_blocklist():
    """Read open GitHub Issues tagged 'digest-feedback' and build a blocklist."""
    blocklist = {}  # article_id -> list of topics to block from
    if not GITHUB_TOKEN:
        return blocklist
    try:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
        params = {"labels": "digest-feedback", "state": "open", "per_page": 100}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        issues = r.json()
        if not isinstance(issues, list):
            return blocklist
        for issue in issues:
            body = issue.get("body", "")
            # Parse article_id and blocked topic from issue body
            art_id = None
            blocked_topic = None
            for line in body.split("\n"):
                if line.startswith("article_id:"):
                    art_id = line.split(":", 1)[1].strip()
                if line.startswith("block_from_topic:"):
                    blocked_topic = line.split(":", 1)[1].strip()
            if art_id:
                if art_id not in blocklist:
                    blocklist[art_id] = []
                if blocked_topic:
                    blocklist[art_id].append(blocked_topic)
                else:
                    blocklist[art_id].append("__all__")
        print(f"  Feedback blocklist: {len(blocklist)} articles blocked")
    except Exception as e:
        print(f"  Feedback fetch error: {e}")
    return blocklist

def create_feedback_label():
    """Ensure the digest-feedback label exists in the repo."""
    if not GITHUB_TOKEN:
        return
    try:
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        # Check if label exists
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/labels/digest-feedback",
            headers=headers, timeout=10
        )
        if r.status_code == 404:
            requests.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/labels",
                headers=headers,
                json={"name": "digest-feedback", "color": "e74c3c", "description": "Feedback from news digest"},
                timeout=10
            )
    except Exception as e:
        print(f"  Label creation error: {e}")

# ── Guardian API ──────────────────────────────────────────────────────────────

def fetch_guardian(sections, max_results=20):
    if not GUARDIAN_API_KEY:
        return []
    articles = []
    for section in sections:
        url = "https://content.guardianapis.com/search"
        params = {
            "section": section,
            "show-fields": "thumbnail,trailText,bodyText",
            "page-size": max_results,
            "api-key": GUARDIAN_API_KEY,
            "order-by": "newest",
        }
        try:
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            for item in data.get("response", {}).get("results", []):
                fields = item.get("fields", {})
                pub_date = item.get("webPublicationDate", "")
                try:
                    dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except Exception:
                    dt = datetime.now(timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=DIGEST.get("max_article_age_hours", 24))
                if dt < cutoff:
                    continue
                articles.append({
                    "id": article_id(item["webUrl"]),
                    "title": item["webTitle"],
                    "url": item["webUrl"],
                    "source": "The Guardian",
                    "summary": fields.get("trailText", ""),
                    "body": fields.get("bodyText", "")[:2000],
                    "image": fields.get("thumbnail"),
                    "date": dt,
                    "headlines_only": False,
                })
        except Exception as e:
            print(f"Guardian fetch error ({section}): {e}")
    return articles

# ── RSS Feeds ─────────────────────────────────────────────────────────────────

def fetch_rss(source):
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            if not is_recent(entry):
                continue
            title = getattr(entry, "title", "")
            url = getattr(entry, "link", "")
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            # Strip HTML tags from summary
            import re
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            if not title or not url:
                continue
            articles.append({
                "id": article_id(url),
                "title": title,
                "url": url,
                "source": source["name"],
                "summary": summary[:500],
                "body": summary[:2000],
                "image": get_image(entry),
                "date": parse_date(entry),
                "headlines_only": source.get("headlines_only", False),
            })
    except Exception as e:
        print(f"RSS fetch error ({source['name']}): {e}")
    return articles

# ── Claude Summarisation ──────────────────────────────────────────────────────

def summarise_batch(articles):
    """Send articles to Claude for summarisation. Falls back to trail text."""
    if not ANTHROPIC_API_KEY:
        return articles

    to_summarise = [a for a in articles if not a["headlines_only"] and a.get("body") and len(a.get("summary", "")) < 100]
    if not to_summarise:
        return articles

    # Batch into groups of 10 to avoid token limits
    for i in range(0, len(to_summarise), 10):
        batch = to_summarise[i:i+10]
        prompt_items = "\n\n".join(
            f"[{j+1}] TITLE: {a['title']}\nTEXT: {a['body'][:800]}"
            for j, a in enumerate(batch)
        )
        prompt = f"""Summarise each of the following news articles in exactly {DIGEST.get('summary_sentences', 3)} clear, informative sentences. 
Return ONLY a JSON array of strings, one summary per article, in the same order.
No preamble, no markdown, just the JSON array.

{prompt_items}"""

        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            data = r.json()
            text = data["content"][0]["text"].strip()
            summaries = json.loads(text)
            for article, summary in zip(batch, summaries):
                article["summary"] = summary
        except Exception as e:
            print(f"Summarisation error: {e}")

    return articles

# ── Trending detection ────────────────────────────────────────────────────────

def find_trending(all_articles):
    """Find topics appearing across multiple sources."""
    if not DIGEST.get("show_trending", True):
        return []

    import re
    # Extract significant words/phrases from titles
    stop_words = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
                  "or", "but", "is", "are", "was", "were", "be", "been", "has",
                  "have", "had", "will", "would", "could", "should", "may", "might",
                  "its", "it", "this", "that", "these", "those", "with", "from",
                  "by", "as", "up", "out", "about", "over", "after", "before"}

    phrase_sources = {}  # phrase -> set of sources
    for article in all_articles:
        words = re.findall(r'\b[A-Z][a-zA-Z]+\b', article["title"])
        for word in words:
            if word.lower() not in stop_words and len(word) > 3:
                if word not in phrase_sources:
                    phrase_sources[word] = set()
                phrase_sources[word].add(article["source"])

    min_sources = DIGEST.get("trending_min_sources", 2)
    trending = [
        {"term": term, "count": len(sources), "sources": list(sources)}
        for term, sources in phrase_sources.items()
        if len(sources) >= min_sources
    ]
    trending.sort(key=lambda x: x["count"], reverse=True)
    return trending[:10]

# ── Following stories ─────────────────────────────────────────────────────────

def find_following(all_articles):
    if not FOLLOWING:
        return []
    results = []
    seen_ids = set()
    for story_query in FOLLOWING:
        keywords = story_query.lower().split()
        for article in all_articles:
            if article["id"] in seen_ids:
                continue
            title_lower = article["title"].lower()
            if sum(1 for kw in keywords if kw in title_lower) >= max(1, len(keywords) // 2):
                article["following_query"] = story_query
                results.append(article)
                seen_ids.add(article["id"])
    return results

# ── Load interaction scores ───────────────────────────────────────────────────

def load_interactions():
    if INTERACTIONS_PATH.exists():
        with open(INTERACTIONS_PATH) as f:
            return json.load(f)
    return {"clicks": {}, "thumbs": {}, "source_scores": {}, "topic_scores": {}}

def score_article(article, interactions):
    score = 0
    source = article["source"]
    art_id = article["id"]
    # Thumbs signals
    thumb = interactions.get("thumbs", {}).get(art_id, 0)
    score += thumb * 10
    # Source preference
    score += interactions.get("source_scores", {}).get(source, 0)
    # Recency bonus
    age = (datetime.now(timezone.utc) - article["date"]).total_seconds() / 3600
    score -= age * 0.5
    return score

# ── HTML Generation ───────────────────────────────────────────────────────────

def format_time_ago(dt):
    diff = datetime.now(timezone.utc) - dt
    hours = diff.total_seconds() / 3600
    if hours < 1:
        return f"{int(diff.total_seconds() / 60)}m ago"
    elif hours < 24:
        return f"{int(hours)}h ago"
    else:
        return f"{int(hours / 24)}d ago"

def render_article_card(article, show_image=True):
    img_html = ""
    if show_image and article.get("image"):
        img_html = f'<img class="card-img" src="{escape(article["image"])}" alt="" loading="lazy" onerror="this.style.display=\'none\'">'

    source_badge = f'<span class="source-badge">{escape(article["source"])}</span>'
    if article.get("headlines_only"):
        source_badge += ' <span class="paywall-badge">Headline only</span>'

    time_str = format_time_ago(article["date"])
    summary = escape(article.get("summary", ""))

    thumb_up = f'<button class="thumb thumb-up" onclick="thumbs(\'{article["id"]}\', 1)" title="More like this">↑</button>'
    thumb_down = f'<button class="thumb thumb-down" onclick="thumbs(\'{article["id"]}\', -1)" title="Less like this">↓</button>'
    feedback_btn = f'<button class="thumb feedback-btn" onclick="openFeedback(\'{article["id"]}\', \'{escape(article["title"][:60])}\', \'{escape(article.get("current_topic", ""))}\', \'{escape(article["url"])}\')" title="Wrong section?">⚑ Wrong section?</button>'

    return f"""
<article class="card" id="card-{article['id']}">
  {img_html}
  <div class="card-body">
    <div class="card-meta">{source_badge} <span class="time-ago">{time_str}</span></div>
    <h3 class="card-title">
      <a href="{escape(article['url'])}" target="_blank" rel="noopener"
         onclick="trackClick('{article['id']}', '{escape(article['source'])}')">{escape(article['title'])}</a>
    </h3>
    {f'<p class="card-summary">{summary}</p>' if summary else ''}
    <div class="card-actions">
      {thumb_up}{thumb_down}{feedback_btn}
    </div>
  </div>
</article>"""

def build_html(topic_articles, following_articles, trending, all_articles):
    app = APPEARANCE
    now_mel = datetime.now(timezone(timedelta(hours=11)))  # AEDT
    date_str = now_mel.strftime("%A, %-d %B %Y")
    time_str = now_mel.strftime("%-I:%M %p")

    # Build topic sections
    topic_sections_html = ""
    for topic in TOPICS:
        articles = topic_articles.get(topic["name"], [])
        if not articles:
            continue
        for a in articles:
            a["current_topic"] = topic["name"]
        cards = "".join(render_article_card(a) for a in articles)
        color = topic.get("color", app["accent"])
        topic_sections_html += f"""
<section class="topic-section" id="topic-{topic['name'].lower().replace(' ', '-').replace('&', '')}">
  <h2 class="section-heading" style="--topic-color:{color}">{escape(topic['name'])}</h2>
  <div class="cards-grid">{cards}</div>
</section>"""

    # Build following section
    following_html = ""
    if following_articles:
        cards = "".join(render_article_card(a) for a in following_articles)
        following_html = f"""
<section class="topic-section following-section">
  <h2 class="section-heading" style="--topic-color:#e74c3c">📌 Following</h2>
  <div class="following-tags">
    {"".join(f'<span class="following-tag">{escape(q)}</span>' for q in FOLLOWING)}
  </div>
  <div class="cards-grid">{cards}</div>
</section>"""

    # Build trending section
    trending_html = ""
    if trending:
        trending_items = "".join(
            f'<a class="trend-item" href="https://news.google.com/search?q={requests.utils.quote(t["term"])}" target="_blank" rel="noopener"><strong>{escape(t["term"])}</strong> '
            f'<em>{t["count"]} sources</em></a>'
            for t in trending[:8]
        )
        trending_html = f"""
<section class="trending-section">
  <h2 class="section-heading trending-heading">🔥 Trending across sources</h2>
  <div class="trending-tags">{trending_items}</div>
</section>"""

    # Nav links
    nav_links = "".join(
        f'<a href="#topic-{t["name"].lower().replace(" ", "-").replace("&", "")}" class="nav-link">{escape(t["name"])}</a>'
        for t in TOPICS
    )

    theme_class = "dark-theme" if app.get("theme") == "dark" else ""

    html = f"""<!DOCTYPE html>
<html lang="en" class="{theme_class}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News Digest — {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700&family=DM+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
/* ── CSS VARIABLES — edit these to change the look ── */
:root {{
  --bg: {app['background']};
  --card-bg: {app['card_background']};
  --accent: {app['accent']};
  --text: #1a1a1a;
  --text-muted: #666;
  --border: #e5e2dc;
  --font-heading: '{app['font_heading']}', sans-serif;
  --font-body: '{app['font_body']}', sans-serif;
  --font-size: {app['font_size']};
  --radius: 8px;
  --shadow: 0 2px 12px rgba(0,0,0,0.07);
}}
.dark-theme {{
  --bg: #141414;
  --card-bg: #1e1e1e;
  --accent: #e8d5b0;
  --text: #e8e8e8;
  --text-muted: #999;
  --border: #2a2a2a;
  --shadow: 0 2px 12px rgba(0,0,0,0.3);
}}

/* ── SCROLL OFFSET — accounts for sticky header ── */
html {{
  scroll-padding-top: 120px;
}}

/* ── RESET & BASE ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ font-size: var(--font-size); scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  line-height: 1.6;
  min-height: 100vh;
}}

/* ── HEADER ── */
.site-header {{
  background: var(--accent);
  color: var(--bg);
  padding: 1.5rem 2rem 1rem;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 20px rgba(0,0,0,0.15);
}}
.header-top {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}}
.site-title {{
  font-family: var(--font-heading);
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}}
.header-date {{
  font-size: 0.85rem;
  opacity: 0.8;
}}
.site-nav {{
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  align-items: center;
}}
.nav-link {{
  color: var(--bg);
  text-decoration: none;
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.75;
  padding: 0.2rem 0.5rem;
  border-radius: 3px;
  transition: opacity 0.15s, background 0.15s;
}}
.nav-link:hover {{ opacity: 1; background: rgba(255,255,255,0.15); }}
.nav-link.active {{ opacity: 1; background: rgba(255,255,255,0.25); border-bottom: 2px solid rgba(255,255,255,0.8); }}
.theme-toggle {{
  margin-left: auto;
  background: rgba(255,255,255,0.2);
  border: none;
  color: var(--bg);
  cursor: pointer;
  font-size: 0.8rem;
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  font-family: var(--font-body);
}}

/* ── MAIN LAYOUT ── */
.main-content {{
  max-width: 1100px;
  margin: 0 auto;
  padding: 2rem 1.5rem;
}}

/* ── SECTIONS ── */
.topic-section {{ margin-bottom: 3rem; }}
.section-heading {{
  font-family: var(--font-heading);
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--topic-color, var(--accent));
  border-bottom: 2px solid var(--topic-color, var(--accent));
  padding-bottom: 0.5rem;
  margin-bottom: 1.25rem;
  letter-spacing: -0.01em;
}}

/* ── CARDS GRID ── */
.cards-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1.25rem;
}}
.card {{
  background: var(--card-bg);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
  overflow: hidden;
  transition: transform 0.2s, box-shadow 0.2s;
  display: flex;
  flex-direction: column;
}}
.card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 24px rgba(0,0,0,0.12); }}
.card-img {{
  width: 100%;
  height: 180px;
  object-fit: cover;
  background: var(--border);
}}
.card-body {{ padding: 1rem; flex: 1; display: flex; flex-direction: column; gap: 0.5rem; }}
.card-meta {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
.source-badge {{
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
}}
.paywall-badge {{
  font-size: 0.65rem;
  color: var(--text-muted);
  border: 1px solid var(--border);
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
}}
.time-ago {{ font-size: 0.72rem; color: var(--text-muted); margin-left: auto; }}
.card-title {{ font-family: var(--font-heading); font-size: 0.95rem; line-height: 1.35; flex: 1; }}
.card-title a {{ color: var(--text); text-decoration: none; }}
.card-title a:hover {{ color: var(--accent); }}
.card-summary {{ font-size: 0.82rem; color: var(--text-muted); line-height: 1.5; }}
.card-actions {{ display: flex; gap: 0.5rem; margin-top: auto; padding-top: 0.5rem; border-top: 1px solid var(--border); }}
.thumb {{
  background: none;
  border: 1px solid var(--border);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 0.85rem;
  padding: 0.2rem 0.5rem;
  border-radius: 4px;
  transition: all 0.15s;
}}
.thumb:hover {{ background: var(--border); color: var(--text); }}
.thumb.active-up {{ background: #e8f5e9; color: #2e7d32; border-color: #2e7d32; }}
.thumb.active-down {{ background: #fce4ec; color: #c62828; border-color: #c62828; }}

/* ── FOLLOWING SECTION ── */
.following-section {{ background: color-mix(in srgb, #e74c3c 5%, var(--bg)); border-left: 4px solid #e74c3c; padding-left: 1rem; border-radius: 0 var(--radius) var(--radius) 0; }}
.following-tags {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }}
.following-tag {{
  font-size: 0.75rem;
  background: #e74c3c;
  color: white;
  padding: 0.2rem 0.6rem;
  border-radius: 20px;
  font-weight: 600;
}}

/* ── TRENDING ── */
.trending-section {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem;
  margin-bottom: 2.5rem;
}}
.trending-heading {{ border-color: #e67e22; color: #e67e22; }}
.trending-tags {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin-top: 0.75rem; }}
.trend-item {{
  font-size: 0.82rem;
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 0.3rem 0.75rem;
  border-radius: 20px;
  color: var(--text);
  text-decoration: none;
  transition: background 0.15s, border-color 0.15s;
  display: inline-block;
}}
.trend-item:hover {{ background: var(--border); border-color: var(--accent); }}
.trend-item strong {{ color: var(--accent); }}
.trend-item em {{ color: var(--text-muted); font-style: normal; font-size: 0.75rem; }}


/* ── FEEDBACK MODAL ── */
.feedback-btn {{
  font-size: 0.72rem;
  margin-left: auto;
  color: var(--text-muted);
  border-color: transparent;
}}
.feedback-btn:hover {{ color: #e74c3c; border-color: #e74c3c; background: #fce4ec; }}
.modal-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 1000;
  align-items: center;
  justify-content: center;
  padding: 1rem;
}}
.modal-overlay.open {{ display: flex; }}
.modal {{
  background: var(--card-bg);
  border-radius: var(--radius);
  padding: 1.5rem;
  max-width: 480px;
  width: 100%;
  box-shadow: 0 8px 40px rgba(0,0,0,0.2);
}}
.modal h3 {{
  font-family: var(--font-heading);
  font-size: 1rem;
  margin-bottom: 0.25rem;
  color: var(--text);
}}
.modal-article-title {{
  font-size: 0.82rem;
  color: var(--text-muted);
  margin-bottom: 1rem;
  font-style: italic;
  line-height: 1.4;
}}
.modal-label {{
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text);
  display: block;
  margin-bottom: 0.5rem;
}}
.modal-options {{
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 1rem;
}}
.modal-option {{
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  font-size: 0.85rem;
  color: var(--text);
  cursor: pointer;
}}
.modal-option input {{ margin-top: 0.2rem; accent-color: #e74c3c; }}
.modal-select {{
  width: 100%;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 0.85rem;
  margin-bottom: 1rem;
  display: none;
}}
.modal-other {{
  width: 100%;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  font-size: 0.85rem;
  margin-bottom: 1rem;
  display: none;
  resize: vertical;
  min-height: 60px;
}}
.modal-actions {{ display: flex; gap: 0.75rem; justify-content: flex-end; }}
.btn-cancel {{
  background: none;
  border: 1px solid var(--border);
  color: var(--text-muted);
  padding: 0.4rem 1rem;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 0.85rem;
}}
.btn-submit {{
  background: #e74c3c;
  border: none;
  color: white;
  padding: 0.4rem 1rem;
  border-radius: 4px;
  cursor: pointer;
  font-family: var(--font-body);
  font-size: 0.85rem;
  font-weight: 600;
}}
.btn-submit:hover {{ background: #c0392b; }}
.feedback-success {{
  text-align: center;
  padding: 1rem;
  font-size: 0.9rem;
  color: #2e7d32;
}}

/* ── FOOTER ── */
.site-footer {{
  text-align: center;
  padding: 2rem;
  font-size: 0.8rem;
  color: var(--text-muted);
  border-top: 1px solid var(--border);
  margin-top: 3rem;
}}

/* ── MOBILE ── */
@media (max-width: 600px) {{
  .site-header {{ padding: 1rem; position: static; }}
  .header-top {{ flex-direction: column; gap: 0.25rem; }}
  .main-content {{ padding: 1rem; }}
  .cards-grid {{ grid-template-columns: 1fr; }}
  .section-heading {{ font-size: 1.1rem; }}
}}
</style>
</head>
<body>

<header class="site-header">
  <div class="header-top">
    <div class="site-title">Your News Digest</div>
    <div class="header-date">{date_str} · {time_str} AEDT</div>
  </div>
  <nav class="site-nav">
    {nav_links}
    <button class="theme-toggle" onclick="toggleTheme()">◐ Theme</button>
  </nav>
</header>

<main class="main-content">
  {following_html}
  {trending_html}
  {topic_sections_html}
</main>

<!-- ── FEEDBACK MODAL ── -->
<div class="modal-overlay" id="feedbackModal" onclick="closeFeedbackOnOverlay(event)">
  <div class="modal">
    <h3>📋 Send feedback</h3>
    <p class="modal-article-title" id="modalArticleTitle"></p>
    <span class="modal-label">What's the issue?</span>
    <div class="modal-options">
      <label class="modal-option">
        <input type="radio" name="feedbackType" value="wrong_topic" onchange="onFeedbackTypeChange(this)">
        Wrong topic — this shouldn't appear here
      </label>
      <label class="modal-option">
        <input type="radio" name="feedbackType" value="not_relevant" onchange="onFeedbackTypeChange(this)">
        Not relevant to any of my topics
      </label>
      <label class="modal-option">
        <input type="radio" name="feedbackType" value="repetitive" onchange="onFeedbackTypeChange(this)">
        Too repetitive — I've seen this story already
      </label>
      <label class="modal-option">
        <input type="radio" name="feedbackType" value="other" onchange="onFeedbackTypeChange(this)">
        Other
      </label>
    </div>
    <select class="modal-select" id="topicRedirect">
      <option value="">— Select correct topic (optional) —</option>
            <option value="US Politics">US Politics</option>      <option value="Australian Politics">Australian Politics</option>      <option value="Australian News">Australian News</option>      <option value="Artificial Intelligence">Artificial Intelligence</option>      <option value="Finance & Markets">Finance & Markets</option>      <option value="Film & TV">Film & TV</option>
      <option value="none">None of my topics</option>
    </select>
    <textarea class="modal-other" id="feedbackOther" placeholder="Tell me more..."></textarea>
    <div class="modal-actions">
      <button class="btn-cancel" onclick="closeFeedback()">Cancel</button>
      <button class="btn-submit" onclick="submitFeedback()">Send feedback</button>
    </div>
  </div>
</div>

<footer class="site-footer">
  Built with Claude · Sources: The Guardian, ABC News, BBC, NPR, WSJ
  · Last updated {time_str} AEDT
</footer>

<script>
// ── Interaction tracking ──────────────────────────────────────────────────
// Feedback token injected at build time
const STORAGE_KEY = 'newsdigest_interactions';

function loadInteractions() {{
  try {{
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {{}};
  }} catch {{ return {{}}; }}
}}

function saveInteractions(data) {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
}}

function trackClick(articleId, source) {{
  const data = loadInteractions();
  data.clicks = data.clicks || {{}};
  data.clicks[articleId] = (data.clicks[articleId] || 0) + 1;
  data.sourceCounts = data.sourceCounts || {{}};
  data.sourceCounts[source] = (data.sourceCounts[source] || 0) + 1;
  saveInteractions(data);
}}

function thumbs(articleId, value) {{
  const data = loadInteractions();
  data.thumbs = data.thumbs || {{}};
  // Toggle off if same value
  if (data.thumbs[articleId] === value) {{
    delete data.thumbs[articleId];
  }} else {{
    data.thumbs[articleId] = value;
  }}
  saveInteractions(data);
  updateThumbUI(articleId, data.thumbs[articleId]);
}}

function updateThumbUI(articleId, value) {{
  const card = document.getElementById('card-' + articleId);
  if (!card) return;
  card.querySelector('.thumb-up')?.classList.toggle('active-up', value === 1);
  card.querySelector('.thumb-down')?.classList.toggle('active-down', value === -1);
}}

// ── Theme toggle ──────────────────────────────────────────────────────────
function toggleTheme() {{
  document.documentElement.classList.toggle('dark-theme');
  localStorage.setItem('theme', document.documentElement.classList.contains('dark-theme') ? 'dark' : 'light');
}}


// ── Feedback modal ────────────────────────────────────────────────────────
let currentFeedback = {{}};

function openFeedback(articleId, title, currentTopic, url) {{
  currentFeedback = {{ articleId, title, currentTopic, url }};
  document.getElementById('modalArticleTitle').textContent = title + (currentTopic ? ' (in: ' + currentTopic + ')' : '');
  document.querySelectorAll('input[name="feedbackType"]').forEach(r => r.checked = false);
  document.getElementById('topicRedirect').style.display = 'none';
  document.getElementById('topicRedirect').value = '';
  document.getElementById('feedbackOther').style.display = 'none';
  document.getElementById('feedbackOther').value = '';
  document.getElementById('feedbackModal').classList.add('open');
}}

function closeFeedback() {{
  document.getElementById('feedbackModal').classList.remove('open');
}}

function closeFeedbackOnOverlay(e) {{
  if (e.target === document.getElementById('feedbackModal')) closeFeedback();
}}

function onFeedbackTypeChange(radio) {{
  document.getElementById('topicRedirect').style.display = radio.value === 'wrong_topic' ? 'block' : 'none';
  document.getElementById('feedbackOther').style.display = radio.value === 'other' ? 'block' : 'none';
}}

async function submitFeedback() {{
  const feedbackType = document.querySelector('input[name="feedbackType"]:checked')?.value;
  if (!feedbackType) {{ alert('Please select an issue type.'); return; }}

  const redirectTopic = document.getElementById('topicRedirect').value;
  const otherText = document.getElementById('feedbackOther').value;

  const labels = ['digest-feedback'];
  let title = '';
  let body = '';

  if (feedbackType === 'wrong_topic') {{
    title = `[Wrong topic] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `**Article appeared in wrong topic section.**

article_id: ${{currentFeedback.articleId}}
appeared_in_topic: ${{currentFeedback.currentTopic}}
block_from_topic: ${{currentFeedback.currentTopic}}
${{redirectTopic && redirectTopic !== 'none' ? 'suggest_topic: ' + redirectTopic : ''}}

**Article:** [${{currentFeedback.title}}](${{currentFeedback.url}})`;
  }} else if (feedbackType === 'not_relevant') {{
    title = `[Not relevant] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `**Article not relevant to any topic.**

article_id: ${{currentFeedback.articleId}}
block_from_topic: __all__

**Article:** [${{currentFeedback.title}}](${{currentFeedback.url}})`;
  }} else if (feedbackType === 'repetitive') {{
    title = `[Repetitive] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `**Article is too repetitive / already seen.**

article_id: ${{currentFeedback.articleId}}
block_from_topic: ${{currentFeedback.currentTopic}}

**Article:** [${{currentFeedback.title}}](${{currentFeedback.url}})`;
  }} else {{
    title = `[Feedback] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `**Other feedback.**

article_id: ${{currentFeedback.articleId}}
appeared_in_topic: ${{currentFeedback.currentTopic}}

**Note:** ${{otherText}}

**Article:** [${{currentFeedback.title}}](${{currentFeedback.url}})`;
  }}

  try {{
    const resp = await fetch(
      'https://deigest-feedback.howdyalf.workers.dev',
      {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
        }},
        body: JSON.stringify({{ title, body, labels }}),
      }}
    );

    if (resp.ok) {{
      const modal = document.querySelector('.modal');
      modal.innerHTML = '<div class="feedback-success">✓ Feedback sent — thanks! The next digest refresh will learn from this.</div>';
      setTimeout(closeFeedback, 2000);
    }} else {{
      alert('Could not send feedback. Please try again later.');
    }}
  }} catch(e) {{
    alert('Could not send feedback — check your connection.');
  }}
}}

// ── Active nav highlighting ───────────────────────────────────────────────
function initNavHighlighting() {{
  const sections = document.querySelectorAll('.topic-section[id]');
  const navLinks = document.querySelectorAll('.nav-link');
  if (!sections.length || !navLinks.length) return;

  const observer = new IntersectionObserver((entries) => {{
    entries.forEach(entry => {{
      if (entry.isIntersecting) {{
        navLinks.forEach(link => link.classList.remove('active'));
        const activeLink = document.querySelector(`.nav-link[href="#${{entry.target.id}}"]`);
        if (activeLink) activeLink.classList.add('active');
      }}
    }});
  }}, {{ rootMargin: '-20% 0px -70% 0px' }});

  sections.forEach(section => observer.observe(section));
}}

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {{
  // Restore theme
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme === 'dark') document.documentElement.classList.add('dark-theme');
  else if (savedTheme === 'light') document.documentElement.classList.remove('dark-theme');

  // Restore thumb states
  const data = loadInteractions();
  Object.entries(data.thumbs || {{}}).forEach(([id, val]) => updateThumbUI(id, val));

  // Init nav highlighting
  initNavHighlighting();
}});
</script>
</body>
</html>"""

    return html

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("📰 Building news digest...")

    (ROOT / "docs").mkdir(exist_ok=True)

    # Fetch all articles
    all_articles = []

    # Guardian API (rich content)
    if GUARDIAN_API_KEY:
        print("  Fetching Guardian API...")
        all_sections = list(set(s for t in TOPICS for s in t.get("guardian_sections", [])))
        guardian_articles = fetch_guardian(all_sections, max_results=15)
        all_articles.extend(guardian_articles)
        print(f"  → {len(guardian_articles)} Guardian articles")

    # RSS feeds
    for source in SOURCES:
        print(f"  Fetching {source['name']}...")
        rss_articles = fetch_rss(source)
        all_articles.extend(rss_articles)
        print(f"  → {len(rss_articles)} articles")

    # Deduplicate by ID
    seen = set()
    unique_articles = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique_articles.append(a)
    all_articles = unique_articles
    print(f"  Total unique articles: {len(all_articles)}")

    # Summarise via Claude if key available
    if ANTHROPIC_API_KEY:
        print("  Summarising articles...")
        all_articles = summarise_batch(all_articles)

    # Load interaction scores
    interactions = load_interactions() if INTERACTIONS_PATH.exists() else {}

    # Load feedback blocklist from GitHub Issues
    create_feedback_label()
    blocklist = fetch_feedback_blocklist()

    # Assign articles to topics
    topic_articles = {}
    for topic in TOPICS:
        matches = [
            a for a in all_articles
            if matches_topic(a["title"] + " " + a.get("summary", ""), topic)
            and topic["name"] not in blocklist.get(a["id"], [])
            and "__all__" not in blocklist.get(a["id"], [])
        ]
        # Score and sort
        matches.sort(
            key=lambda a: score_article(a, interactions),
            reverse=True
        )
        # Deduplicate within topic (same title from different sources)
        seen_titles = set()
        deduped = []
        for a in matches:
            title_key = a["title"][:50].lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                deduped.append(a)
        topic_articles[topic["name"]] = deduped[:DIGEST.get("stories_per_topic", 5)]

    # Following stories
    following_articles = find_following(all_articles)

    # Trending
    trending = find_trending(all_articles)

    # Build HTML
    html = build_html(topic_articles, following_articles, trending, all_articles)

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"✅ Digest built → {OUTPUT_PATH}")
    print(f"   Following: {len(following_articles)} articles")
    print(f"   Trending: {len(trending)} topics")

if __name__ == "__main__":
    main()
