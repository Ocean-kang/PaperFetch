# Codex 修改计划：PaperFetch 稳定运行、空结果通知与 arXiv 429 修复

## 0. 任务定位

你需要修改当前 `PaperFetch` 项目，使它能够稳定执行每日论文检索，并且在三种情况下都给用户发送邮件通知：

1. 找到匹配论文：发送论文列表邮件。
2. 没有找到匹配论文：发送“未找到相关论文”的简洁邮件。
3. arXiv 请求失败，例如 HTTP 429、timeout、网络异常：发送“运行失败 / arXiv 请求失败”的错误通知邮件。

本次任务不是重构项目，而是在现有代码基础上做稳定性修复和必要功能增强。

请优先阅读以下文件：

- `PaperFrech_daily_keyword.py`
- `run.sh`
- `config/MyEmail.yaml`
- `log/run.log`
- `cache/` 相关缓存逻辑

---

## 1. 当前已观察到的问题

根据运行日志，当前项目已经可以正常进入环境并运行：

```text
===== PaperFetch START 2026-06-03 16:14:34 +0800 =====
PWD=/root/code/PaperFetch
Conda env: paperfetch
Python: /root/miniconda3/envs/paperfetch/bin/python
Python 3.12.11
```

说明：

- `run.sh` 能启动。
- conda 环境能激活。
- Python 脚本能执行。

但是后续出现了 arXiv 请求失败：

```text
HTTP 429 from arXiv
retry after 36.03s
HTTP 429 from arXiv
retry after 64.80s
HTTP 429 from arXiv
retry after 124.55s
HTTP 429 from arXiv
retry after 247.57s
HTTP 429 from arXiv
RuntimeError: Failed to fetch arXiv feed after 5 retries: HTTP Error 429: Unknown Error
```

最终 `run.sh` 报错退出：

```text
===== PaperFetch ERROR 2026-06-03 16:23:11 +0800 =====
line=57
cmd=timeout 600 "$PY" "$SCRIPT"
exit=1
```

这说明当前还有一个核心缺陷：

> 当 arXiv 请求失败时，程序直接异常退出，没有发送任何邮件通知。

之前已经要求修复“0 篇论文也要发邮件”，但这次不是 0 篇论文，而是 arXiv API 请求阶段失败。因此需要新增“请求失败也发邮件”的逻辑。

---

## 2. 总体目标

最终目标是：

```text
bash run.sh 一定产生邮件通知：

1. 有论文：
   - 发送论文列表。
   - 日志显示 final paper count=N。
   - 日志显示 email sent to ...

2. 没有论文：
   - 发送“未找到匹配论文”的简洁报告。
   - 日志显示 final paper count=0。
   - 日志显示 No matched papers found, sending empty report email.
   - 日志显示 email sent to ...

3. arXiv 请求失败：
   - 发送“PaperFetch 运行失败 / arXiv 请求失败”的错误报告。
   - 邮件说明失败原因，例如 HTTP 429 或 timeout。
   - 日志显示 exception traceback。
   - 日志显示 failure email sent to ...
```

同时满足：

- 默认检索最近 20 天。
- 关键词覆盖 open-vocabulary segmentation、无监督匹配、无监督 alignment、多模态 alignment、分布对齐、embedding translator 等方向。
- 减少 arXiv 429 的概率。
- 不因为空结果或请求失败而静默无通知。
- 不把邮箱授权码写死进代码。
- 不重构整个项目。

---

## 3. 必须完成的需求

### 3.1 默认检索范围改为 20 天

找到当前控制检索天数的地方，可能是：

```python
DAYS = 1
```

或者：

```python
parser.add_argument("--days", type=int, default=1, ...)
```

修改为：

```python
DEFAULT_DAYS = 20
```

或者：

```python
parser.add_argument("--days", type=int, default=20, ...)
```

要求：

1. 直接运行 `bash run.sh` 时默认检索最近 20 天。
2. 命令行传入 `--days N` 时仍然可以覆盖默认值。
3. 日志必须打印实际使用的 days：

