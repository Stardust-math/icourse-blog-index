# USTC iCourse Blog Index

一个谨慎、可审计且自动更新的非官方评课社区博客索引。

本项目从 [`https://icourse.club/user/{id}`](https://icourse.club/user/1) 的公开资料区读取用户自行填写的博客链接。完整数据会记录每个已经检查的数字 ID；下方索引只展示当前已确认存在博客的用户。

> [!IMPORTANT]
> 本仓库不是评课社区或中国科学技术大学的官方项目，也不表示站方已经正式授权。爬虫单线程、限速运行，并在站点规则或访问状态不允许时停止。详细范围、限制与更正方式见 [DATA_NOTICE.md](DATA_NOTICE.md)。

<!-- BEGIN GENERATED INDEX -->
## 数据状态

| 指标 | 当前值 |
|---|---:|
| 初始化状态 | `in_progress` |
| 已记录 ID | 500 |
| 最高已尝试 ID | 500 |
| 最高已确认用户 ID | 500 |
| 已确认博客 | 21 |
| 公开 / 隐藏 / 不存在 / 未决 | 497 / 1 / 1 / 1 |
| 最近成功更新 | 2026-08-30T05:12:05Z |

## 博客索引

| 用户 ID | 博客 | 可访问性 | 资料最后确认 |
| ---: | --- | --- | --- |
| [1](https://icourse.club/user/1) | [jenny42.com](http://jenny42.com/) | 未检查 | 2026-08-30 |
| [2](https://icourse.club/user/2) | [01.me](https://01.me/) | 未检查 | 2026-08-30 |
| [4](https://icourse.club/user/4) | [zhengzihan.com](https://zhengzihan.com/) | 未检查 | 2026-08-30 |
| [10](https://icourse.club/user/10) | [cvhc.cc](https://cvhc.cc/) | 未检查 | 2026-08-30 |
| [14](https://icourse.club/user/14) | [ibat.me](http://ibat.me/) | 未检查 | 2026-08-30 |
| [15](https://icourse.club/user/15) | [wzhd.gitcafe.io](http://wzhd.gitcafe.io/) | 未检查 | 2026-08-30 |
| [16](https://icourse.club/user/16) | [home.ustc.edu.cn/~lyishuai](http://home.ustc.edu.cn/~lyishuai) | 未检查 | 2026-08-30 |
| [59](https://icourse.club/user/59) | [kuriyamamika.blogspot.jp](http://kuriyamamika.blogspot.jp/) | 未检查 | 2026-08-30 |
| [64](https://icourse.club/user/64) | [home.ustc.edu.cn/~mouzq](http://home.ustc.edu.cn/~mouzq/) | 未检查 | 2026-08-30 |
| [116](https://icourse.club/user/116) | [ustclyh.github.io](https://ustclyh.github.io/) | 未检查 | 2026-08-30 |
| [121](https://icourse.club/user/121) | [www.zhangjy9610.me/index-cn.html](https://www.zhangjy9610.me/index-cn.html) | 未检查 | 2026-08-30 |
| [190](https://icourse.club/user/190) | [peijunz.github.io](http://peijunz.github.io/) | 未检查 | 2026-08-30 |
| [316](https://icourse.club/user/316) | [www.zhihu.com/people/gai-nie-39](http://www.zhihu.com/people/gai-nie-39) | 未检查 | 2026-08-30 |
| [320](https://icourse.club/user/320) | [ewind.us](http://ewind.us/) | 未检查 | 2026-08-30 |
| [385](https://icourse.club/user/385) | [eipi10ydz.github.io](http://eipi10ydz.github.io/) | 未检查 | 2026-08-30 |
| [397](https://icourse.club/user/397) | [parlorpink.weebly.com](http://parlorpink.weebly.com/) | 未检查 | 2026-08-30 |
| [444](https://icourse.club/user/444) | [home.ustc.edu.cn/~ming9510](http://home.ustc.edu.cn/~ming9510) | 未检查 | 2026-08-30 |
| [450](https://icourse.club/user/450) | [0x01.me](http://0x01.me/) | 未检查 | 2026-08-30 |
| [455](https://icourse.club/user/455) | [ustczf.com](http://ustczf.com/) | 未检查 | 2026-08-30 |
| [462](https://icourse.club/user/462) | [rat-racer.github.io](https://rat-racer.github.io/) | 未检查 | 2026-08-30 |
| [470](https://icourse.club/user/470) | [home.ustc.edu.cn/~qzr](http://home.ustc.edu.cn/~qzr) | 未检查 | 2026-08-30 |
<!-- END GENERATED INDEX -->

## 数据在哪里

`README.md` 只是自动生成的浏览视图，不是权威数据源。完整数据结构如下：

```text
data/
├── manifest.json          # 整体进度与统计
├── users/                 # 每 1,000 个 ID 一个 JSONL 分片
├── blogs.csv              # 当前已确认博客的便携导出
├── link-health.jsonl      # 按规范化 URL 去重的可访问性结果
├── changes/               # 已确认的状态及博客变更事件
└── runs/                  # 每次运行的审计摘要
state/
└── crawler.json           # 可恢复的初始化和维护游标
```

所有尝试过的 ID 都会保存在 `data/users/`，无论它是否有博客。记录严格区分：

- `public + present`：公开主页且有已确认博客；
- `public + absent`：公开主页且已确认没有博客；
- `hidden + unknown`：主页明确隐藏，无法判断博客；
- `missing + unknown`：正文明确显示用户不存在，包括 HTTP 200 的软 404；
- `unknown + unknown`：网络、限流、拦截或解析错误，不能下结论。

一次请求失败不会删除以前确认的博客。新增、删除和修改都要经过二次一致确认；冲突观测会暂存，稍后复查。仓库不会保存原始 HTML，也不会收集邮箱、学号、用户名、简介、点评或关注关系。字段的精确定义见 [DATA_NOTICE.md](DATA_NOTICE.md)。

## 在 GitHub 上初始化

推荐使用公开仓库的标准 GitHub-hosted runner。初始化不需要服务器、数据库、Git LFS、OpenAI API Key、GitHub PAT 或仓库 Secret。公开仓库的标准 runner 按 GitHub 当前政策可以免费使用，但仍应以 [GitHub Actions 当前计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)为准。

1. 新建一个**公开** GitHub 仓库，将本项目全部文件放在仓库根目录后推送。
2. 打开仓库 **Settings → Actions → General**，允许 Actions 运行，并将 **Workflow permissions** 设为 **Read and write permissions**。
3. 打开 **Actions → Bootstrap dataset → Run workflow**，手动启动一次。
4. 等待七个严格串行的任务完成。每个任务最多处理 4,000 个 ID，并定期提交检查点；中断后重新运行同一工作流即可续跑。
5. 初始化完成后，`Update dataset` 每天自动执行，无需再次手动启动 Bootstrap。

正常启动和普通断线续跑时，始终保持 `resume_paused` 未勾选。只有日志明确显示安全暂停、维护者已经查明并解决原因后，才可勾选它重新运行；爬虫仍会先复查当前 `robots.txt` 和活动中的拦截，不能借此绕过禁止规则。

如果默认分支启用了禁止 `github-actions[bot]` 推送的分支保护，工作流无法保存数据。请为该工作流允许写入，或在初始化和自动维护期间使用不阻止 Actions 提交的规则。请勿同时手动编辑生成的数据文件。

初始化保持单线程，请求间隔随机为 2.5–3.5 秒。按当前约 2.34 万个用户 ID 估算，通常需要约 24–36 小时，实际时间取决于站点响应、退避、GitHub 调度和初始化期间新增的用户。工作流不会在第一个缺失 ID 停止，而会跨过编号空洞，并在前沿之后连续缺失区间得到复核后结束。

每次 `Bootstrap dataset` 最多安排 7 × 4,000 个 ID；如果未来编号前沿超过单次容量，工作流会保留 `next_id`，再次手动运行即可继续，不需要改代码或清空数据。仓库只保存分片 JSONL 和小型派生文件，不保存原始网页；以当前规模预计是普通 Git 仓库可以直接承受的几十 MB 级文本数据，无需 Git LFS。日常任务只处理有上限的用户和链接批次，避免每天重写全量数据。若将来 ID 规模增加到当前的数倍，应根据实际仓库历史体积和 Actions 用量降低复查批次或归档旧审计日志，而不是提高抓取并发。

## 自动更新

初始化和更新工作流使用同一个 `concurrency` 组，因此不会并发访问源站。

- `Bootstrap dataset`：仅手动启动；七个串行任务，每个任务最长 5 小时 30 分钟。
- `Update dataset`：每天 `19:37 UTC` 触发，即北京时间/新加坡时间次日 `03:37`；也可以手动启动。
- 日常更新：探测新注册 ID，轮转复查既有记录，优先重试临时错误，并检查有限数量的博客链接。
- README、CSV、manifest、变更日志和运行日志都由工作流重新生成或更新，并由 `github-actions[bot]` 提交。

GitHub 的定时工作流可能延迟；公开仓库长期无活动时也可能被自动停用。游标会保留进度，不会因某天未运行而跳过既有 ID；若工作流被停用，在 Actions 页面重新启用即可。

## 本地使用与验收

需要 Python 3.12 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
python -m icourse_blog_index validate
```

常用命令：

```bash
# 真实网络请求必须在 User-Agent 中提供公开仓库地址，便于站方联系
export ICOURSE_REPOSITORY_URL="https://github.com/OWNER/REPOSITORY"

# 单个用户的实时诊断；不写入正式数据
python -m icourse_blog_index inspect-user 11706

# 处理一段可恢复的初始化任务
python -m icourse_blog_index bootstrap-chunk \
  --max-ids 250 \
  --time-budget-seconds 1200

# 仅在查明并解决安全暂停原因后显式恢复；仍会重新执行安全检查
python -m icourse_blog_index resume \
  --acknowledge "已查明并解决本次暂停原因"

# 执行一轮增量维护和有限外链检查
python -m icourse_blog_index update
python -m icourse_blog_index check-links --max-links 200

# 重新生成视图并验证整个数据集
python -m icourse_blog_index render
python -m icourse_blog_index validate
```

请将示例中的 `OWNER/REPOSITORY` 替换为实际的公开仓库。GitHub Actions 工作流会根据当前仓库自动设置同一个地址，本地执行联网命令时则必须设置该环境变量，或在子命令前传入全局选项 `--repository-url https://github.com/OWNER/REPOSITORY`。

验收用户 `11706` 时，直接页面当前应解析并规范化为 `https://stardust-math.pages.dev/`。这只是检查解析器与缓存处理的一次性基线，地址不会硬编码在数据逻辑中，用户以后正常修改链接时仍会按二次确认规则更新。

## 开发原则

- 只读取公开用户页及博客 URL 的有限 HTTP 响应，不登录、不执行博客 JavaScript。
- 每次抓取前读取 `robots.txt`，尊重 `Retry-After`，遇到持续拦截、限流或结构异常便停止。
- 只以用户页直接响应为资料来源，不采用搜索摘要、网页存档或 AI 缓存。
- 博客健康检查拒绝回环、内网、link-local 等地址，并在每次重定向后重新验证目标。
- 初始化基线不生成数万条伪“变更”；后续只有已确认变化才写入 `data/changes/`。
- 自动生成内容必须通过 schema、跨文件一致性和 README 派生视图检查后才能提交。

软件代码采用 [MIT License](LICENSE)。该许可证不延伸至源网站、用户资料或第三方博客内容。
