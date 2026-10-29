# -*- coding: utf-8 -*-
"""语音：把一句话合成 mp3。

用 edge-tts（微软 Edge 的在线朗读接口，免费、不用申请 Key），中文音色自然度
明显好于 pyttsx3 那套离线引擎。合成结果按内容哈希缓存，同一句话不重复请求。

关于「能不能发成微信语音气泡」：不能。微信 Windows 客户端本身没有录音入口，
唯一的语音消息通道是往本地消息库写 SILK 行——那属于 hook 范畴，随时会因为
微信更新失效，脏数据还可能把会话搞坏。所以这里走「音频文件」通道：对方点开
就能听，内容一样。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


class TTS:
    def __init__(
        self,
        out_dir: Path,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+8%",
        pitch: str = "+0Hz",
        max_chars: int = 220,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.max_chars = max_chars

    # ------------------------------------------------------------------
    def _clean(self, text: str) -> str:
        text = re.sub(r"```.*?```", " ", text, flags=re.S)
        text = re.sub(r"[*#`>_~]", "", text)
        text = re.sub(r"https?://\S+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > self.max_chars:
            cut = text[: self.max_chars]
            for mark in ("。", "！", "？", "，", ". "):
                pos = cut.rfind(mark)
                if pos > self.max_chars * 0.5:
                    cut = cut[: pos + 1]
                    break
            text = cut
        return text

    def synth(self, text: str) -> Optional[Path]:
        """合成语音，返回 mp3 路径；失败返回 None（调用方降级成纯文字）。"""
        spoken = self._clean(text)
        if not spoken:
            return None

        digest = hashlib.md5(
            ("%s|%s|%s|%s" % (self.voice, self.rate, self.pitch, spoken)).encode("utf-8")
        ).hexdigest()[:16]
        out = self.out_dir / f"{digest}.mp3"
        if out.exists() and out.stat().st_size > 1024:
            return out

        try:
            asyncio.run(self._run(spoken, out))
        except Exception as exc:  # noqa: BLE001
            logger.warning("语音合成失败：%s", exc)
            return None

        if not out.exists() or out.stat().st_size < 1024:
            logger.warning("语音合成产出为空：%s", spoken[:30])
            return None
        self._gc()
        return out

    async def _run(self, text: str, out: Path) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(
            text, voice=self.voice, rate=self.rate, pitch=self.pitch
        )
        tmp = out.with_suffix(".part")
        await communicate.save(str(tmp))
        tmp.replace(out)

    def _gc(self, keep_days: float = 3.0) -> None:
        """清掉几天前的缓存语音，别把磁盘塞满。"""
        cutoff = time.time() - keep_days * 86400
        for path in self.out_dir.glob("*.mp3"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
