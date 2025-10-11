import feedparser
from datetime import datetime, timedelta, timezone
import yagmail
from omegaconf import OmegaConf 

CONFIGPATH = f'./config/'

# ========== User Setting ==========
query = "Open-vocabulary Semantic Segmentation"
categories = ["cs.CV", "cs.CL", "cs.AI"] 
max_results = 50
days = 7


# Email Setting
emailfile = f'MyEmail.yaml'
EMAILPATH = CONFIGPATH + emailfile
email_cfg = OmegaConf.load(EMAILPATH)
sender_email = email_cfg.sender_email
sender_pass = email_cfg.sender_pass
receiver_email = email_cfg.receiver_email

# ========== Fetch paper infomation on arXiv ==========
def fetch_arxiv(category, query, max_results=50):
    base_url = "http://export.arxiv.org/api/query?"
    search_query = f"cat:{category}+AND+({query.replace(' ', '+')})"
    url = (f"{base_url}search_query={search_query}"
           f"&start=0&max_results={max_results}"
           f"&sortBy=submittedDate&sortOrder=descending")
    feed = feedparser.parse(url)
    return feed.entries

def get_recent_papers():
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    papers = []

    for cat in categories:
        entries = fetch_arxiv(cat, query, max_results)
        for e in entries:
            published = datetime.strptime(e.published, "%Y-%m-%dT%H:%M:%SZ")
            published = published.replace(tzinfo=timezone.utc)
            if published >= cutoff:
                papers.append({
                    "title": e.title.replace("\n", " ").strip(),
                    "authors": ", ".join(a.name for a in e.authors),
                    "summary": e.summary.replace("\n", " ").strip(),
                    "published": published.strftime("%Y-%m-%d"),
                    "category": cat,
                    "link": e.link
                })
    return papers

# ========== Generate Markdown ==========
def generate_markdown(papers):
    if not papers:
        return "# 今日 arXiv 没有找到相关论文\n"

    papers.sort(key=lambda x: x["published"], reverse=True)

    md = "# 今日 arXiv 新论文精选\n\n"
    md += f"**检索时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n"
    md += f"**关键词**: {query}  \n"
    md += f"**分类**: {', '.join(categories)}  \n\n"

    for i, p in enumerate(papers, 1):
        md += f"### {i}. [{p['title']}]({p['link']})\n"
        md += f"**Authors:** {p['authors']}  \n"
        md += f"**Category:** {p['category']}  \n"
        md += f"**Published:** {p['published']}  \n\n"
        md += f"{p['summary']}\n\n---\n\n"

    return md

# ========== 发送邮件 ==========
def send_email(subject, body_md):
    yag = yagmail.SMTP(sender_email, sender_pass, host="smtp.qq.com", port=465, smtp_ssl=True,)
    yag.send(
        to=receiver_email,
        subject=subject,
        contents=[body_md]
    )
    print(f"邮件已发送至 {receiver_email}")

# ========== 主程序 ==========
if __name__ == "__main__":
    papers = get_recent_papers()
    md_report = generate_markdown(papers)
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"arXiv 今日精选论文 {today}"

    # 保存本地 Markdown
    with open(f"./savefile/arxiv_daily_{today}.md", "w", encoding="utf-8") as f:
        f.write(md_report)

    # 发送邮件
    send_email(subject, md_report)
