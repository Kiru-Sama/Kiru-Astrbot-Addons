# -*- coding: utf-8 -*-
"""DeepSeek 官方 Search / Fetch API 客户端。

文档：https://api-docs.deepseek.com/guides/search/
  POST {base}/v1/tools/search   {query, count, ori_question, need_content}
  POST {base}/v1/tools/fetch    {url}
鉴权与聊天接口相同（Bearer key）。按成功请求计费，失败/空结果不计费。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request


class DeepSeekWebError(RuntimeError):
    """官方接口返回的错误（带 HTTP 状态，便于决定要不要回退）。"""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _post_sync(url: str, key: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise DeepSeekWebError(
            f"HTTP {exc.code}: {detail or exc.reason}", status=exc.code
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DeepSeekWebError(f"请求失败：{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise DeepSeekWebError(f"返回不是 JSON：{exc}") from exc


async def search(
    base: str,
    key: str,
    query: str,
    *,
    count: int = 8,
    ori_question: str = "",
    need_content: bool = False,
    timeout: float = 25.0,
) -> list[dict]:
    """调官方搜索，返回 results 列表（已按接口原样）。"""
    payload: dict = {
        "query": str(query)[:128],
        "count": max(1, min(50, int(count))),
        "need_content": bool(need_content),
    }
    if ori_question:
        payload["ori_question"] = str(ori_question)[:128]
    data = await asyncio.to_thread(
        _post_sync, f"{base.rstrip('/')}/v1/tools/search", key, payload, timeout
    )
    results = data.get("results")
    if not isinstance(results, list):
        raise DeepSeekWebError("返回里没有 results 数组")
    return results


async def fetch(base: str, key: str, url: str, *, timeout: float = 30.0) -> dict:
    """调官方抓取，返回 {title, content}。"""
    data = await asyncio.to_thread(
        _post_sync,
        f"{base.rstrip('/')}/v1/tools/fetch",
        key,
        {"url": str(url)[:8192]},
        timeout,
    )
    content = data.get("content")
    if not isinstance(content, str):
        raise DeepSeekWebError("返回里没有 content")
    return {"title": str(data.get("title") or ""), "content": content}