```text
starting PaperFetch days=20 max_results=100
```

---

### 3.2 没有找到论文也必须发邮件

当前可能存在类似逻辑：

```python
if not papers:
    LOGGER.info("No matched papers found.")
    if not SEND_EMPTY_REPORT:
        LOGGER.info("No matched papers today, skip email.")
        return 0
```

这是不允许的。

请修改为：

```python
if not papers:
    LOGGER.info("No matched papers found, sending empty report email.")
```

然后继续构造邮件并发送。

邮件主题建议：

```text
PaperFetch: 最近 20 天未找到匹配论文
```

邮件正文建议：

```markdown
# PaperFetch 检索结果

本次没有找到符合条件的论文。

- 检索范围：最近 20 天
- 匹配论文数量：0
- arXiv categories: cs.CV, cs.CL, cs.AI
- 关键词数量：N
- 运行时间：YYYY-MM-DD HH:MM:SS

这表示程序运行成功，但本次查询没有匹配结果，不是邮箱发送失败。
```

要求：

1. 不允许 0 篇论文时直接 `return 0` 而不发邮件。
2. 不允许依赖环境变量才发送空报告。
3. 如果保留 `SEND_EMPTY_REPORT`，默认值必须为 `true`。
4. 更推荐直接取消“空结果不发邮件”的分支，使空结果始终发送邮件。

---

### 3.3 arXiv 请求失败也必须发邮件

这是本次最重要的新增需求。

当前程序在这里异常退出：

```python
papers = fetch_arxiv_papers(...)
```

当 `fetch_arxiv_papers()` 内部因为 HTTP 429、timeout 或 RuntimeError 失败时，主流程没有捕获异常，因此不会发送邮件。

请在 `main()` 里使用 `try/except` 包裹论文抓取逻辑。

参考结构：

```python
try:
    papers = fetch_arxiv_papers(
        keywords=keywords,
        categories=categories,
        days=days,
        max_results=max_results,
        no_cache=no_cache,
    )
except Exception as exc:
    LOGGER.exception("Failed to fetch arXiv papers")
    subject = build_failure_subject(days=days)
    body = build_failure_email_body(
        error=exc,
        keywords=keywords,
        categories=categories,
        days=days,
        max_results=max_results,
    )
    try:
        send_email(subject, body)
        LOGGER.info("failure email sent to configured receiver")
    except Exception:
        LOGGER.exception("Failed to send failure notification email")
    return 1
```

错误通知邮件主题建议：

```text
PaperFetch: arXiv 请求失败
```

错误通知邮件正文必须包含：

```markdown
# PaperFetch 运行失败

程序已经启动，但在请求 arXiv 时失败，因此没有得到论文结果。

- 错误类型：RuntimeError
- 错误信息：Failed to fetch arXiv feed after 5 retries: HTTP Error 429: Unknown Error
- 检索范围：最近 20 天
- max_results: 100
- arXiv categories: cs.CV, cs.CL, cs.AI
- 关键词数量：N
- 运行时间：YYYY-MM-DD HH:MM:SS

说明：
这通常是 arXiv API 限流、网络超时或请求过于频繁导致。
这不是邮箱配置错误。
```

要求：

1. arXiv 429 时必须发送错误邮件。
2. timeout 时必须发送错误邮件。
3. RuntimeError 时必须发送错误邮件。
4. 不能静默失败。
5. 如果错误邮件也发送失败，必须写入日志 traceback。
6. arXiv 请求失败时返回非 0 exit code 是可以接受的，但必须先尝试发送错误通知邮件。

---

### 3.4 扩展关键词范围

保留原有关键词，同时扩展为以下方向：

- open-vocabulary semantic segmentation
- open-vocabulary segmentation
- vision-language alignment
- image-text alignment
- cross-modal alignment
- multimodal alignment
- unsupervised alignment
- unsupervised embedding alignment
- unsupervised representation alignment
- unpaired image-text alignment
- unpaired vision-language alignment
- distribution matching
- embedding distribution alignment
- embedding translation
- embedding translator
- vector space alignment
- representation alignment
- manifold alignment
- optimal transport alignment
- adversarial alignment

