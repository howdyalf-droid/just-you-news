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
    Respects exclude_keywords — any match there disqualifies the article."""
    text_lower = text.lower()
    keywords = topic["keywords"]

    # Check exclusions first
    exclude = topic.get("exclude_keywords", [])
    if any(ex.lower() in text_lower for ex in exclude):
        return False

    matches = sum(1 for kw in keywords if kw.lower() in text_lower)
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
    """Read open GitHub Issues tagged 'digest-feedback'.
    Returns (blocklist, redirects, examples):
      blocklist: article_id -> [topics to block from]
      redirects: article_id -> suggested correct topic
      examples: list of {title, wrong_topic, correct_topic} for prompt injection
    """
    blocklist = {}
    redirects = {}
    examples = []
    if not GITHUB_TOKEN:
        return blocklist, redirects, examples
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
            print(f"  Feedback API returned unexpected format: {type(issues)}")
            # Try fetching without label filter as fallback
            params2 = {"state": "open", "per_page": 100}
            r2 = requests.get(url, headers=headers, params=params2, timeout=10)
            issues = r2.json()
            if not isinstance(issues, list):
                return blocklist, redirects, examples
        print(f"  Issues fetched: {len(issues)}")
        for issue in issues:
            # Only process digest-feedback issues
            issue_labels = [l.get("name", "") for l in issue.get("labels", [])]
            if issue_labels and "digest-feedback" not in issue_labels:
                continue
            body = issue.get("body", "")
            art_id = None
            blocked_topic = None
            suggest_topic = None
            # Handle both newline-separated and space-separated formats
            import re as _re
            for field in ["article_id", "block_from_topic", "suggest_topic", "suggested_topic", "appeared_in_topic"]:
                pattern = field + r":\s*([^\s\n]+)"
                match = _re.search(pattern, body)
                if match:
                    val = match.group(1).strip()
                    if field == "article_id":
                        art_id = val
                    elif field == "block_from_topic":
                        blocked_topic = val
                    elif field in ("suggest_topic", "suggested_topic"):
                        suggest_topic = val
            # Extract article title from issue body for examples
            title_match = _re.search(r'Article:[ ]*(.+?)(?:\n|$)', body)
            article_title = title_match.group(1).strip() if title_match else None
            # Clean markdown bold markers
            if article_title:
                article_title = article_title.replace("**", "").replace("__", "").strip()

            if art_id:
                if art_id not in blocklist:
                    blocklist[art_id] = []
                if blocked_topic:
                    blocklist[art_id].append(blocked_topic)
                else:
                    blocklist[art_id].append("__all__")
                if suggest_topic and suggest_topic not in ("none", ""):
                    redirects[art_id] = suggest_topic

                # Build example for prompt injection
                if article_title and blocked_topic and blocked_topic != "__all__":
                    example = {
                        "title": article_title,
                        "wrong_topic": blocked_topic,
                        "correct_topic": suggest_topic if suggest_topic and suggest_topic not in ("none", "") else "None"
                    }
                    # Avoid duplicate examples for same title
                    if not any(e["title"] == article_title for e in examples):
                        examples.append(example)

        print(f"  Feedback: {len(blocklist)} blocked, {len(redirects)} redirected, {len(examples)} examples")
        if blocklist:
            print(f"  Blocked IDs: {list(blocklist.keys())[:5]}")
        if redirects:
            print(f"  Redirects: {dict(list(redirects.items())[:3])}")
    except Exception as e:
        print(f"  Feedback fetch error: {e}")
    return blocklist, redirects, examples

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

def classify_and_summarise(articles, feedback_examples=None):
    """Use Claude to classify articles into topics AND summarise them.
    Returns (classified dict or None, updated articles list)."""
    if not ANTHROPIC_API_KEY:
        return None, articles

    # Build feedback examples section for prompt
    if feedback_examples:
        lines = []
        for ex in feedback_examples[:20]:
            if ex["correct_topic"] and ex["correct_topic"] != "None":
                lines.append('  - "' + ex["title"] + '" is NOT ' + ex["wrong_topic"] + ', it belongs in ' + ex["correct_topic"])
            else:
                lines.append('  - "' + ex["title"] + '" does NOT belong in ' + ex["wrong_topic"])
        examples_text = "\n".join(lines) if lines else "(none yet)"
    else:
        examples_text = "(none yet)"

    topic_descriptions = "\n".join(
        '- "' + t["name"] + '": ' + ", ".join(t["keywords"][:6])
        for t in TOPICS
    )

    summary_sentences = DIGEST.get("summary_sentences", 2)

    # Process in batches of 10
    classified = {}
    for t in TOPICS:
        classified[t["name"]] = []

    api_headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    for i in range(0, len(articles), 10):
        batch = articles[i:i+10]
        prompt_items = "\n\n".join(
            "[" + str(j+1) + "] TITLE: " + a["title"] + "\nSOURCE: " + a.get("source","?") + "\nDESCRIPTION: " + a.get("summary", a.get("body",""))[:400]
            for j, a in enumerate(batch)
        )

        prompt = (
            "You are classifying news articles for a personal news digest for a reader in Melbourne, Australia.\n\n"
            "Available topics:\n" + topic_descriptions + "\n\n"
            "USER FEEDBACK — past corrections to learn from:\n" + examples_text + "\n\n"
            "CLASSIFICATION RULES:\n"
            "1. BREAKING NEWS: Active wars, major attacks, mass casualty events, significant crises happening NOW. The Iran conflict belongs here.\n"
            "2. INTERNATIONAL NEWS: Geopolitics, diplomacy, foreign policy. War analysis/background after the initial breaking story.\n"
            "3. US POLITICS: Trump, Congress, White House, US government, US elections, DOGE.\n"
            "4. AUSTRALIAN POLITICS: Federal/state Australian government, ALP, Liberal, Greens, parliament, Albanese, Dutton.\n"
            "5. AUSTRALIAN NEWS: Australian domestic stories not covered by politics above.\n"
            "6. MELBOURNE & VICTORIA: Stories specifically about Melbourne or Victoria.\n"
            "7. TECHNOLOGY: Tech products, companies, software, hardware, cybersecurity, social media.\n"
            "8. ARTIFICIAL INTELLIGENCE: AI models, LLMs, OpenAI, Anthropic, Google DeepMind, AI research, AI regulation.\n"
            "9. FINANCE & STOCKS: Stock markets, ASX, S&P500, earnings, interest rates, RBA, Fed, banking, commodities.\n"
            "10. PERSONAL FINANCE: Mortgages, superannuation, cost of living, household budgets, Australian tax. Australian context only.\n"
            "11. ENVIRONMENT & CLIMATE: Climate science, emissions policy, conservation, renewable energy, wildlife, extreme weather.\n"
            "12. FILM & TV: Movies, TV shows, streaming, Oscars, Emmys, box office, reviews, casting. Oscar season stories go here.\n"
            "13. MUSIC: Albums, artists, concerts, tours, festivals, Grammy, ARIA, music industry.\n"
            "14. ARTS & CULTURE: Art exhibitions, theatre, books, dance, opera, architecture. Include major international events.\n"
            "\nKEY RULES:\n"
            "- Assign None ONLY for sport scores, reality TV gossip, or purely local news from non-English-speaking countries with no broader relevance.\n"
            "- Do NOT overuse International News. If a story fits Technology, Finance, Environment, Film, Music or Arts, use that specific topic.\n"
            "- WSJ or The Australian opinion/editorial pieces = None.\n"
            "- US-specific personal finance (401k, social security, US tax) = None.\n"
            "- Each article gets exactly ONE topic.\n\n"
            "For each article return a " + str(summary_sentences) + "-sentence summary and the best topic.\n\n"
            "Return ONLY a JSON array, one object per article, in order:\n"
            '[{"topic": "Topic Name", "summary": "Summary text."}, ...]\n\n'
            "No preamble, no markdown fences, just the JSON array.\n\n"
            "Articles:\n" + prompt_items
        )

        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=api_headers,
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=45,
            )
            data = r.json()
            text = data["content"][0]["text"].strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            if text.endswith("```"):
                text = text[:-3].strip()
            results = json.loads(text)
            if isinstance(results, dict):
                results = [results]
            for article, result in zip(batch, results):
                if not isinstance(result, dict):
                    continue
                topic = result.get("topic", "None")
                summary = result.get("summary", "")
                if summary:
                    article["summary"] = summary
                if topic in classified:
                    classified[topic].append(article)
            print("  Classified batch " + str(i//10 + 1) + ": " + str(len(batch)) + " articles")
        except Exception as e:
            print("  Classification error (batch " + str(i//10 + 1) + "): " + str(e))
            import traceback
            print("  Traceback: " + traceback.format_exc())
            continue

    total_classified = sum(len(v) for v in classified.values())
    for t, arts in classified.items():
        if arts:
            print("    " + t + ": " + str(len(arts)) + " articles")
    if total_classified == 0:
        print("  Classification produced no results - falling back to keywords")
        return None, articles
    return classified, articles


# ── Trending detection ────────────────────────────────────────────────────────

def stem(word):
    """Simple suffix stemming to merge Israel/Israeli, attack/attacks etc."""
    w = word.lower()
    for suffix in ("ian", "ians", "ese", "ing", "ings", "tion", "tions",
                   "ed", "er", "ers", "ly", "ies", "ied", "'s", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[:-len(suffix)]
    return w

def find_trending(all_articles):
    """Find multi-word phrases appearing across multiple sources."""
    if not DIGEST.get("show_trending", True):
        return []

    import re
    stop_words = {
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "and",
        "or", "but", "is", "are", "was", "were", "be", "been", "has",
        "have", "had", "will", "would", "could", "should", "may", "might",
        "its", "it", "this", "that", "these", "those", "with", "from",
        "by", "as", "up", "out", "about", "over", "after", "before",
        "what", "when", "where", "who", "why", "how", "which", "than",
        "into", "says", "said", "also", "just", "more", "new", "first",
        "last", "one", "two", "year", "years", "time", "amid", "after",
        "while", "within", "under", "back", "still", "now", "here",
    }

    # Extract capitalised words (proper nouns / names) from each title
    # Group by stem to merge variants like Israel/Israeli/Israelis
    stem_to_display = {}   # stem -> best display form (most common)
    stem_sources = {}      # stem -> set of sources
    stem_counts = {}       # stem -> count of occurrences

    for article in all_articles:
        title = article["title"]
        # Extract runs of capitalised words as potential named entities
        # e.g. "Donald Trump" or "Gaza Strip" or just "Israel"
        phrases = re.findall(r'\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\b', title)
        seen_stems_this_article = set()
        for phrase in phrases:
            words = phrase.split()
            # Skip single short words and stop words
            if len(words) == 1:
                if len(phrase) <= 3 or phrase.lower() in stop_words:
                    continue
            # Use first word's stem as the key for grouping
            key = stem(words[0])
            if key in stop_words:
                continue
            if key not in seen_stems_this_article:
                seen_stems_this_article.add(key)
                if key not in stem_sources:
                    stem_sources[key] = set()
                    stem_counts[key] = {}
                stem_sources[key].add(article["source"])
                # Track which display form appears most
                stem_counts[key][phrase] = stem_counts[key].get(phrase, 0) + 1

    # Pick best display form for each stem (most frequent, prefer multi-word)
    for key in stem_counts:
        best = max(stem_counts[key], key=lambda p: (len(p.split()), stem_counts[key][p]))
        stem_to_display[key] = best

    min_sources = DIGEST.get("trending_min_sources", 2)
    trending = [
        {"term": stem_to_display[k], "count": len(stem_sources[k])}
        for k in stem_sources
        if len(stem_sources[k]) >= min_sources
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
    return {
        "clicks": {},
        "thumbs": {},
        "source_scores": {},
        "topic_scores": {},
        "keyword_scores": {},
        "read_count": 0,
    }

def extract_keywords(title):
    """Extract significant words from a title for interest tracking."""
    import re
    stop = {"the","a","an","in","on","at","to","for","of","and","or","but",
            "is","are","was","were","be","been","has","have","had","will",
            "with","from","by","as","up","out","over","after","its","says",
            "new","how","why","what","who","after","about","into"}
    words = re.findall(r"\b[a-zA-Z]{4,}\b", title.lower())
    return [w for w in words if w not in stop]

def score_article(article, interactions):
    score = 0.0
    source = article["source"]
    art_id = article["id"]

    # Explicit thumbs signals (strongest signal)
    thumb = interactions.get("thumbs", {}).get(art_id, 0)
    score += thumb * 15

    # Click history — articles from sources you click get a boost
    source_scores = interactions.get("source_scores", {})
    score += source_scores.get(source, 0) * 2

    # Keyword interest — words appearing in articles you've clicked get boosted
    keyword_scores = interactions.get("keyword_scores", {})
    if keyword_scores:
        keywords = extract_keywords(article["title"])
        keyword_boost = sum(keyword_scores.get(kw, 0) for kw in keywords)
        # Normalise — cap at 10 points to avoid overwhelming recency
        score += min(keyword_boost * 0.5, 10)

    # Recency — prefer fresh articles but don't completely bury older ones
    age = (datetime.now(timezone.utc) - article["date"]).total_seconds() / 3600
    score -= age * 0.3

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
    safe_title = article["title"][:60].replace("'", "\\'").replace('"', '&quot;')
    feedback_btn = f'<button class="thumb feedback-btn" onclick="openFeedback(\'{article["id"]}\', \'{safe_title}\', \'{escape(article.get("current_topic", ""))}\', \'{escape(article["url"])}\')" title="Wrong section?">⚑ Wrong section?</button>'

    return f"""
