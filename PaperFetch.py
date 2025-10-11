import feedparser
from datetime import datetime, timedelta, timezone
import pandas as pd
from omegaconf import OmegaConf 

# -------- parameter --------
query = "computer vision OR multimodal"  # 关键词，可改成你的研究方向
category = "cs.CV"  # arXiv 分类，如 cs.CV, cs.CL, cs.AI 等
max_results = 50    # 每天最多获取多少篇
days = 7            # 获取最近多少天内的论文

# -------- construct query --------
base_url = "http://export.arxiv.org/api/query?"
start = 0
sortBy = "submittedDate"
sortOrder = "descending"

search_query = f"cat:{category}+AND+({query.replace(' ', '+')})"

url = (f"{base_url}search_query={search_query}"
       f"&start={start}&max_results={max_results}"
       f"&sortBy={sortBy}&sortOrder={sortOrder}")

# -------- 抓取数据 --------
feed = feedparser.parse(url)
print(f"从 arXiv 获取到 {len(feed.entries)} 篇论文")

# -------- 筛选最近 N 天内的论文 --------
cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

records = []
for entry in feed.entries:
    published = datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%SZ")
    published = published.replace(tzinfo=timezone.utc)
    if published >= cutoff_date:
        records.append({
            "title": entry.title.replace("\n", " ").strip(),
            "authors": ", ".join(a.name for a in entry.authors),
            "published": published.strftime("%Y-%m-%d"),
            "summary": entry.summary.replace("\n", " ").strip(),
            "link": entry.link
        })

# -------- 保存结果 --------
if records:
    df = pd.DataFrame(records)
    filename = f"./savefile/arxiv_{category}_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"已保存 {len(records)} 篇论文到 {filename}")
else:
    print("最近一天没有相关论文。")