推荐关键词：

```python
KEYWORDS = [
    # open-vocabulary segmentation
    "open vocabulary semantic segmentation",
    "open-vocabulary semantic segmentation",
    "open vocabulary segmentation",
    "open-vocabulary segmentation",

    # vision-language / multimodal alignment
    "vision-language alignment",
    "vision language alignment",
    "image-text alignment",
    "image text alignment",
    "cross-modal alignment",
    "cross modal alignment",
    "multimodal alignment",
    "multi-modal alignment",

    # unsupervised / unpaired alignment
    "unsupervised alignment",
    "unsupervised embedding alignment",
    "unsupervised representation alignment",
    "unsupervised cross-modal alignment",
    "unpaired alignment",
    "unpaired image-text alignment",
    "unpaired vision-language alignment",
    "unpaired multimodal alignment",

    # distribution / geometry / translator
    "distribution matching",
    "embedding distribution alignment",
    "embedding translation",
    "embedding translator",
    "vector space alignment",
    "representation alignment",
    "manifold alignment",
    "optimal transport alignment",
    "adversarial alignment",
]
```

匹配要求：

1. arXiv query 中关键词之间是 OR，不是 AND。
2. Python 二次过滤时，命中任意关键词即可保留。
3. 保留 categories：

```python
CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]
```

4. 不要把所有关键词强制要求同时出现。

---

### 3.5 降低 arXiv 429 风险

当前日志显示多次 HTTP 429，说明请求过于频繁或 arXiv 处于限流状态。需要做以下防护。

#### 3.5.1 保留 retry + exponential backoff

如果已有 `rate_limited_fetch()` 或 `fetch_feed_with_retry()`，请保留并增强。

要求：

1. 捕获 HTTP 429。
2. 捕获 timeout。
3. 捕获网络异常。
4. 使用指数退避，例如 30s、60s、120s、240s。
5. 如果响应头里有 `Retry-After`，优先使用 `Retry-After`。
6. 每次 retry 写日志。

伪代码：

```python
for attempt in range(1, max_retries + 1):
    try:
        return request_url(url)
    except HTTPError as exc:
        if exc.code == 429:
            wait_seconds = get_retry_after(exc) or base_sleep * (2 ** (attempt - 1))
            LOGGER.warning("HTTP 429 from arXiv, retry after %.2fs", wait_seconds)
            time.sleep(wait_seconds)
            continue
        raise
    except TimeoutError:
        wait_seconds = base_sleep * (2 ** (attempt - 1))
        LOGGER.warning("timeout while requesting arXiv, retry after %.2fs", wait_seconds)
        time.sleep(wait_seconds)
        continue
```

#### 3.5.2 避免一次构造超长 query

如果所有关键词放在一个 query 中太长，建议分批查询。

推荐策略：

```python
KEYWORD_BATCH_SIZE = 5
BATCH_SLEEP_SECONDS = 10
```

实现：

```python
for batch in chunked(KEYWORDS, KEYWORD_BATCH_SIZE):
    query = build_query(batch, categories, days)
    result = rate_limited_fetch(query)
    papers.extend(parse_result(result))
    time.sleep(BATCH_SLEEP_SECONDS)
```

然后按 arXiv id 去重：

```python
unique = {}
for paper in papers:
    unique[paper["id"]] = paper
papers = list(unique.values())
```

要求：

1. 每批关键词之间 sleep 8 到 15 秒。
2. 每批最多 5 个关键词。
3. 所有批次结果按 arXiv id 去重。
4. 一个批次失败时可以记录错误并继续其他批次；但如果所有批次都失败，要发送失败邮件。
5. 不要为了分批查询引入复杂架构。

#### 3.5.3 避免频繁 cron

不要在代码里直接编辑 cron，但请在 README 或注释中说明建议频率：

