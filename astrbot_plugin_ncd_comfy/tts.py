# -*- coding: utf-8 -*-
"""语音合成工作流（IndexTTS-2）：把一段文字 + 一段参考音频变成 mp3。

工作流来自用户调好的 `zIndexTTS2 AI语音克隆-情绪控制(带语速调节).json`，
这里裁成最小可跑的一条链：

    LoadAudio(参考音频) ──┐
                          ├─→ indexTTSGenerate(文字) → SaveAudioMP3
    IndexTTS 模型加载 ────┘

为什么不做成"每条回复都配语音"：
  实测这节点每次推理都会重新做一轮模型初始化（约 39 秒），加推理总共 ~80 秒一条。
  那样的延迟不适合聊天，所以做成按需工具（用户明确要语音时才合成）。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent / "workflows" / "tts.json"


class TTSError(Exception):
    """语音合成的可预期失败。"""


#: 情绪预设 → easy indexTTSEmotionVector 的 8 个维度（每个 0~1.4）
#: 维度顺序：开心/生气/难过/害怕/讨厌/低落/惊讶/平静
EMOTION_DIMS = ("Happy", "Angry", "Sad", "Fear", "Hate", "Low", "Surprise", "Neutral")

EMOTIONS: dict[str, dict[str, float]] = {
    # ★ 整体调低：IndexTTS 的向量很“用力”，0.4~0.55 已经是明显情绪，
    #   拉满（0.9~1.2）会变得亢奋、发飘（2026-10-22 实测反馈）。
    "开心": {"Happy": 0.45, "Neutral": 0.35},
    "很高兴": {"Happy": 0.6, "Neutral": 0.3},
    "生气": {"Angry": 0.45, "Neutral": 0.35},
    "难过": {"Sad": 0.45, "Low": 0.25},
    "委屈": {"Sad": 0.35, "Low": 0.3},
    "害怕": {"Fear": 0.4, "Sad": 0.15},
    "害羞": {"Happy": 0.25, "Neutral": 0.45},
    "惊讶": {"Surprise": 0.45, "Neutral": 0.3},
    "讨厌": {"Hate": 0.35, "Neutral": 0.4},
    "低落": {"Low": 0.45, "Sad": 0.25},
    "平静": {"Neutral": 0.6},
    "温柔": {"Neutral": 0.5, "Happy": 0.2},
    "撒娇": {"Happy": 0.3, "Neutral": 0.45},
}

#: 名字 → 预设键（允许英文/别名）
EMOTION_ALIASES = {
    "happy": "开心", "joy": "开心", "excited": "很高兴",
    "angry": "生气", "mad": "生气",
    "sad": "难过", "cry": "难过", "wronged": "委屈",
    "fear": "害怕", "scared": "害怕",
    "shy": "害羞", "surprise": "惊讶", "surprised": "惊讶",
    "hate": "讨厌", "dislike": "讨厌",
    "low": "低落", "depressed": "低落",
    "calm": "平静", "neutral": "平静", "normal": "平静",
    "gentle": "温柔", "soft": "温柔",
    "coquettish": "撒娇", "cute": "撒娇",
}


#: 省略号 / 连续点 → 停顿标记（IndexTTS 支持 -0.5s- 这种写法）
_ELLIPSIS = re.compile(r"(?:…{1,}|\\.{3,}|。{2,}|，{2,}|、{2,})")


def normalize_pauses(text: str, seconds: float = 1.0) -> str:
    """把“……”/“...”换成显式停顿标记。

    为什么要换：省略号在 TTS 里经常被直接吞掉，节奏就没了；换成 -0.7s-
    是模型能听懂的停顿指令，长短还能调。
    """
    raw = str(text or "")
    sec = max(0.1, min(3.0, float(seconds or 1.0)))
    marker = f"-{sec:g}s-"
    # 已经手写了 -0.5s- 的地方不动
    # ★ 停顿标记必须【独占一行】——节点用 ^-\\d+s-$ 匹配整行，
    #   塞在句子中间会被当成普通文字念出来；而且节点会裁掉每段尾部静音，
    #   所以标记值要比“想要的静音”大 1 秒左右（2.5s 标记 ≈ 1.1s 可感停顿）。
    spaced = _ELLIPSIS.sub(f"\\n{marker}\\n", raw)
    return re.sub(r"\\n{2,}", "\\n", spaced).strip()


def normalize_emotion(name: str) -> str:
    """把模型给的情绪词归一成预设键；认不出返回空串（= 不带情绪）。"""
    text = str(name or "").strip().lower()
    if not text:
        return ""
    if text in EMOTIONS:
        return text
    if text in EMOTION_ALIASES:
        return EMOTION_ALIASES[text]
    for key in EMOTIONS:
        if key in text:
            return key
    return ""


def emotion_names() -> str:
    return " / ".join(EMOTIONS.keys())



def load_template() -> dict:
    if not WORKFLOW.exists():
        raise TTSError(f"语音工作流不存在：{WORKFLOW}")
    try:
        return json.loads(WORKFLOW.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TTSError(f"tts.json 不是合法 JSON：{exc}") from exc


def build(
    *,
    text: str,
    reference: str,
    speed: float = 1.0,
    seed: int = 0,
    beams: int = 1,
    emotion: str = "",
    pause_seconds: float = 0.0,
    prefix: str = "tts/sigua",
    unload_model: bool = False,
) -> dict:
    """生成一份可提交的图（不改磁盘上的模板）。

    emotion：情绪预设名（见 EMOTIONS）。给空 = 不带情绪（只用参考音频的音色）。
    """
    graph = copy.deepcopy(load_template())
    if "3" not in graph:
        raise TTSError("tts.json 里找不到 3 号生成节点")
    gen = graph["3"]["inputs"]
    spoken = str(text)
    if pause_seconds and pause_seconds > 0:
        spoken = normalize_pauses(spoken, pause_seconds)
    gen["text"] = spoken
    gen["speech_speed"] = float(max(0.5, min(2.0, speed or 1.0)))
    gen["seed"] = int(seed or 0)
    gen["num_beams"] = max(1, min(10, int(beams or 1)))
    gen["unload_model"] = bool(unload_model)
    if "2" in graph:
        graph["2"]["inputs"]["audio"] = str(reference)

    # 情绪：插一个情绪节点，并把生成节点的 emotions 接过去
    #   · 预设词（开心/害羞…）→ 8 维向量，稳定可控
    #   · 其它任意描述（"又羞又恼，尾音发颤"）→ 交给 Qwen 情感模型算向量，最生动
    key = normalize_emotion(emotion)
    desc = "" if key else str(emotion or "").strip()
    need_pause = bool(pause_seconds and pause_seconds > 0)
    if (key or desc or need_pause) and "2" in graph:
        if key:
            vector = {dim: 0.0 for dim in EMOTION_DIMS}
            vector.update(EMOTIONS[key])
            graph["0"] = {
                "class_type": "easy indexTTSEmotionVector",
                "inputs": {
                    "reference_audio": ["2", 0],
                    "voice_name": "s1",
                    "use_random": False,
                    **vector,
                },
            }
        elif desc:
            graph["0"] = {
                "class_type": "easy indexTTSEmotionText",
                "inputs": {
                    "reference_audio": ["2", 0],
                    "voice_name": "s1",
                    "description": desc,
                    "use_random": False,
                },
            }
        else:
            # 只为让停顿被解析：全零向量 = 不加情绪
            graph["0"] = {
                "class_type": "easy indexTTSEmotionVector",
                "inputs": {
                    "reference_audio": ["2", 0],
                    "voice_name": "s1",
                    "use_random": False,
                    **{dim: 0.0 for dim in EMOTION_DIMS},
                },
            }
        gen["emotions"] = ["0", 0]
        # ★ 每一行都要带 [s1]：节点在停顿后会清空当前音色，
        #   不带前缀的后续行会被整段丢掉（2026-10-22 实测：停顿后那句直接没声）。
        lines = [ln.strip() for ln in spoken.splitlines() if ln.strip()]
        gen["text"] = "\n".join(
            ln
            if (re.fullmatch(r"-\d+(?:\.\d+)?s-", ln) or ln.startswith("["))
            else f"[s1] {ln}"
            for ln in lines
        )

    if "4" in graph:
        graph["4"]["inputs"]["filename_prefix"] = str(prefix)
    return graph


def summarize(graph: dict) -> dict:
    gen = (graph.get("3") or {}).get("inputs") or {}
    return {
        "text": str(gen.get("text") or ""),
        "speed": float(gen.get("speech_speed") or 1.0),
        "reference": str(((graph.get("2") or {}).get("inputs") or {}).get("audio") or ""),
    }
