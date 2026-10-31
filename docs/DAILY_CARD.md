# 电影日签

本地生成一张竖版早安海报：日期、中文片名、导演与年份、一句中文金句、电影画面和二维码。它是写作工作台旁边的小工具，不写入个人说明书或稿件，不登录微信，也不替你发朋友圈。

云端 Worker 不包含这个功能，也没有定时任务。

## 准备

需要 Python 3.11、3.12 或 3.13，以及 Pillow。二维码可选：

```bash
python -m pip install pillow qrcode
```

中文使用 [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC)（SIL Open Font License 1.1）。首次生成时，若本机没有可用中文字体，程序会把 Regular、Bold、Black 三个字重下载到用户缓存目录（一般是 `~/.cache/moments-agent/fonts`），不放进仓库。已经装了思源黑体、文泉驿微米黑或微软雅黑时会直接使用。也可以自己指定：

```bash
export DAILY_CARD_FONT=/path/to/NotoSansSC-Regular.ttf
export DAILY_CARD_FONT_BOLD=/path/to/NotoSansSC-Bold.ttf
export DAILY_CARD_FONT_BLACK=/path/to/NotoSansSC-Black.ttf
```

`DAILY_CARD_ALLOW_DOWNLOAD=0` 会禁止这次下载。

## TMDB 密钥

片名、导演、年份和剧照来自 [TMDB](https://www.themoviedb.org/)。免费 API Key 在登录后的 [API 设置](https://www.themoviedb.org/settings/api) 里申请，只放在环境变量里：

```bash
export TMDB_API_KEY=你的密钥
```

不要把密钥写进仓库、稿件或命令行历史里长期保存的脚本。本工具会在海报和说明里注明使用了 TMDB API，且未获 TMDB 或片方背书。剧照版权属于权利人，只适合个人预览和自行分享前再核对使用权，不随 MIT 发行包分发。

没有密钥、密钥被拒绝或网络失败时，程序改用 `daily_card/fixtures/movies.json` 里的固定片单，并画一张生成画面。海报底部会写 `GENERATED STILL`，避免把示例画面当成影片剧照。

## 金句

默认向 [一言](https://hitokoto.cn/) 要一句电影分类（`c=h`）的短句，太长或失败时再试其他分类，最后用仓库里的本地句库。本地句库是本仓库原文，不是影片台词。海报上的 `QUOTE FROM` 用来标这一句的出处。

## 生成

在仓库根目录执行：

```bash
python -m daily_card generate --date 2026-10-31
```

默认把 PNG 和同名 JSON 写到 `artifacts/daily-cards/`。JSON 记录片名、金句、来源和是否用了密钥，不包含密钥本身。

只想离线看版式：

```bash
python -m daily_card generate --date 2026-10-31 --fixture --output /tmp/daily-card.png
```

`--fixture` 不访问 TMDB，也不访问一言。日期决定片单和本地金句，同一天重复运行会得到同一部电影。片单顺序参与种子，新片请加在 `movies.json` 末尾。

二维码指向 `https://www.themoviedb.org/movie/<编号>`，方便查看公开页面。未安装 `qrcode` 时，右下角保留一个方框，版式不变。

## 和写作工作台的边界

选稿、改稿、保留和已发确认仍走原来的工作台。日签不会进入素材库，也不会因为生成了海报就被当成已发表。
