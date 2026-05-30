"""
PaperFetch_daily.py
自动爬取 arXiv 上最新的相关论文并发送 Markdown 格式邮件
"""

import feedparser
import time
import re
import yagmail
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from omegaconf import OmegaConf 

CONFIGPATH = f'./config/'

# ========== 🧩 用户配置区域 ==========
# arXiv 查询设置
CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]   # 查询类别
KEYWORDS = ["open vocabulary semantic segmentation", "open-vocabulary semantic segmentation"]  # 查询关键词
DAYS = 20             # 最近几天的论文
MAX_RESULTS = 100    # 每个类别最大返回论文数
MAX_PAGES = 5         # 最多翻页次数，避免近期论文超过单页上限时漏抓
REQUEST_TIMEOUT = 30

# Email Setting
emailfile = f'MyEmail.yaml'
EMAILPATH = CONFIGPATH + emailfile
email_cfg = OmegaConf.load(EMAILPATH)
SENDER_EMAIL = email_cfg.sender_email
SENDER_PASS = email_cfg.sender_pass
RECEIVER_EMAIL = email_cfg.receiver_email
SMTP_HOST = "smtp.qq.com"

# =====================================


# ---------- Tool Function ----------
def normalize_keywords(keywords):
    """确保关键词为字符串列表"""
    if isinstance(keywords, str):
        parts = re.split(r'\s*,\s*|\s+OR\s+|\s*\|\|\s*', keywords.strip(), flags=re.IGNORECASE)
        return [p.strip() for p in parts if p.strip()]
    elif isinstance(keywords, (list, tuple)):
        return [str(k).strip() for k in keywords if str(k).strip()]
    else:
        raise ValueError("keywords must be a list or a string")


def make_arxiv_inner_clause(keywords):
    """生成 arXiv 查询字符串"""
    kws = normalize_keywords(keywords)
    parts = []
    for kw in kws:
        kw_escaped = kw.replace('"', '\\"')
        if " " in kw_escaped:
            parts.append(f'all:"{kw_escaped}"')
        else:
            parts.append(f"all:{kw_escaped}")
    return " OR ".join(parts)


def build_arxiv_url(category, keywords, start=0, max_results=50):
    """构造 arXiv API 查询 URL"""
    inner = make_arxiv_inner_clause(keywords)
    raw_query = f"cat:{category} AND ({inner})"
    encoded_query = quote_plus(raw_query)
    base_url = "https://export.arxiv.org/api/query?"
    return (f"{base_url}search_query={encoded_query}"
            f"&start={start}&max_results={max_results}"
            f"&sortBy=submittedDate&sortOrder=descending")


def fetch_feed_with_retry(url, max_retries=3, delay=2):
    """带重试的 feedparser 请求；请求失败时不要伪装成空结果。"""
    last_error = None
    for attempt in range(max_retries):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": "PaperFetch/1.0 (arXiv daily digest; contact: local-user)"
                },
            )
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                data = response.read()

            feed = feedparser.parse(data)
            if getattr(feed, "bozo", False):
                last_error = getattr(feed, "bozo_exception", "feed parse error")
                raise RuntimeError(f"arXiv feed parse failed: {last_error}")
            return feed
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))

    raise RuntimeError(f"Failed to fetch arXiv feed after {max_retries} retries: {last_error}")


def keyword_in_text(text, kw):
    """宽松关键词匹配"""
    patt = r'(?<![A-Za-z0-9_])' + re.escape(kw.lower()) + r'(?![A-Za-z0-9_])'
    return re.search(patt, text.lower()) is not None


def keyword_match(entry, keywords):
    """标题/摘要匹配关键词"""
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    return any(keyword_in_text(text, kw) for kw in keywords)


def entry_recent_time(entry):
    """取论文 published/updated 中较新的时间，避免更新论文被 published 过滤掉。"""
    candidates = []
    for key in ("published", "updated"):
        value = entry.get(key)
        if not value:
            continue
        try:
            candidates.append(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return max(candidates) if candidates else None


# ---------- 主爬取函数 ----------
def fetch_arxiv_papers(keywords, categories, days=1, max_results=100, max_pages=MAX_PAGES):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    papers, seen = [], set()

    for cat in categories:
        for page in range(max_pages):
            start = page * max_results
            url = build_arxiv_url(cat, keywords, start=start, max_results=max_results)
            feed = fetch_feed_with_retry(url)

            if not feed.entries:
                break

            reached_old_entries = False
            for e in feed.entries:
                recent_time = entry_recent_time(e)
                if recent_time is None:
                    continue

                if recent_time < cutoff:
                    reached_old_entries = True
                    continue
                if not keyword_match(e, keywords):
                    continue

                title = e.get("title", "").strip().replace("\n", " ")
                key = e.get("id") or e.get("link") or (title, recent_time.strftime("%Y-%m-%d"))
                if key in seen:
                    continue
                seen.add(key)

                papers.append({
                    "title": title,
                    "authors": ", ".join(a.name for a in e.get("authors", [])),
                    "summary": re.sub(r"\s+", " ", e.get("summary", "").strip()),
                    "published": recent_time.strftime("%Y-%m-%d"),
                    "category": cat,
                    "link": e.get("link", "")
                })

            if reached_old_entries:
                break

            time.sleep(3)

    # Sorted by Dates
    papers.sort(key=lambda x: x["published"], reverse=True)
    return papers


# ---------- Markdown Generation ----------
def generate_markdown(papers):
    if not papers:
        return "### No new papers found today.\n"

    md = ["# Daily arXiv Digest\n"]
    for i, p in enumerate(papers, 1):
        md.append(f"### {i}. [{p['title']}]({p['link']})")
        md.append(f"- **Authors:** {p['authors']}")
        md.append(f"- **Category:** `{p['category']}`")
        md.append(f"- **Published:** {p['published']}\n")
        md.append(f"{p['summary']}\n")
    return "\n".join(md)


# ---------- 邮件发送 ----------
def send_email(subject, markdown_content):
    yag = yagmail.SMTP(user=SENDER_EMAIL, password=SENDER_PASS, host=SMTP_HOST)
    yag.send(
        to=RECEIVER_EMAIL,
        subject=subject,
        contents=[markdown_content]
    )
    print(f"邮件已发送至 {RECEIVER_EMAIL}")


# ---------- 主程序 ----------
if __name__ == "__main__":
    print("正在从 arXiv 获取最新论文...")

    papers = fetch_arxiv_papers(KEYWORDS, CATEGORIES, days=DAYS, max_results=MAX_RESULTS)
    print(f"共获取到 {len(papers)} 篇符合条件的论文")

    md_report = generate_markdown(papers)
    subject = f"arXiv Daily Digest - {datetime.now().strftime('%Y-%m-%d')}"

    send_email(subject, md_report)
