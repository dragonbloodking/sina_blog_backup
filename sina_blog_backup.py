import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from requests.utils import get_encodings_from_content
from bs4 import BeautifulSoup


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def load_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_cookie(config):
    cookie = config.get("cookie", "").strip()
    if cookie:
        return cookie
    cookie_file = config.get("cookie_file", "").strip()
    if cookie_file and os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def build_session(cookie, user_agent):
    session = requests.Session()
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    if cookie:
        headers["Cookie"] = cookie
    session.headers.update(headers)
    return session


def normalize_encoding(response):
    if response.encoding and response.encoding.lower() != "iso-8859-1":
        return

    content_head = response.content[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=['\"]?([a-zA-Z0-9_-]+)", content_head, re.I)
    if match:
        response.encoding = match.group(1)
        return

    encodings = get_encodings_from_content(content_head)
    if encodings:
        response.encoding = encodings[0]
        return

    response.encoding = response.apparent_encoding or "utf-8"


def fetch_text(session, url, timeout, verify_ssl):
    resp = session.get(url, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()
    normalize_encoding(resp)
    return resp.text


def is_article_url(url, regex_pattern=None):
    if regex_pattern:
        return re.search(regex_pattern, url) is not None
    if "blog.sina.com.cn/s/blog_" in url:
        return True
    return re.search(r"blog\.sina\.com\.cn/s/blog_[0-9a-z]+\.html", url) is not None


def extract_article_links(list_html, base_url, config):
    soup = BeautifulSoup(list_html, "html.parser")
    links = set()
    selector = config.get("selectors", {}).get("list_link", "")
    regex_pattern = config.get("article_url_regex", "")

    if selector:
        for a in soup.select(selector):
            href = a.get("href", "").strip()
            if not href:
                continue
            full = urljoin(base_url, href)
            if is_article_url(full, regex_pattern):
                links.add(full)
        return links

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        full = urljoin(base_url, href)
        if is_article_url(full, regex_pattern):
            links.add(full)
    return links


def pick_text(soup, selectors):
    for sel in selectors:
        if not sel:
            continue
        if sel.startswith("meta:"):
            prop = sel.split(":", 1)[1]
            node = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop}
            )
            if node and node.get("content"):
                return node["content"].strip()
            continue
        node = soup.select_one(sel)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return ""


def pick_html(soup, selectors):
    for sel in selectors:
        if not sel:
            continue
        node = soup.select_one(sel)
        if node:
            return node
    return None


def guess_title(soup):
    title = pick_text(
        soup,
        [
            "h2.titName",
            "h1",
            "meta:og:title",
            "meta:title",
            "title",
        ],
    )
    if title:
        return title.replace("- 新浪博客", "").strip()
    return ""


def guess_time(soup):
    text = pick_text(
        soup,
        [
            "span.time",
            "span.time SG_txtc",
            "meta:article:published_time",
            "meta:og:pubdate",
        ],
    )
    if not text:
        body_text = soup.get_text("\n", strip=True)
        match = re.search(
            r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            body_text,
        )
        if match:
            text = match.group(0)
    return text.strip()


def guess_category(soup):
    category = pick_text(
        soup,
        [
            "a[rel='category tag']",
            "a.blogCategory",
            "span.category",
        ],
    )
    if category:
        return category
    raw = soup.get_text(" ", strip=True)
    match = re.search(r"分类[:：]\s*([^\s]+)", raw)
    return match.group(1) if match else ""


def guess_tags(soup):
    tags = []
    for a in soup.select("a[rel='tag'], .blog_tag a, .tag a"):
        text = a.get_text(strip=True)
        if text and text not in tags:
            tags.append(text)
    if tags:
        return tags
    raw = soup.get_text(" ", strip=True)
    match = re.search(r"标签[:：]\s*([^\n]+)", raw)
    if match:
        segment = match.group(1).strip()
        if len(segment) <= 120:
            parts = re.split(r"[,\s]+", segment)
            return [p for p in parts if p]
    return []


def is_tag_block(node):
    classes = " ".join(node.get("class", []))
    if "articalTag" in classes:
        return True
    if node.find(class_=re.compile(r"(blog_tag|blog_class)", re.I)):
        return True
    text = node.get_text(" ", strip=True)
    if "标签" in text and "分类" in text and len(text) < 300:
        return True
    return False


def is_noise_node(node):
    node_id = (node.get("id") or "").lower()
    if re.search(r"(nav|menu|footer|foot|side|comment|share|recommend)", node_id):
        return True
    classes = " ".join(node.get("class", [])).lower()
    if re.search(r"(nav|menu|footer|foot|side|comment|share|recommend|articaltag)", classes):
        return True
    return False


def guess_content_node(soup, selectors):
    node = pick_html(soup, selectors)
    if node:
        return node

    for sel in [
        "div#sina_keyword_ad_area2",
        "div.articalContent",
        "div#articlebody",
        "div#artibody",
    ]:
        node = soup.select_one(sel)
        if node and not is_tag_block(node):
            return node

    node = soup.find("div", id=re.compile(r"sina_keyword_ad_area", re.I))
    if node and not is_tag_block(node):
        return node

    node = soup.find(
        "div", class_=re.compile(r"(articalContent|article|post|content)", re.I)
    )
    if node and not is_tag_block(node):
        return node

    candidates = soup.find_all("div")
    if not candidates:
        return None
    candidates = [c for c in candidates if not is_noise_node(c)]
    if not candidates:
        return None
    candidates.sort(key=lambda x: len(x.get_text(" ", strip=True)), reverse=True)
    return candidates[0]


def clean_content(node):
    for tag in node.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for tag in node.select(
        ".articalTag, .blog_tag, .blog_class, .share, .shareBtn, #share, .comment, #comment"
    ):
        tag.decompose()
    return node


def safe_filename(name, fallback):
    name = name.strip() or fallback
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return fallback
    return name


def parse_article(html_text, url, config):
    soup = BeautifulSoup(html_text, "html.parser")
    selectors = config.get("selectors", {})

    title = pick_text(soup, selectors.get("title", [])) or guess_title(soup)
    published_at = pick_text(soup, selectors.get("time", [])) or guess_time(soup)
    category = pick_text(soup, selectors.get("category", [])) or guess_category(soup)
    tags = selectors.get("tags", [])
    if tags:
        tag_list = []
        for sel in tags:
            for a in soup.select(sel):
                text = a.get_text(strip=True)
                if text and text not in tag_list:
                    tag_list.append(text)
        tags = tag_list
    else:
        tags = guess_tags(soup)

    content_node = guess_content_node(soup, selectors.get("content", []))
    content_html = ""
    if content_node:
        content_html = str(clean_content(content_node))

    return {
        "title": title,
        "published_at": published_at,
        "category": category,
        "tags": tags,
        "url": url,
        "content_html": content_html,
    }


def download_images(content_html, article_url, session, output_dir, timeout, verify_ssl):
    soup = BeautifulSoup(content_html, "html.parser")
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    cache = {}

    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or ""
        )
        src = src.strip()
        if not src:
            continue
        full_url = urljoin(article_url, src)
        if full_url in cache:
            img["src"] = cache[full_url]
            continue

        filename_hash = hashlib.sha1(full_url.encode("utf-8")).hexdigest()[:12]
        ext = os.path.splitext(urlparse(full_url).path)[1]
        if not ext or len(ext) > 5:
            ext = ".jpg"
        filename = f"{filename_hash}{ext}"
        file_path = os.path.join(images_dir, filename)

        try:
            if not os.path.exists(file_path):
                resp = session.get(full_url, timeout=timeout, verify=verify_ssl)
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    f.write(resp.content)
            rel_path = os.path.join("images", filename).replace("\\", "/")
            img["src"] = rel_path
            cache[full_url] = rel_path
        except requests.RequestException:
            continue

    return str(soup)


