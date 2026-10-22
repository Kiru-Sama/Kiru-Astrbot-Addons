# -*- coding: utf-8 -*-
"""群聊图片的内部审核判定（不注册为聊天工具，只给插件自己调）。

判定标准（用户定的）：
  · 只有【明确露出乳首 / 生殖器（三点）】才算 拒绝
  · 性暗示、情趣内衣、泳装、露沟、大腿、姿势暧昧 —— 一律 通过

调用方把「用户刚才的原话」和「这张图的提示词」原样传进来，不做摘要，
让视觉模型带着上下文判断；回答只认 PASS / REJECT。
"""

from __future__ import annotations

import asyncio
import pathlib
import uuid

from astrbot.api import logger

PROMPT_TEMPLATE = """你是图片合规审核员。先读上下文，再只看这张图本身。

【用户刚才的原话（原封不动）】
{user_text}

【这张图的生成提示词（原封不动）】
{image_prompt}

【判定规则】
只有画面里明确、无遮挡地露出下面任一部位时，才算 REJECT：
  1. 女性乳首（乳头）
  2. 女性生殖器 / 外阴
  3. 男性生殖器

以下情况一律 PASS，不算违规：
  · 性暗示、挑逗表情、暧昧姿势
  · 情趣内衣、泳装、内衣、透视装
  · 露沟、露大腿、露背、露脐
  · 隔着衣物、被手/道具遮挡、只露轮廓

【回答格式】
只回一个词：PASS 或 REJECT。不要解释，不要附加任何其他文字。
"""


def build_prompt(user_text: str, image_prompt: str) -> str:
    return PROMPT_TEMPLATE.format(
        user_text=(user_text or "（没有文字，只有图）").strip()[:1500],
        image_prompt=(image_prompt or "（无，这张图不是本轮生成的）").strip()[:1500],
    )


async def judge(
    provider,
    image_bytes: bytes,
    *,
    user_text: str = "",
    image_prompt: str = "",
    timeout: float = 40.0,
) -> bool | None:
    """返回 True=通过 / False=拒绝 / None=判断不了（调用失败或回答含糊）。"""
    if not image_bytes:
        return None
    tmp = pathlib.Path(__import__("tempfile").gettempdir()) / f"ncd_judge_{uuid.uuid4().hex[:8]}.png"
    try:
        tmp.write_bytes(image_bytes)
        resp = await asyncio.wait_for(
            provider.text_chat(
                prompt=build_prompt(user_text, image_prompt),
                image_urls=[str(tmp)],
            ),
            timeout=timeout,
        )
        answer = str(getattr(resp, "completion_text", "") or "").strip()
        upper = answer.upper()
        # 先判拒绝（"不通过" 里也含"通过"，顺序不能反）
        if "REJECT" in upper or "拒绝" in answer or "不通过" in answer:
            logger.info(f"[comfy] 审核=拒绝（回答={answer[:40]!r}）")
            return False
        if "PASS" in upper or "通过" in answer:
            logger.info(f"[comfy] 审核=通过（回答={answer[:40]!r}）")
            return True
        logger.warning(f"[comfy] 审核回答含糊，按判断不了处理：{answer[:80]!r}")
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
