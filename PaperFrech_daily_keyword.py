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
from omegaconf import OmegaConf 

CONFIGPATH = f'./config/'

# ========== 🧩 用户配置区域 ==========
# arXiv 查询设置
CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]   # 查询类别
KEYWORDS = ["open vocabulary semantic segmentation", "match"]  # 查询关键词
DAYS = 1             # 最近几天的论文
MAX_RESULTS = 100    # 每个类别最大返回论文数

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
    base_url = "http://export.arxiv.org/api/query?"
    return (f"{base_url}search_query={encoded_query}"
            f"&start={start}&max_results={max_results}"
            f"&sortBy=submittedDate&sortOrder=descending")


def fetch_feed_with_retry(url, max_retries=3, delay=2):
    """带重试的 feedparser 请求"""
    for attempt in range(max_retries):
        feed = feedparser.parse(url)
        if feed.entries:
            return feed
        time.sleep(delay)
    return feedparser.parse(url)


def keyword_in_text(text, kw):
    """宽松关键词匹配"""
    patt = r'(?<![A-Za-z0-9_])' + re.escape(kw.lower()) + r'(?![A-Za-z0-9_])'
    return re.search(patt, text.lower()) is not None


def keyword_match(entry, keywords):
    """标题/摘要匹配关键词"""
    text = (entry.title + " " + entry.summary).lower()
    return any(keyword_in_text(text, kw) for kw in keywords)


# ---------- 主爬取函数 ----------
def fetch_arxiv_papers(keywords, categories, days=1, max_results=100):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    papers, seen = [], set()

    for cat in categories:
        url = build_arxiv_url(cat, keywords, start=0, max_results=max_results)
        feed = fetch_feed_with_retry(url)

        for e in feed.entries:
            try:
                published = datetime.strptime(e.published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if published < cutoff:
                continue
            if not keyword_match(e, keywords):
                continue

            title = e.title.strip().replace("\n", " ")
            key = (title, published.strftime("%Y-%m-%d"))
            if key in seen:
                continue
            seen.add(key)

            papers.append({
                "title": title,
                "authors": ", ".join(a.name for a in e.authors),
                "summary": re.sub(r"\s+", " ", e.summary.strip()),
                "published": published.strftime("%Y-%m-%d"),
                "category": cat,
                "link": e.link
            })

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