def render_post_html(article):
    title = html.escape(article["title"] or "Untitled")
    published_at = html.escape(article.get("published_at", ""))
    category = html.escape(article.get("category", ""))
    tags = article.get("tags", [])
    tags_html = " ".join(
        f"<span class='tag'>{html.escape(t)}</span>" for t in tags
    )
    content = article.get("content_html", "")

    meta_line = " ".join(
        part
        for part in [
            f"<span>{published_at}</span>" if published_at else "",
            f"<span>分类: {category}</span>" if category else "",
        ]
        if part
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 0; background: #f6f7fb; color: #1f2430; }}
    .wrap {{ max-width: 920px; margin: 40px auto; background: #fff; padding: 32px 40px; border-radius: 16px; box-shadow: 0 10px 24px rgba(15, 18, 30, 0.08); }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    .meta {{ color: #5c667a; font-size: 14px; display: flex; gap: 16px; flex-wrap: wrap; }}
    .tags {{ margin-top: 12px; }}
    .tag {{ display: inline-block; padding: 4px 8px; margin-right: 6px; margin-bottom: 6px; background: #eef1f7; border-radius: 999px; font-size: 12px; color: #4b5563; }}
    .content {{ margin-top: 24px; line-height: 1.8; font-size: 16px; }}
    .content img {{ max-width: 100%; height: auto; }}
    a {{ color: #3366ff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{title}</h1>
    <div class="meta">{meta_line}</div>
    <div class="tags">{tags_html}</div>
    <div class="content">{content}</div>
    <p><a href="{html.escape(article.get("url", ""))}" target="_blank">原文链接</a></p>
  </div>
</body>
</html>"""


def render_index_html(articles):
    items = []
    for item in articles:
        title = html.escape(item.get("title") or "Untitled")
        published = html.escape(item.get("published_at") or "")
        category = html.escape(item.get("category") or "")
        tags = item.get("tags") or []
        tags_html = " ".join(
            f"<span class='tag'>{html.escape(t)}</span>" for t in tags
        )
        items.append(
            f"""<article class="card">
  <h2><a href="{html.escape(item['file'])}">{title}</a></h2>
  <div class="meta">{published} {f"· {category}" if category else ""}</div>
  <div class="tags">{tags_html}</div>
</article>"""
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sina Blog Backup</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 0; background: #f2f4f8; color: #1f2430; }}
    .wrap {{ max-width: 960px; margin: 40px auto; padding: 0 24px 40px; }}
    h1 {{ margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }}
    .card {{ background: #fff; padding: 20px; border-radius: 14px; box-shadow: 0 8px 18px rgba(20, 26, 38, 0.08); }}
    .card h2 {{ font-size: 18px; margin: 0 0 10px; }}
    .meta {{ color: #5c667a; font-size: 13px; margin-bottom: 10px; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag {{ display: inline-block; padding: 3px 8px; background: #eef1f7; border-radius: 999px; font-size: 12px; color: #4b5563; }}
    a {{ color: #3366ff; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>新浪博客备份</h1>
    <div class="grid">
      {''.join(items)}
    </div>
  </div>
</body>
</html>"""


def render_progress_html(state):
    phase = html.escape(state.get("phase", ""))
    current = state.get("current", 0)
    total = state.get("total", 0)
    title = html.escape(state.get("title", ""))
    percent = 0
    if total:
        percent = int(current * 100 / total)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="2" />
  <title>备份进度</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", sans-serif; margin: 0; background: #f4f6fb; color: #1f2430; }}
    .wrap {{ max-width: 860px; margin: 40px auto; background: #fff; padding: 28px 32px; border-radius: 16px; box-shadow: 0 10px 24px rgba(15, 18, 30, 0.08); }}
    .bar {{ height: 12px; background: #e7eaf2; border-radius: 999px; overflow: hidden; margin: 12px 0 6px; }}
    .bar > span {{ display: block; height: 100%; width: {percent}%; background: linear-gradient(90deg, #4f7cff, #6aa8ff); }}
    .meta {{ color: #5c667a; font-size: 14px; margin-top: 4px; }}
    .title {{ margin-top: 12px; font-size: 16px; line-height: 1.5; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2ff; color: #2f4bdd; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>新浪博客备份进度</h1>
    <div class="badge">{phase}</div>
    <div class="bar"><span></span></div>
    <div class="meta">{current} / {total} （{percent}%）</div>
    <div class="title">当前文章：{title}</div>
  </div>
</body>
</html>"""


def write_progress(output_dir, state):
    os.makedirs(output_dir, exist_ok=True)
    progress_json = os.path.join(output_dir, "progress.json")
    progress_html = os.path.join(output_dir, "progress.html")
    with open(progress_json, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    with open(progress_html, "w", encoding="utf-8") as f:
        f.write(render_progress_html(state))


def print_progress(current, total, title):
    width = 28
    percent = 0
    if total:
        percent = current / total
    filled = int(width * percent)
    bar = "#" * filled + "-" * (width - filled)
    title = (title or "")[:48]
    sys.stdout.write(f"\r[{bar}] {current}/{total} {int(percent*100):3d}% {title}")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Sina Blog Backup")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    config = load_config(args.config)
    uid = config.get("uid", "").strip()
    if not uid:
        raise ValueError("uid is required in config")

    list_url_template = config.get("list_url_template", "").strip()
    if not list_url_template:
        raise ValueError("list_url_template is required in config")

    cookie = read_cookie(config)
    if not cookie:
        print("Warning: cookie is empty. Some content might require login.")

    session = build_session(cookie, config.get("user_agent", DEFAULT_USER_AGENT))
    timeout = config.get("timeout_sec", 15)
    verify_ssl = config.get("verify_ssl", True)
    delay = float(config.get("request_delay_sec", 1))
    max_pages = int(config.get("max_pages", 50))
    output_dir = config.get("output_dir", "output")
    download_imgs = bool(config.get("download_images", True))
    save_raw = bool(config.get("save_raw_html", False))

    os.makedirs(output_dir, exist_ok=True)
    posts_dir = os.path.join(output_dir, "posts")
    os.makedirs(posts_dir, exist_ok=True)

    article_urls = []
    seen = set()
    write_progress(
        output_dir,
        {"phase": "collecting", "current": 0, "total": 0, "title": "列表抓取中"},
    )
    for page in range(1, max_pages + 1):
        list_url = list_url_template.format(uid=uid, page=page)
        print(f"Fetching list page {page}: {list_url}")
        try:
            html_text = fetch_text(session, list_url, timeout, verify_ssl)
        except requests.RequestException as exc:
            print(f"Failed to fetch list page {page}: {exc}")
            break

        links = extract_article_links(html_text, list_url, config)
        new_links = [link for link in links if link not in seen]
        if not new_links:
            print("No new article links found. Stop.")
            break
        for link in new_links:
            seen.add(link)
            article_urls.append(link)
        time.sleep(delay)

    articles = []
    total_articles = len(article_urls)
    write_progress(
        output_dir,
        {"phase": "downloading", "current": 0, "total": total_articles, "title": ""},
    )
    for idx, url in enumerate(article_urls, start=1):
        print_progress(idx, total_articles, url)
        try:
            html_text = fetch_text(session, url, timeout, verify_ssl)
        except requests.RequestException as exc:
            sys.stdout.write("\n")
            print(f"Failed to fetch article: {exc}")
            continue

        if save_raw:
            raw_path = os.path.join(
                output_dir, "raw", f"raw_{idx:04d}.html"
            )
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(html_text)

        article = parse_article(html_text, url, config)
        if download_imgs and article.get("content_html"):
            article["content_html"] = download_images(
                article["content_html"],
                url,
                session,
                output_dir,
                timeout,
                verify_ssl,
            )

        date_part = ""
        if article.get("published_at"):
            match = re.search(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", article["published_at"])
            if match:
                date_part = match.group(0).replace("/", "-").replace(".", "-")
                try:
                    date_part = datetime.strptime(date_part, "%Y-%m-%d").strftime(
                        "%Y%m%d"
                    )
                except ValueError:
                    date_part = ""

        base_name = safe_filename(article.get("title", ""), f"post_{idx:04d}")
        if date_part:
            base_name = f"{date_part}_{base_name}"
        filename = f"{base_name}.html"
        file_path = os.path.join(posts_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(render_post_html(article))

        articles.append(
            {
                "title": article.get("title"),
                "published_at": article.get("published_at"),
                "category": article.get("category"),
                "tags": article.get("tags"),
                "file": os.path.join("posts", filename).replace("\\", "/"),
            }
        )

        write_progress(
            output_dir,
            {
                "phase": "downloading",
                "current": idx,
                "total": total_articles,
                "title": article.get("title", ""),
                "url": url,
            },
        )
        time.sleep(delay)

    sys.stdout.write("\n")
    index_html = render_index_html(articles)
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    write_progress(
        output_dir,
        {
            "phase": "done",
            "current": total_articles,
            "total": total_articles,
            "title": "备份完成",
        },
    )
    print(f"Done. Output: {output_dir}")


if __name__ == "__main__":
    main()