<article class="card" id="card-{article['id']}">
  {img_html}
  <div class="card-body">
    <div class="card-meta">{source_badge} <span class="time-ago">{time_str}</span></div>
    <h3 class="card-title">
      <a href="{escape(article['url'])}" target="_blank" rel="noopener"
         onclick="trackClick('{article['id']}', '{escape(article['source'])}', {json.dumps(article['title'])})">{escape(article['title'])}</a>
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

/* ── SCROLL OFFSET — set dynamically by JS ── */
html {{
  scroll-padding-top: 160px;
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


/* ── SETTINGS PANEL ── */
.settings-btn {{
  margin-left: 0.5rem;
  background: rgba(255,255,255,0.2);
  border: none;
  color: var(--bg);
  cursor: pointer;
  font-size: 0.8rem;
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  font-family: var(--font-body);
}}
.settings-panel {{
  display: none;
  position: fixed;
  top: 0;
  right: 0;
  width: 320px;
  height: 100vh;
  background: var(--card-bg);
  box-shadow: -4px 0 24px rgba(0,0,0,0.15);
  z-index: 200;
  overflow-y: auto;
  padding: 1.5rem;
}}
.settings-panel.open {{ display: block; }}
.settings-panel h3 {{
  font-family: var(--font-heading);
  font-size: 1.1rem;
  margin-bottom: 1.25rem;
  color: var(--text);
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.settings-close {{
  background: none;
  border: none;
  font-size: 1.2rem;
  cursor: pointer;
  color: var(--text-muted);
  padding: 0;
}}
.settings-section-label {{
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin: 1rem 0 0.5rem;
}}
.settings-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.75rem;
  gap: 0.5rem;
}}
.settings-row label {{
  font-size: 0.85rem;
  color: var(--text);
  flex: 1;
}}
.settings-count {{
  display: flex;
  align-items: center;
  gap: 0.25rem;
}}
.count-btn {{
  width: 24px;
  height: 24px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
  border-radius: 4px;
  font-size: 0.9rem;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-body);
}}
.count-btn:hover {{ background: var(--border); }}
.count-value {{
  min-width: 24px;
  text-align: center;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text);
}}
.settings-toggle {{
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
  cursor: pointer;
  font-size: 0.85rem;
  color: var(--text);
}}
.toggle-switch {{
  width: 36px;
  height: 20px;
  background: var(--border);
  border-radius: 10px;
  position: relative;
  transition: background 0.2s;
  flex-shrink: 0;
}}
.toggle-switch.on {{ background: var(--accent); }}
.toggle-knob {{
  width: 16px;
  height: 16px;
  background: white;
  border-radius: 50%;
  position: absolute;
  top: 2px;
  left: 2px;
  transition: left 0.2s;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}}
