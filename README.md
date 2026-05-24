# Bangumi Explorer

基于 [bangumi.tv](https://bgm.tv) 数据的动漫评分搜索与分析工具。

## 功能

- **全量搜索** — 本地 SQLite 数据库收录全部 28,000+ 部动漫，秒级响应
- **模糊搜索** — 支持中文名、日文名部分匹配
- **标签筛选** — 39,000+ 标签库，输入自动补全，5% 阈值过滤误标
- **多维过滤** — 播出时间（精确到月）、排名范围
- **灵活排序** — 排名 / 评分 / 时间，升序降序可选
- **精确评分** — 从评分分布加权计算至小数点后两位，非 API 四舍五入值
- **海报展示** — 直接引用 bangumi CDN，懒加载
- **一键跳转** — 标题链接到 bgm.tv 条目页，标签点击添加到筛选
- **自动更新** — 后台定时刷新评分与标签数据

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 初始化数据库（需 bangumi-data 仓库于 /tmp/bangumi-data-investigation）
python sync_db.py

# 启动服务
python app.py
```

访问 `http://localhost:8080`

目前基于阿里云试用云服务器部署了该网页，ip为 39.105.76.120，但是没有长期运维的打算

## 数据来源

- [bangumi-data](https://github.com/bangumi-data/bangumi-data) — 条目 ID 索引
- [bangumi API](https://github.com/bangumi/api) — 条目详情、评分分布、标签

## 技术栈

Python · Flask · SQLite · Vanilla JS

## 项目结构

```
├── app.py              # Flask 后端，本地搜索 + API 回退
├── database.py         # SQLite 数据库操作，搜索与过滤逻辑
├── sync_db.py          # 全量 / 增量数据同步脚本
├── templates/
│   └── index.html      # 前端 UI
└── requirements.txt
```
