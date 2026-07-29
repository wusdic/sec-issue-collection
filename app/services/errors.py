"""异常 → 人能看懂的一行原因(页面只显示这一行,必须说清"到底为什么失败")。

此前失败信息存的是 traceback 的一段,页面再截前 300 字,结果用户看到的是
"...cursor.execute(statement, parameters)  sqlalchemy.exc.OperationalE" —— 恰好在真正的
错误消息处断掉,等于什么都没说。这里统一提取 类型+消息,并对常见坑给出可操作的处置建议。
"""
import re

# 常见故障 → 该怎么办(匹配错误消息全文,不区分大小写)
_HINTS: list[tuple[str, str]] = [
    (r"database is locked|database table is locked",
     "SQLite 写锁等待超时:并行采集时多个 worker 同时写库。请到设置页把『抓取并发数』调小"
     "(如 5→2)、或把『SQLite 写锁等待』调大;数据量大时建议换 PostgreSQL。"),
    (r"no such column|no such table",
     "数据库结构比代码旧(升级后没重启)。停掉服务重新启动一次即可自动补列;"
     "若仍报错,备份 data/app.db 后删除重建。"),
    (r"unable to open database file|disk I/O error|attempt to write a readonly database",
     "数据库文件打不开/不可写:检查 data/ 目录是否存在、是否被杀毒软件或网盘同步锁住、磁盘是否已满。"),
    (r"database or disk is full", "磁盘空间不足,清理后重试。"),
    (r"too many open files", "打开的文件/连接过多:把『抓取并发数』调小后重试。"),
    (r"UNIQUE constraint failed",
     "唯一约束冲突(通常是并发写入同一条源/文档)。可在数据源页点『扫描同站多源』整理后重试。"),
    (r"timed out|timeout|ReadTimeout|ConnectTimeout",
     "网络超时:目标站慢或不可达。可在设置页调大『抓取超时』,或把该源转『站内检索』。"),
    (r"SSLError|certificate verify failed", "目标站证书异常,多为对方站点问题;可把该源转『站内检索』兜底。"),
]


def error_headline(e: BaseException, limit: int = 480) -> str:
    """一行原因:`类型: 消息`,并在识别出常见故障时附上处置建议。"""
    msg = " ".join(str(e).split())          # 压掉换行,避免页面上撑成一大坨
    head = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
    for pat, hint in _HINTS:
        if re.search(pat, head, re.I):
            head = f"{head} ── 处置建议:{hint}"
            break
    return head[:limit]
