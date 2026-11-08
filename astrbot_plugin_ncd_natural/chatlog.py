"""读取 QQ 聊天记录导出文件，生成模仿对象的风格档案与可检索片段。

支持的输入格式（都能自动识别）：
1. qq-chat-exporter 风格：
   {"chatInfo": {...}, "messages": [{"time": 1700000000,
     "sender": {"uin": "123", "name": "林小满"},
     "content": [{"type": "text", "text": "你好"}]}]}
2. 精简风格（本项目示例数据用的）：
   {"messages": [{"time": 1700000000, "name": "林小满", "text": "你好"}]}
3. 纯数组：上面 messages 里的对象直接构成一个数组。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# 这些类型的片段不算"话"，只当作媒体占位
MEDIA_TYPES = {"image", "face", "mface", "video", "file", "voice", "json", "xml", "ark", "markdown"}

PUNCT_END = "。！？!?…~～"
STOP_GRAMS = {
    "我们", "你们", "他们", "这个", "那个", "什么", "怎么", "可以", "就是", "不是",
    "没有", "一个", "现在", "时候", "真的", "感觉", "然后", "所以", "但是", "因为",
    "如果", "还是", "已经", "知道", "觉得", "有点", "应该", "自己", "这样", "那样",
    "一下", "一点", "其实", "而且", "不过", "还有", "东西", "地方",
}


@dataclass
class Message:
    time: int
    name: str
    text: str
    media_only: bool = False


@dataclass
class Persona:
    name: str
    me: str
    profile: dict
    pairs: list = field(default_factory=list)
    total: int = 0

    def profile_lines(self) -> list[str]:
        p = self.profile
        lines = [
            f"- 一共 {p['count']} 条消息，平均每条 {p['avg_len']} 个字，一半的消息不超过 {p['median_len']} 个字",
            f"- 大概 {p['no_punct_pct']}% 的消息结尾不带句号，几乎不打标点",
            f"- 语气词/口头禅：" + "、".join(p["particles"]) if p["particles"] else "",
            f"- 常出现的短语：" + "、".join(p["phrases"]) if p["phrases"] else "",
            f"- 表情和表情包：{p['media_pct']}% 的消息带 [表情] 或 [图片]",
        ]
        if p["laugh"]:
            lines.append(f'- 笑的时候习惯用：{p["laugh"]}')
        return [x for x in lines if x]


def _clean(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _flatten_content(content) -> tuple[str, bool]:
    """把 content 字段统一成纯文本，返回 (文本, 是否只有媒体)。"""
    if isinstance(content, str):
        return _clean(content), False
    if not isinstance(content, list):
        return "", False

    parts, has_text = [], False
    for seg in content:
        if not isinstance(seg, dict):
            if isinstance(seg, str):
                parts.append(seg)
                has_text = True
            continue
        ctype = str(seg.get("type", "")).lower()
        if ctype in MEDIA_TYPES:
            parts.append("[表情]" if ctype in ("face", "mface") else "[图片]")
            continue
        text = seg.get("text") or seg.get("data") or ""
        if isinstance(text, dict):
            text = text.get("text", "")
        text = _clean(str(text))
        if text:
            parts.append(text)
            has_text = True
    return "".join(parts), not has_text


def _raw_list(doc) -> list:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("messages", "data", "records", "chat", "list"):
            if isinstance(doc.get(key), list):
                return doc[key]
    raise ValueError("认不出这个聊天记录格式：需要一个数组，或含 messages/data/records 字段的对象")


def _pick_name(raw: dict) -> str:
    sender = raw.get("sender")
    if isinstance(sender, dict):
        for k in ("name", "nickname", "card", "uin"):
            if sender.get(k):
                return str(sender[k])
    for k in ("name", "senderName", "nickname", "nick", "uin"):
        if raw.get(k):
            return str(raw[k])
    return "未知"


def _pick_time(raw: dict) -> int:
    for k in ("time", "timestamp", "sendTime", "msgTime", "date"):
        v = raw.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def _pick_text(raw: dict) -> tuple[str, bool]:
    if "content" in raw:
        return _flatten_content(raw["content"])
    for k in ("text", "message", "msg", "content"):
        if isinstance(raw.get(k), str):
            return _clean(raw[k]), False
    return "", False


def _is_qce(doc) -> bool:
    """QQ Chat Exporter 的导出格式：chatInfo 里有 selfUin / peerUin。"""
    if not isinstance(doc, dict):
        return False
    info = doc.get("chatInfo")
    return isinstance(info, dict) and "selfUin" in info and isinstance(doc.get("messages"), list)


# 这些类型的消息不带可学习的话，直接丢掉
_QCE_DROP = {"json", "video", "file", "markdown", "type_19", "type_23", "type_24",
             "type_25", "type_31", "type_32"}


def _qce_messages(doc: dict) -> tuple[list[Message], dict]:
    info = doc["chatInfo"]
    self_uin = str(info.get("selfUin") or "")
    self_name = info.get("selfName") or "我"
    peer_name = info.get("name") or "TA"

    out: list[Message] = []
    for raw in doc["messages"]:
        if not isinstance(raw, dict):
            continue
        if raw.get("recalled") or raw.get("system"):
            continue
        mtype = raw.get("type")
        if mtype in _QCE_DROP:
            continue
        sender = raw.get("sender") or {}
        name = self_name if str(sender.get("uin") or "") == self_uin else peer_name

        content = raw.get("content") or {}
        text = _clean(str(content.get("text") or ""))
        media_only = False
        if mtype == "reply":
            text = re.sub(r"^\[回复消息\]", "", text)
        elif mtype == "type_17":
            text, media_only = "[表情]", True
        elif mtype == "audio":
            text, media_only = "[语音]", True
        elif mtype != "text":
            continue
        if not text:
            continue
        if re.fullmatch(r"(\[(图片|表情|语音|视频|文件)[^\]]*\])+", text):
            media_only = True

        ts = raw.get("timestamp") or 0
        out.append(Message(int(ts) // 1000 if ts > 1e11 else int(ts), name, text, media_only))

    out.sort(key=lambda m: m.time)
    meta = {"name": peer_name, "self": {"name": self_name},
            "self_uin": self_uin, "peer_uin": str(info.get("peerUin") or "")}
    return out, meta


def load_messages(path: str | Path) -> tuple[list[Message], dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if _is_qce(doc):
        return _qce_messages(doc)

    info = doc.get("chatInfo") or doc.get("chat_info") or {} if isinstance(doc, dict) else {}

    messages: list[Message] = []
    for raw in _raw_list(doc):
        if not isinstance(raw, dict):
            continue
        text, media_only = _pick_text(raw)
        if not text:
            continue
        # 精简格式里没有 type 字段，靠占位符反推
        if not media_only and re.fullmatch(r"(\[(表情|图片|语音|视频|文件)\])+", text):
            media_only = True
        messages.append(Message(_pick_time(raw), _pick_name(raw), text, media_only))
    messages.sort(key=lambda m: m.time)
    return messages, info


def guess_roles(messages: list[Message], info: dict, ta: str | None, me: str | None) -> tuple[str, str]:
    """确定"模仿对象"和"我"。"""
    counts: dict[str, int] = {}
    for m in messages:
        counts[m.name] = counts.get(m.name, 0) + 1
    if not counts:
        raise ValueError("聊天记录里一条消息都没解析出来")

    self_name = me
    if not self_name and isinstance(info.get("self"), dict):
        self_name = info["self"].get("name")

    if not ta:
        if self_name and self_name in counts:
            others = [n for n in counts if n != self_name]
            ta = max(others, key=lambda n: counts[n]) if others else None
        else:
            # 没有明确标注"我"：用发言最多的当 TA
            ta = max(counts, key=lambda n: counts[n])
    elif ta not in counts:
        raise ValueError(f"记录里没有叫「{ta}」的人，可选的有：{list(counts)}")
    if not ta:
        raise ValueError(f"没能确定要模仿谁，可选的有：{list(counts)}，请用 --ta 指定")

    if not self_name or self_name == ta:
        others = [n for n in counts if n != ta]
        self_name = max(others, key=lambda n: counts[n]) if others else "我"
    return ta, self_name


def _grams(text: str) -> dict[str, int]:
    """中文按字符 2~4 gram 切。"""
    out: dict[str, int] = {}
    for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text):
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                g = seg[i:i + n]
                out[g] = out.get(g, 0) + 1
    return out


def build_profile(messages: list[Message], ta: str) -> dict:
    texts = [m.text for m in messages if m.name == ta and not m.media_only]
    lens = [len(t) for t in texts] or [0]
    lens_sorted = sorted(lens)

    no_punct = sum(1 for t in texts if t and t[-1] not in PUNCT_END)
    media = sum(1 for m in messages if m.name == ta and m.media_only)
    ta_total = max(len(texts) + media, 1)

    particles = ["哈哈哈", "哈哈", "emmm", "啊这", "好耶", "绝了", "我服了", "算了", "行吧",
                 "真的假的", "救命", "烦死", "？？", "晚安", "好滴", "呵呵", "嗯嗯", "哦哦"]
    hits = []
    for p in particles:
        c = sum(t.count(p) for t in texts)
        if c >= 2:
            hits.append((p, c))
    hits.sort(key=lambda x: -x[1])
    kept: list[tuple[str, int]] = []
    for p, c in hits:
        if any(p in k or k in p for k, _ in kept):
            continue
        kept.append((p, c))
    hits = kept

    gram_counts: dict[str, int] = {}
    gram_df: dict[str, int] = {}
    for t in texts:
        grams = _grams(t)
        for g, c in grams.items():
            gram_counts[g] = gram_counts.get(g, 0) + c
            gram_df[g] = gram_df.get(g, 0) + 1
    # 只保留"在不止一条消息里出现过"的纯中文片段，才像口头禅而不是偶发词
    placeholders = ("图片", "语音", "表情", "视频", "文件", "回复", "消息")
    cand = [(g, c) for g, c in gram_counts.items()
            if c >= 2 and gram_df.get(g, 0) >= 2 and g not in STOP_GRAMS
            and all("\u4e00" <= ch <= "\u9fff" for ch in g)
            and not any(ph in g for ph in placeholders)]
    cand.sort(key=lambda x: (-x[1], -len(x[0])))
    phrases: list[str] = []
    for g, _ in cand:
        if any(g in p or p in g for p in phrases):
            continue
        phrases.append(g)
        if len(phrases) >= 6:
            break

    laughs = [t for t in texts if "哈哈" in t or "2333" in t]
    laugh_style = max(laughs, key=len) if laughs else ""

    profile = {
        "count": len(texts),
        "avg_len": round(sum(lens) / max(len(lens), 1), 1),
        "median_len": lens_sorted[len(lens_sorted) // 2],
        "p90_len": lens_sorted[int(len(lens_sorted) * 0.9)],
        "no_punct_pct": round(100 * no_punct / max(len(texts), 1)),
        "media_pct": round(100 * media / ta_total),
        "particles": [p for p, _ in hits[:6]],
        "phrases": phrases,
        "laugh": laugh_style,
    }
    return profile


def build_pairs(messages: list[Message], ta: str, me: str, ctx: int = 2) -> list[dict]:
    """把每条 TA 的发言搭配上文，做成 (我: … → TA: …) 的示例对。"""
    pairs = []
    for i, m in enumerate(messages):
        if m.name != ta or m.media_only:
            continue
        if len(m.text) < 2:
            continue
        before = messages[max(0, i - ctx):i]
        context = " / ".join(f"{'你' if b.name == ta else me}:{b.text}" for b in before if not b.media_only)
        pairs.append({"context": context, "reply": m.text})
    return pairs


def build_persona(path: str | Path, ta: str | None = None, me: str | None = None,
                  notes: str = "") -> Persona:
    messages, info = load_messages(path)
    ta_name, me_name = guess_roles(messages, info, ta, me)
    profile = build_profile(messages, ta_name)
    pairs = build_pairs(messages, ta_name, me_name)

    if notes:
        profile["notes"] = notes.strip()

    return Persona(name=ta_name, me=me_name, profile=profile, pairs=pairs, total=len(messages))


# ---------------------------------------------------------------- 检索

def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for seg in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text):
        if len(seg) == 1:
            out.add(seg)
            continue
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


class Retriever:
    """极简 TF-IDF：从历史对话里捞出跟当前这句最像的几条，当 few-shot 用。"""

    def __init__(self, pairs: list[dict]):
        self.pairs = pairs
        self.docs = [_bigrams(p["context"] + " " + p["reply"]) for p in pairs]
        df: dict[str, int] = {}
        for d in self.docs:
            for g in d:
                df[g] = df.get(g, 0) + 1
        n = max(len(self.docs), 1)
        self.idf = {g: math.log((n + 1) / (c + 1)) + 1 for g, c in df.items()}
        self.avg_len = sum(len(d) for d in self.docs) / n if self.docs else 1.0

    def search(self, query: str, k: int = 5) -> list[dict]:
        if not self.pairs or not query.strip():
            return self.pairs[:k]
        q = _bigrams(query)
        scored = []
        for pair, doc in zip(self.pairs, self.docs):
            if not doc:
                continue
            hit = sum(self.idf.get(g, 0.5) for g in q & doc)
            if hit <= 0:
                continue
            # 用文档长度做个温和的归一，避免长片段永远赢
            score = hit / (1 + 0.05 * (len(doc) - self.avg_len))
            scored.append((score, pair))
        scored.sort(key=lambda x: -x[0])
        picked = [p for _, p in scored[:k]]
        if len(picked) < k:
            # 不够就用最近的几条补齐，保证示例数量稳定
            for pair in self.pairs[:k]:
                if pair not in picked:
                    picked.append(pair)
                if len(picked) >= k:
                    break
        return picked


# ---------------------------------------------------------------- 提示词

SYSTEM_TEMPLATE = """你是「{name}」，不是 AI 助手，是我认识的那个人。现在你在 QQ 上跟我聊天。

# 你平时说话的样子
{profile}

# 你以前真实发过的话（照这个感觉说，别照抄）
{examples}

# 规矩
1. 只输出你要发出去的消息本身。连发几条就用换行分开，每条都短。
2. 不写旁白、不写动作、不加括号解释、不用 markdown、不列点、不总结。
3. 长度跟上面的原话一致，大部分时候 2~12 个字，别长篇大论。
4. 不用敬语和书面语，该带"啊 / 吧 / 呢 / 哈 / 咯"就带上。
5. 永远别承认自己在模仿、也别提到 AI 和模型。被追问就随口敷衍过去，继续当「{name}」。
6. {notes}
"""


def build_system_prompt(persona: Persona, examples: list[dict], notes: str = "") -> str:
    lines = "\n".join(persona.profile_lines())
    sample_lines = []
    for ex in examples:
        if ex["context"]:
            sample_lines.append(f"{ex['context']}\n你:{ex['reply']}")
        else:
            sample_lines.append(f"你:{ex['reply']}")
    examples_text = "\n".join(sample_lines)
    return SYSTEM_TEMPLATE.format(
        name=persona.name,
        profile=lines,
        examples=examples_text,
        notes=notes or "别主动结束话题，也别一直追问，自然点。",
    )