.toggle-switch.on .toggle-knob {{ left: 18px; }}
.settings-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.3);
  z-index: 199;
}}
.settings-overlay.open {{ display: block; }}

/* ── HORIZONTAL SCROLL MODE ── */
.cards-grid.scroll-mode {{
  display: flex;
  flex-direction: row;
  overflow-x: auto;
  gap: 1.25rem;
  padding-bottom: 0.75rem;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
  -webkit-overflow-scrolling: touch;
}}
.cards-grid.scroll-mode::-webkit-scrollbar {{ height: 4px; }}
.cards-grid.scroll-mode::-webkit-scrollbar-track {{ background: transparent; }}
.cards-grid.scroll-mode::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}
.cards-grid.scroll-mode .card {{
  min-width: 300px;
  max-width: 300px;
  flex-shrink: 0;
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
    <button class="settings-btn" onclick="openSettings()">⚙ Settings</button>
  </nav>
</header>

<main class="main-content">
  {following_html}
  {trending_html}
  {topic_sections_html}
</main>

<!-- ── SETTINGS OVERLAY ── -->
<div class="settings-overlay" id="settingsOverlay" onclick="closeSettings()"></div>

<!-- ── SETTINGS PANEL ── -->
<div class="settings-panel" id="settingsPanel">
  <h3>
    ⚙ Settings
    <button class="settings-close" onclick="closeSettings()">✕</button>
  </h3>

  <div class="settings-section-label">Display</div>
  <label class="settings-toggle" onclick="toggleScrollMode()">
    <div class="toggle-switch" id="scrollToggle"><div class="toggle-knob"></div></div>
    Horizontal scroll sections
  </label>

  <div class="settings-section-label">Stories per section</div>
  <div id="topicCountRows"></div>

  <div style="margin-top:1.5rem; padding-top:1rem; border-top:1px solid var(--border)">
    <div class="settings-section-label">Your interests (learned from clicks)</div>
    <div id="interestSummary" style="font-size:0.78rem; color:var(--text-muted); line-height:1.6">
      Keep reading articles to build your profile.
    </div>
  </div>
  <div style="margin-top:1rem; font-size:0.72rem; color:var(--text-muted)">
    Changes apply immediately and are saved in your browser.
  </div>
</div>

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
    <div id="feedbackSuccess" style="display:none" class="feedback-success">
      ✓ Feedback sent — thanks! Next refresh will learn from this.
    </div>
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

function extractKeywords(title) {{
  const stop = new Set(["the","and","for","that","this","with","from","have",
    "been","will","were","they","their","about","into","after","over","when",
    "says","said","what","who","how","why","new","also","more","than","just"]);
  return title.toLowerCase()
    .replace(/[^a-z ]/g, " ")
    .split(/ +/)
    .filter(w => w.length >= 4 && !stop.has(w));
}}

function trackClick(articleId, source, title) {{
  const data = loadInteractions();
  // Click count
  data.clicks = data.clicks || {{}};
  data.clicks[articleId] = (data.clicks[articleId] || 0) + 1;
  // Source preference
  data.source_scores = data.source_scores || {{}};
  data.source_scores[source] = (data.source_scores[source] || 0) + 1;
  // Keyword interest
  data.keyword_scores = data.keyword_scores || {{}};
  if (title) {{
    extractKeywords(title).forEach(kw => {{
      data.keyword_scores[kw] = (data.keyword_scores[kw] || 0) + 1;
    }});
  }}
  data.read_count = (data.read_count || 0) + 1;
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



// ── Settings panel ────────────────────────────────────────────────────────
const SETTINGS_KEY = 'newsdigest_settings';

// Topic data injected at build time
const TOPIC_DATA = __TOPIC_DATA__;

function loadSettings() {{
  try {{ return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {{}}; }}
  catch {{ return {{}}; }}
}}

function saveSettings(s) {{
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
}}

function openSettings() {{
  document.getElementById('settingsPanel').classList.add('open');
  document.getElementById('settingsOverlay').classList.add('open');
  buildInterestSummary();
}}

function closeSettings() {{
  document.getElementById('settingsPanel').classList.remove('open');
  document.getElementById('settingsOverlay').classList.remove('open');
}}

function toggleScrollMode() {{
  const s = loadSettings();
  s.scrollMode = !s.scrollMode;
  saveSettings(s);
  applyScrollMode(s.scrollMode);
  const toggle = document.getElementById('scrollToggle');
  toggle.classList.toggle('on', s.scrollMode);
}}

function applyScrollMode(enabled) {{
  document.querySelectorAll('.cards-grid').forEach(g => {{
    g.classList.toggle('scroll-mode', enabled);
  }});
}}

function changeCount(topicName, delta) {{
  const s = loadSettings();
  s.counts = s.counts || {{}};
  const defaultCount = TOPIC_DATA.find(t => t.name === topicName)?.default_count || 5;
  const current = s.counts[topicName] ?? defaultCount;
  const newVal = Math.max(1, Math.min(12, current + delta));
  s.counts[topicName] = newVal;
  saveSettings(s);
  // Update display
  const el = document.getElementById('count-' + topicName.replace(/[^a-z0-9]/gi, '-'));
  if (el) el.textContent = newVal;
  // Update visible cards
  applyTopicCount(topicName, newVal);
}}

function applyTopicCount(topicName, count) {{
  const sectionId = 'topic-' + topicName.toLowerCase().replace(/ +/g, '-').replace(/[^a-z0-9-]/g, '');
  const section = document.getElementById(sectionId);
  if (!section) return;
  const cards = section.querySelectorAll('.card');
  cards.forEach((card, i) => {{
    card.style.display = i < count ? '' : 'none';
  }});
}}

function buildSettingsRows() {{
  const s = loadSettings();
  const container = document.getElementById('topicCountRows');
  if (!container) return;
  container.innerHTML = TOPIC_DATA.map(t => {{
    const current = (s.counts || {{}})[t.name] ?? t.default_count;
    const safeId = t.name.replace(/[^a-z0-9]/gi, '-');
    return `<div class="settings-row">
      <label style="color:${{t.color}};font-weight:600">${{t.name}}</label>
      <div class="settings-count">
        <button class="count-btn" onclick="changeCount('${{t.name}}', -1)">−</button>
        <span class="count-value" id="count-${{safeId}}">${{current}}</span>
        <button class="count-btn" onclick="changeCount('${{t.name}}', 1)">+</button>
      </div>
    </div>`;
  }}).join('');
}}

function applyAllSettings() {{
  const s = loadSettings();
  // Scroll mode
  applyScrollMode(!!s.scrollMode);
  const toggle = document.getElementById('scrollToggle');
  if (toggle) toggle.classList.toggle('on', !!s.scrollMode);
  // Topic counts
  if (s.counts) {{
    Object.entries(s.counts).forEach(([name, count]) => applyTopicCount(name, count));
  }}
}}

// ── Feedback modal ────────────────────────────────────────────────────────
let currentFeedback = {{}};

function openFeedback(articleId, title, currentTopic, url) {{
  currentFeedback = {{ articleId, title, currentTopic, url }};
  document.getElementById('modalArticleTitle').textContent = title + (currentTopic ? ' (in: ' + currentTopic + ')' : '');
  document.querySelectorAll('input[name="feedbackType"]').forEach(r => r.checked = false);
  // Populate topic dropdown dynamically from TOPIC_DATA
  const select = document.getElementById('topicRedirect');
  select.style.display = 'none';
  select.value = '';
  // Rebuild options from current topic list
  select.innerHTML = '<option value="">— Select correct topic (optional) —</option>' +
    TOPIC_DATA.map(t => `<option value="${{t.name}}">${{t.name}}</option>`).join('') +
    '<option value="none">None of my topics</option>';
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
    const suggestLine = (redirectTopic && redirectTopic !== 'none') ? `\nsuggested_topic: ${{redirectTopic}}` : '';
    body = `article_id: ${{currentFeedback.articleId}}\nappeared_in_topic: ${{currentFeedback.currentTopic}}\nblock_from_topic: ${{currentFeedback.currentTopic}}${{suggestLine}}\n\nArticle: ${{currentFeedback.title}}\n${{currentFeedback.url}}`;
  }} else if (feedbackType === 'not_relevant') {{
    title = `[Not relevant] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `article_id: ${{currentFeedback.articleId}}\nblock_from_topic: __all__\n\nArticle: ${{currentFeedback.title}}\n${{currentFeedback.url}}`;
  }} else if (feedbackType === 'repetitive') {{
    title = `[Repetitive] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `article_id: ${{currentFeedback.articleId}}\nblock_from_topic: ${{currentFeedback.currentTopic}}\n\nArticle: ${{currentFeedback.title}}\n${{currentFeedback.url}}`;
  }} else {{
    title = `[Feedback] ${{currentFeedback.title.substring(0, 60)}}`;
    body = `article_id: ${{currentFeedback.articleId}}\nappeared_in_topic: ${{currentFeedback.currentTopic}}\nnote: ${{otherText}}\n\nArticle: ${{currentFeedback.title}}\n${{currentFeedback.url}}`;
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
      // Show success without destroying form
      const successEl = document.getElementById('feedbackSuccess');
      if (successEl) {{
        successEl.style.display = 'block';
        setTimeout(() => {{
          successEl.style.display = 'none';
          closeFeedback();
          // Reset form
          document.querySelectorAll('input[name="feedbackType"]').forEach(r => r.checked = false);
          document.getElementById('topicRedirect').style.display = 'none';
          document.getElementById('topicRedirect').value = '';
          document.getElementById('feedbackOther').style.display = 'none';
          document.getElementById('feedbackOther').value = '';
        }}, 2000);
      }}
    }} else {{
      alert('Could not send feedback. Please try again later.');
    }}
  }} catch(e) {{
    alert('Could not send feedback — check your connection.');
  }}
}}