```text
建议每天运行 1 次，最多每 12 小时运行 1 次。
不建议每分钟或每 5 分钟运行，否则容易触发 arXiv HTTP 429。
```

---

### 3.6 run.sh 添加文件锁，避免重复运行

在 `run.sh` 开头添加 flock 锁，防止前一个任务没结束，后一个 cron 又启动。

推荐修改：

```bash
#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/paperfetch.lock"
exec 200>"$LOCK_FILE"

flock -n 200 || {
    echo "Another PaperFetch is already running, exit."
    exit 0
}
```

要求：

1. 如果已有任务运行，新任务直接退出 0。
2. 日志里要能看到：

```text
Another PaperFetch is already running, exit.
```

3. 不要让多个 `run.sh` 同时请求 arXiv。

---

### 3.7 缓存逻辑修复

当前可能存在问题：

- 空结果被缓存。
- 请求失败状态被缓存。
- 修改 days 或 keywords 后仍然读取旧缓存。

请检查 `cache/` 逻辑，确保：

1. 不缓存请求失败结果。
2. 不建议缓存空论文结果；如果缓存空结果，必须带上完整 query hash。
3. 缓存 key 至少包含：
   - date
   - days
   - categories hash
   - keywords hash
   - max_results
4. 修改关键词或 days 后，不能读到旧缓存。
5. `--no-cache` 必须完全绕过读取和写入缓存。

推荐简单做法：

```python
if papers:
    write_cache(cache_path, query, papers)
```

不要缓存空结果：

```python
if not papers:
    LOGGER.info("No papers found, skip writing empty cache.")
```

---

## 4. 推荐的代码结构

### 4.1 主流程 main()

推荐主流程改成：

```python
def main():
    args = parse_args()
    setup_logging(args.log_level)

    days = args.days
    keywords = KEYWORDS
    categories = CATEGORIES

    LOGGER.info("starting PaperFetch days=%s max_results=%s", days, args.max_results)

    try:
        papers = fetch_arxiv_papers(
            keywords=keywords,
            categories=categories,
            days=days,
            max_results=args.max_results,
            no_cache=args.no_cache,
        )
    except Exception as exc:
        LOGGER.exception("Failed to fetch arXiv papers")
        subject = build_failure_subject(days)
        body = build_failure_email_body(
            error=exc,
            keywords=keywords,
            categories=categories,
            days=days,
            max_results=args.max_results,
        )
        try:
            send_email(subject, body)
            LOGGER.info("failure email sent to configured receiver")
        except Exception:
            LOGGER.exception("Failed to send failure notification email")
        return 1

    LOGGER.info("final paper count=%s", len(papers))

    if papers:
        subject = build_success_subject(papers, days)
        body = build_success_email_body(papers, keywords, categories, days)
    else:
        LOGGER.info("No matched papers found, sending empty report email.")
        subject = build_empty_subject(days)
        body = build_empty_email_body(keywords, categories, days)

    send_email(subject, body)
    LOGGER.info("email sent to configured receiver")
    return 0
```

---

### 4.2 邮件构造函数

建议增加：

```python
def build_success_subject(papers, days):
    return f"PaperFetch: 最近 {days} 天找到 {len(papers)} 篇相关论文"
```

```python
def build_empty_subject(days):
    return f"PaperFetch: 最近 {days} 天未找到匹配论文"
```

```python
def build_failure_subject(days):
    return f"PaperFetch: arXiv 请求失败（最近 {days} 天）"
```

```python
def build_empty_email_body(keywords, categories, days):
    ...
```

```python
def build_failure_email_body(error, keywords, categories, days, max_results):
    ...
```

这样代码结构清晰，Codex 后续维护更容易。

---

## 5. 修改边界

请不要做以下事情：

