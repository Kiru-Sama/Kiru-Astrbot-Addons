"""自测：收藏表情的读取 / 分页 / 越界 / 预览 / 缓存 / 发送参数组装。

不真的发送消息 —— send_custom_face 被替换成间谍函数。
需要 SnowLuma 正在运行（默认从 127.0.0.1:3000 的 OneBot HTTP 接口取数据，
与插件实际走的 OneBot 通道是同一个 action 注册表）。

用法：<AstrBot>\\.venv\\Scripts\\python.exe 测试-收藏表情.py
     端口不是 3000 时：测试-收藏表情.py 3001   （或设环境变量 SNOWLUMA_HTTP）
"""

import asyncio
import io
import json
import os
import pathlib
import sys
import urllib.request

from PIL import Image

PLUGIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN))

import stickers  # noqa: E402

# SnowLuma 的 OneBot HTTP 端口。插件本体走的是 AstrBot 的 OneBot 连接，
# 不需要这个端口；这里只是为了离线测试能直接取数据。
_port = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SNOWLUMA_HTTP", "3000"))
SNOWLUMA_BASE = f"http://127.0.0.1:{_port}"


class HttpBackedBot:
    """把 call_action 转发到 SnowLuma 的 OneBot HTTP 端口，数据形态与真机一致。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        req = urllib.request.Request(
            f"{self.base}/{action}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        body = json.loads(urllib.request.urlopen(req, timeout=60).read())
        if body.get("status") == "failed":
            raise RuntimeError(f"ActionFailed: {body.get('wording')}")
        return body.get("data")


class FakeEvent:
    def __init__(self, bot, group_id: str = "", sender_id: str = "") -> None:
        self.bot = bot
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id


async def main() -> int:
    bot = HttpBackedBot(SNOWLUMA_BASE)
    # 用假群号即可 —— 这个测试用间谍函数替换了发送，不会真的发消息出去
    FAKE_GROUP = 100000001
    event = FakeEvent(bot, group_id=str(FAKE_GROUP))
    failures: list[str] = []

    try:
        items = await stickers.fetch_all(event)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: 连不上 SnowLuma（{SNOWLUMA_BASE}）：{exc}")
        print("请确认 QQ 客户端已登录、SnowLuma 在运行。")
        return 1

    print(f"取回收藏表情 {len(items)} 张")
    if not items:
        print("FAIL: 一张都没取到")
        return 1
    with_note = [i for i in items if (i.get("desc") or "").strip()]
    print(f"  有备注 {len(with_note)} 张 / 无备注 {len(items) - len(with_note)} 张\n")

    print("--- 第 1 页")
    page1 = stickers.render_list(items, 1, 10)
    print(page1)
    if f"{len(items)} 张" not in page1:
        failures.append("列表里的总数与取回数量不符")

    if len(items) > 10:
        print("\n--- 第 2 页")
        page2 = stickers.render_list(items, 2, 10)
        print(page2)
        if "[11]" not in page2:
            failures.append("第 2 页序号没有延续全局序号")

    print("\n--- 越界序号")
    try:
        stickers.find_by_index(items, len(items) + 5)
        failures.append("越界序号没有报错")
    except stickers.StickerError as exc:
        print(f"  正确报错: {exc}")

    print("\n--- 预览图")
    for probe in (items[0], items[-1]):
        jpeg = await stickers.load_preview(probe, max_side=224)
        im = Image.open(io.BytesIO(jpeg))
        ok_magic = jpeg[:2] == b"\xff\xd8"
        print(f"  {im.width}x{im.height} {len(jpeg)} 字节 JPEG={ok_magic}")
        if not ok_magic:
            failures.append("预览不是合法 JPEG")
        if max(im.width, im.height) > 224:
            failures.append(f"预览最长边 {max(im.width, im.height)} 超过 224")
        if max(im.width, im.height) < 100:
            failures.append(f"预览尺寸 {im.width}x{im.height} 过小，可能取错了帧")

    before = len(bot.calls)
    await stickers.load_preview(items[0], max_side=224)
    if len(bot.calls) != before:
        failures.append("预览缓存没生效，重复请求了接口")
    else:
        print("\n缓存命中：第二次预览没有重新请求接口")

    print("\n--- 发送参数组装（不真的发送）")
    original = bot.call_action

    async def spy(action, **params):
        if action == "send_custom_face":
            bot.calls.append((action, params))
            return {"message_id": 12345}
        return await original(action, **params)

    bot.call_action = spy
    await stickers.send_sticker(event, items[0])
    action, params = bot.calls[-1]
    print(f"  action={action} params={params}")
    if action != "send_custom_face":
        failures.append(f"发送调用的是 {action}，期望 send_custom_face")
    if params.get("group_id") != FAKE_GROUP:
        failures.append(f"group_id 应为 int {FAKE_GROUP}，实际 {params.get('group_id')!r}")
    if "user_id" in params:
        failures.append("群聊场景不该同时带 user_id")

    if failures:
        print("\n失败项:")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    print("\nPASS: 读取/分页/越界/预览/缓存/发送参数 全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
