# dature
一键下载保存您的新浪博客内容（标题、正文、时间、分类、标签、图片），生成 HTML 文件，方便本地长期备份。

## 使用方式

1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

2. 准备配置

复制 `config.example.json` 为 `config.json`，并填写：

- `uid`: 你的博客 uid（数字）
- `cookie`: 登录后的浏览器 Cookie（若列表页/正文页需要登录）
- `list_url_template`: 博客列表页模板，必须包含 `{uid}` 和 `{page}`

示例：`https://blog.sina.com.cn/s/articlelist_{uid}_0_{page}.html`

3. 运行导出

```bash
python sina_blog_backup.py --config config.json
```

导出结果会生成在 `output/` 目录，包含 `index.html` 和每篇文章的 HTML 文件。

## 解析不准时怎么办

不同博客主题可能导致结构差异。可以在 `config.json` 里填写 `selectors`：

- `list_link`: 列表页文章链接的 CSS 选择器
- `title` / `time` / `category` / `tags` / `content`: 文章页的 CSS 选择器数组

填了后会优先用你提供的选择器。未填写则使用内置的自动识别规则。
