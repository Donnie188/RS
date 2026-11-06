# -*- coding: utf-8 -*-
"""进程内滑动窗口限流器（阶段 7 上线加固新增）。

解决上线检查清单"并发与限流"维度的两个问题：
1. **LLM 调用配额**：大模型按 token 计费，被脚本刷接口 = 烧钱；
   对 /api/chat 按用户做滑动窗口限流（默认 60 次/分钟）。
2. **SQLite 写竞争**：限流同时天然压住了单用户的高频写入。

实现取舍（与"小系统"约束一致）：
- 不引 Redis：单实例部署，进程内存即可；多实例部署时换 Redis 的 ZSET 实现，
  接口签名保持一致（`hit(user_id) -> bool`），业务代码零改动。
- 不做全局桶：只按用户限流，管理员和员工互不影响。
- O(1) 内存回收：窗口滑走的时间戳在每次 hit 时惰性清理，无后台线程。
"""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """按用户维度的滑动窗口限流器（线程安全）。

    用法：
        limiter = SlidingWindowLimiter(max_per_min=60)
        if not limiter.hit(user_id):
            return api_error(...)   # 429 Too Many Requests
    """

    def __init__(self, max_per_min: int):
        self.max_per_min = max(1, int(max_per_min))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, user_id: str) -> bool:
        """记录一次请求。返回 True = 放行；False = 超出配额（拒绝）。"""
        now = time.monotonic()
        window_start = now - 60.0
        with self._lock:
            q = self._hits[user_id]
            # 惰性清理：弹出滑出窗口的时间戳
            while q and q[0] < window_start:
                q.popleft()
            if len(q) >= self.max_per_min:
                return False          # 窗口内已满 → 拒绝（注意：拒绝的请求不计数）
            q.append(now)
            return True

    def remaining(self, user_id: str) -> int:
        """当前窗口内该用户剩余配额（便于排查与未来加响应头）。"""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(user_id)
            if not q:
                return self.max_per_min
            window_start = now - 60.0
            used = sum(1 for t in q if t >= window_start)
            return max(0, self.max_per_min - used)
