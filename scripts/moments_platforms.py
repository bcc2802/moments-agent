"""One ordered platform policy for current collection and historical reads.

Archived platform identities are never an authorization to collect new data.
Keep their original order so old public pool hashes remain reproducible.
"""

PLATFORMS = {
    "baidu": ("百度", "baidu.com"),
    "nowcoder": ("牛客", "nowcoder.com"),
    "hupu": ("虎扑", "hupu.com"),
    "juejin": ("稀土掘金", "juejin.cn"),
    "sspai": ("少数派", "sspai.com"),
    "weibo": ("微博", "weibo.com"),
    "zhihu": ("知乎", "zhihu.com"),
}
SOURCE_IDS = tuple(PLATFORMS)
SOURCE_DOMAINS = {key: domain for key, (_, domain) in PLATFORMS.items()}

# Read-only compatibility for menus, published notes, media and public runs
# created before the seven-platform policy. New collections must use SOURCE_IDS.
ARCHIVE_PLATFORMS = {
    **PLATFORMS,
    "douyin": ("抖音", "douyin.com"),
    "bilibili-hot-search": ("Bilibili 热搜", "bilibili.com"),
}
HISTORICAL_SOURCE_IDS = (
    "douyin", "baidu", "bilibili-hot-search", "hupu", "juejin", "sspai", "weibo", "zhihu",
)