1. 不要重构整个项目。
2. 不要更换邮件发送库，除非当前库确实不可用。
3. 不要把邮箱、授权码、收件人硬编码到 Python 文件里。
4. 不要删除已有日志。
5. 不要让没有论文时静默退出。
6. 不要让 arXiv 请求失败时静默退出。
7. 不要把关键词改成全部 AND 匹配。
8. 不要引入数据库、Web 服务、任务队列。
9. 不要引入复杂配置系统。
10. 不要删除现有命令行参数，除非有明确替代。

---

## 6. 验收测试

修改完成后必须执行以下测试。

### 测试 1：普通运行

```bash
cd /root/code/PaperFetch
bash run.sh
tail -100 log/run.log
```

期望看到：

```text
starting PaperFetch days=20
final paper count=N
email sent to ...
```

如果 `N=0`，也必须看到：

```text
No matched papers found, sending empty report email.
email sent to ...
```

---

### 测试 2：直接运行 Python，跳过缓存

```bash
cd /root/code/PaperFetch
source /root/miniconda3/etc/profile.d/conda.sh
conda activate paperfetch

python PaperFrech_daily_keyword.py --days 20 --max-results 100 --no-cache --log-level DEBUG
```

期望：

1. 程序不报错。
2. 日志显示 `days=20`。
3. 有论文时发送论文列表邮件。
4. 没论文时发送空结果邮件。
5. arXiv 请求失败时发送错误通知邮件。

---

### 测试 3：人为制造空结果

临时把关键词改成一个几乎不可能命中的值：

```python
KEYWORDS = [
    "zzzz_nonexistent_paperfetch_keyword_test_12345"
]
```

运行：

```bash
python PaperFrech_daily_keyword.py --days 20 --max-results 100 --no-cache --log-level DEBUG
```

期望：

```text
final paper count=0
No matched papers found, sending empty report email.
email sent to ...
```

邮箱应该收到“未找到匹配论文”的邮件。

测试完成后恢复正常关键词。

---

### 测试 4：人为模拟 arXiv 请求失败

可以临时把 arXiv API URL 改成一个错误地址，或者在 `rate_limited_fetch()` 中临时抛出：

```python
raise RuntimeError("mock arXiv failure for test")
```

运行：

```bash
python PaperFrech_daily_keyword.py --days 20 --max-results 100 --no-cache --log-level DEBUG
```

期望：

```text
Failed to fetch arXiv papers
failure email sent to configured receiver
```

邮箱应该收到“PaperFetch: arXiv 请求失败”的邮件。

测试完成后恢复正常代码。

---

### 测试 5：检查 run.sh 文件锁

手动开两个终端同时运行：

```bash
bash run.sh
```

期望第二个任务直接退出：

```text
Another PaperFetch is already running, exit.
```

并且不会重复请求 arXiv。

---

## 7. 推荐 cron 设置

请不要高频运行。

推荐每天一次：

```bash
30 18 * * * cd /root/code/PaperFetch && bash run.sh
```

或者最多每 12 小时一次：

```bash
30 9,21 * * * cd /root/code/PaperFetch && bash run.sh
```

不推荐：

```bash
* * * * * cd /root/code/PaperFetch && bash run.sh
```

因为每分钟运行容易触发 arXiv HTTP 429。

---

## 8. 最终交付说明

完成修改后，请输出：

1. 修改了哪些文件。
2. 每个文件具体修改内容。
3. 当前默认 days 是否为 20。
4. 当前关键词列表是否包含：
   - open vocabulary segmentation
   - unsupervised alignment
   - unsupervised embedding alignment
   - unpaired image-text alignment
   - distribution matching
   - embedding translator
   - vector space alignment
   - manifold alignment
   - optimal transport alignment
   - adversarial alignment
5. 空结果是否会发送邮件。
6. arXiv 429 / timeout 是否会发送错误邮件。
7. run.sh 是否已经添加 flock 防重复运行。
8. 如何运行测试。
9. 如何检查日志。

最终必须满足：

```text
bash run.sh 后，用户一定能收到一封邮件：
- 成功找到论文：论文列表邮件。
- 查询成功但 0 篇：空结果通知邮件。
- arXiv 请求失败：错误通知邮件。
```