// ── Interest summary ─────────────────────────────────────────────────────
function buildInterestSummary() {{
  const el = document.getElementById('interestSummary');
  if (!el) return;
  const data = loadInteractions();
  const reads = data.read_count || 0;
  if (reads < 3) {{
    el.textContent = 'Read a few more articles to build your profile.';
    return;
  }}
  // Top sources
  const sources = Object.entries(data.source_scores || {{}})
    .sort((a,b) => b[1]-a[1]).slice(0,3).map(e => e[0]);
  // Top keywords
  const keywords = Object.entries(data.keyword_scores || {{}})
    .sort((a,b) => b[1]-a[1]).slice(0,8).map(e => e[0]);
  let html = '<strong>' + reads + ' articles read</strong><br>';
  if (sources.length) html += '📰 Favourite sources: ' + sources.join(', ') + '<br>';
  if (keywords.length) html += '🔑 Your interests: ' + keywords.join(', ');
  el.innerHTML = html;
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
  // Dynamic scroll padding based on actual header height
  function updateScrollPadding() {{
    const header = document.querySelector('.site-header');
    if (header) {{
      const h = header.getBoundingClientRect().height;
      document.documentElement.style.setProperty('scroll-padding-top', (h + 24) + 'px');
    }}
  }}
  updateScrollPadding();
  window.addEventListener('resize', updateScrollPadding);

  // Init settings
  buildSettingsRows();
  applyAllSettings();
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

    # Load interaction scores
    interactions = load_interactions() if INTERACTIONS_PATH.exists() else {}

    # Load feedback blocklist from GitHub Issues
    create_feedback_label()
    blocklist, redirects, feedback_examples = fetch_feedback_blocklist()

    # Pre-filter: split sources into news and specialist tiers
    # to ensure specialist topics (Tech, AI, Finance, Arts) always get coverage
    NEWS_SOURCES = {
        "The Guardian - World", "The Guardian - Australia",
        "ABC News Australia", "Al Jazeera English", "Vox",
        "WSJ - World News", "WSJ - US Business",
    }
    SPECIALIST_SOURCES = {
        "The Guardian - Environment", "The Guardian - Culture",
        "Ars Technica", "MIT Technology Review",
        "Reuters World", "Reuters Finance", "CNBC Finance",
        "Finder AU", "Pitchfork", "Vulture",
        "AP Entertainment", "AP Technology",
        "Time Out Melbourne", "Carbon Brief", "The Conversation",
    }
    MAX_PER_SOURCE = 12
    NEWS_CAP = 50
    SPECIALIST_CAP = 60

    from collections import defaultdict
    news_buckets = defaultdict(list)
    specialist_buckets = defaultdict(list)

    for a in sorted(all_articles, key=lambda a: a["date"], reverse=True):
        source = a["source"]
        if source in NEWS_SOURCES:
            if len(news_buckets[source]) < MAX_PER_SOURCE:
                news_buckets[source].append(a)
        elif source in SPECIALIST_SOURCES:
            if len(specialist_buckets[source]) < MAX_PER_SOURCE:
                specialist_buckets[source].append(a)

    news_articles = []
    for arts in news_buckets.values():
        news_articles.extend(arts)
    news_articles.sort(key=lambda a: a["date"], reverse=True)
    news_articles = news_articles[:NEWS_CAP]

    specialist_articles = []
    for arts in specialist_buckets.values():
        specialist_articles.extend(arts)
    specialist_articles.sort(key=lambda a: a["date"], reverse=True)
    specialist_articles = specialist_articles[:SPECIALIST_CAP]

    articles_to_classify = news_articles + specialist_articles
    print(f"  Pre-filter: {len(all_articles)} total → {len(news_articles)} news + {len(specialist_articles)} specialist = {len(articles_to_classify)} for classification")

    # Assign articles to topics — use Claude classification if API key available
    topic_articles = {}
    if ANTHROPIC_API_KEY:
        print("  Classifying articles with Claude...")
        classified, articles_to_classify = classify_and_summarise(articles_to_classify, feedback_examples)
    else:
        classified = None

    if classified is not None:
        # Claude classified — apply blocklist, score, deduplicate
        # Track article IDs already assigned to prevent cross-topic duplicates
        assigned_ids = set()
        for topic in TOPICS:
            matches = [
                a for a in classified.get(topic["name"], [])
                if topic["name"] not in blocklist.get(a["id"], [])
                and "__all__" not in blocklist.get(a["id"], [])
                and a["id"] not in assigned_ids
            ]
            matches.sort(key=lambda a: score_article(a, interactions), reverse=True)
            seen_titles = set()
            deduped = []
            for a in matches:
                title_key = a["title"][:50].lower()
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    deduped.append(a)
                    assigned_ids.add(a["id"])
            count = topic.get("default_count", DIGEST.get("stories_per_topic", 5))
            topic_articles[topic["name"]] = deduped[:count]
        print(f"  Classification complete — {len(assigned_ids)} articles assigned")
    else:
        # Fallback: keyword matching
        print("  Using keyword matching (no API key or classification failed)")
        for topic in TOPICS:
            matches = [
                a for a in all_articles
                if matches_topic(a["title"] + " " + a.get("summary", ""), topic)
                and topic["name"] not in blocklist.get(a["id"], [])
                and "__all__" not in blocklist.get(a["id"], [])
            ]
            matches.sort(key=lambda a: score_article(a, interactions), reverse=True)
            seen_titles = set()
            deduped = []
            for a in matches:
                title_key = a["title"][:50].lower()
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    deduped.append(a)
            count = topic.get("default_count", DIGEST.get("stories_per_topic", 5))
            topic_articles[topic["name"]] = deduped[:count]

    # Apply redirects from user feedback
    for art_id, target_topic in redirects.items():
        article = next((a for a in all_articles if a["id"] == art_id), None)
        if not article:
            continue
        if target_topic not in topic_articles:
            continue
        existing_ids = {a["id"] for a in topic_articles[target_topic]}
        if art_id not in existing_ids:
            article["redirected"] = True
            topic_articles[target_topic].insert(0, article)
            print(f"  Redirected article {art_id} to {target_topic}")

    # Following stories
    following_articles = find_following(all_articles)

    # Trending
    trending = find_trending(all_articles)

    # Build HTML
    html = build_html(topic_articles, following_articles, trending, all_articles)

    # Inject topic data for settings panel
    import json as _json
    topic_data = _json.dumps([
        {"name": t["name"], "color": t.get("color", "#333"), "default_count": t.get("default_count", 5)}
        for t in TOPICS
    ])
    html = html.replace("__TOPIC_DATA__", topic_data)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"✅ Digest built → {OUTPUT_PATH}")
    print(f"   Following: {len(following_articles)} articles")
    print(f"   Trending: {len(trending)} topics")

if __name__ == "__main__":
    main()
