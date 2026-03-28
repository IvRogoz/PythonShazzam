from __future__ import annotations

import asyncio
import ctypes
import io
import os
import threading
import time
import tempfile
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

_NUMPY_FROMSTRING = np.fromstring


def _fromstring_compat(data, dtype=float, count: int = -1, sep: str = ""):
    if sep == "":
        return np.frombuffer(data, dtype=dtype, count=count if count >= 0 else -1)
    return _NUMPY_FROMSTRING(data, dtype=dtype, count=count, sep=sep)


np.fromstring = _fromstring_compat

import soundcard as sc

from music import Shazam, TrackInfo, shazam_identify_bytes


SOURCE_LABELS = {
    "local": "Local listen",
    "system": "System audio",
    "both": "Both",
}


class AudioCaptureError(RuntimeError):
    pass


@dataclass
class LiveTrackResult:
    source: str
    device_name: str
    detected_at: float
    track: TrackInfo


class LiveAudioListener:
    def __init__(
        self,
        source: str,
        auto_preview: bool,
        stop_event: threading.Event,
        on_status: Callable[[str], None],
        on_result: Callable[[LiveTrackResult], None],
        on_level: Callable[[str, float], None] | None = None,
        on_wave: Callable[[str, list[float]], None] | None = None,
        on_preview_ready: Callable[[str, str], None] | None = None,
        sample_rate: int = 48_000,
        clip_seconds: int = 20,
        min_clip_seconds: int = 10,
        recognize_every: float = 4.0,
        timeout: float = 20.0,
        dedupe_seconds: float = 120.0,
    ) -> None:
        self.source = source
        self.auto_preview = auto_preview
        self.stop_event = stop_event
        self.on_status = on_status
        self.on_result = on_result
        self.on_level = on_level
        self.on_wave = on_wave
        self.on_preview_ready = on_preview_ready
        self.sample_rate = sample_rate
        self.clip_seconds = clip_seconds
        self.min_clip_seconds = min_clip_seconds
        self.recognize_every = recognize_every
        self.timeout = timeout
        self.dedupe_seconds = dedupe_seconds
        self._seen: dict[tuple[str, str, str, str], float] = {}
        self._workers: list[threading.Thread] = []
        self._worker_errors: list[str] = []
        self._error_lock = threading.Lock()

    def run(self) -> None:
        com_initialized = self._co_initialize()
        targets = self._resolve_targets()
        self._workers = [
            threading.Thread(target=self._capture_loop, args=(target_source, backend, device, rate), daemon=True)
            for target_source, backend, device, rate in targets
        ]

        for worker in self._workers:
            worker.start()

        while not self.stop_event.is_set():
            alive = any(worker.is_alive() for worker in self._workers)
            if not alive:
                break
            time.sleep(0.2)

        self.stop_event.set()

        for worker in self._workers:
            worker.join(timeout=max(self.timeout + 15.0, 30.0))

        if com_initialized:
            self._co_uninitialize()

        if self._worker_errors:
            raise AudioCaptureError(self._worker_errors[0])

    def _resolve_targets(self) -> list[tuple[str, str, object, int]]:
        targets: list[tuple[str, str, object, int]] = []
        if self.source in ("local", "both"):
            mic = sc.default_microphone()
            if mic is None:
                raise AudioCaptureError("No default microphone was found.")
            targets.append(("local", "soundcard", mic, self.sample_rate))

        if self.source in ("system", "both"):
            stereo_mix = self._find_stereo_mix_device()
            if stereo_mix is not None:
                device_info = sd.query_devices(stereo_mix)
                rate = int(device_info.get("default_samplerate") or self.sample_rate)
                targets.append(("system", "sounddevice", stereo_mix, rate))
            else:
                speaker = sc.default_speaker()
                if speaker is None:
                    raise AudioCaptureError("No default speaker was found.")
                targets.append(("system", "soundcard", sc.get_microphone(speaker.name, include_loopback=True), self.sample_rate))

        if not targets:
            raise AudioCaptureError("No audio source was selected.")

        return targets

    def _capture_loop(self, source: str, backend: str, device: object, sample_rate: int) -> None:
        com_initialized = self._co_initialize()
        block_frames = 2048
        max_frames = int(sample_rate * self.clip_seconds)
        min_frames = int(sample_rate * self.min_clip_seconds)
        chunks: deque[np.ndarray] = deque()
        total_frames = 0
        last_attempt = 0.0
        shazam = Shazam(segment_duration_seconds=12)

        source_label = SOURCE_LABELS.get(source, source.title())
        device_name = self._device_name(backend, device, source_label)
        self.on_status(f"{source_label}: listening on {device_name}")

        try:
            recorder_ctx = self._open_recorder(backend, device, sample_rate, block_frames)
            with recorder_ctx as recorder:
                while not self.stop_event.is_set():
                    data = self._read_block(backend, recorder, block_frames)
                    if data is None or getattr(data, "size", 0) == 0:
                        continue

                    normalized = self._normalize_audio(data)
                    if normalized.size == 0:
                        continue

                    if self.on_level is not None:
                        level = float(np.sqrt(np.mean(np.square(normalized))))
                        self.on_level(source, min(level, 1.0))
                    if self.on_wave is not None:
                        mono = normalized.mean(axis=1)
                        self.on_wave(source, self._wave_points(mono, 160))

                    chunks.append(normalized)
                    total_frames += int(normalized.shape[0])

                    while total_frames > max_frames and chunks:
                        dropped = chunks.popleft()
                        total_frames -= int(dropped.shape[0])

                    now = time.monotonic()
                    if total_frames < min_frames or (now - last_attempt) < self.recognize_every:
                        continue

                    clip = np.concatenate(list(chunks), axis=0)
                    prepared = self._prepare_for_recognition(clip, sample_rate)
                    preview_bytes = self._wav_bytes(prepared, sample_rate=sample_rate)
                    last_attempt = now
                    preview_path = self._write_preview_file(preview_bytes)
                    if self.on_preview_ready is not None:
                        self.on_preview_ready(source, preview_path)
                    if self.auto_preview:
                        self.on_status(f"{source_label}: previewing capture before recognition...")
                        self._play_preview(prepared, sample_rate)
                    self.on_status(f"{source_label}: identifying current audio...")
                    track = asyncio.run(
                        shazam_identify_bytes(
                            shazam,
                            preview_bytes,
                            timeout=self.timeout,
                        )
                    )
                    if not track:
                        self.on_status(f"{source_label}: no match yet, still listening...")
                        continue

                    key = (
                        source,
                        (track.artist or "").casefold(),
                        (track.title or "").casefold(),
                        (track.album or "").casefold(),
                    )
                    previous = self._seen.get(key, 0.0)
                    if (now - previous) < self.dedupe_seconds:
                        continue

                    self._seen[key] = now
                    self.on_result(
                        LiveTrackResult(
                            source=source,
                            device_name=device_name,
                            detected_at=time.time(),
                            track=track,
                        )
                    )
                    self.on_status(f"{source_label}: matched {track.artist} - {track.title}")
        except Exception as exc:
            with self._error_lock:
                self._worker_errors.append(f"{source_label}: {exc}")
            self.stop_event.set()
        finally:
            if com_initialized:
                self._co_uninitialize()

    @staticmethod
    def _co_initialize() -> bool:
        if os.name != "nt":
            return False
        try:
            result = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            return result in (0, 1)
        except Exception:
            return False

    @staticmethod
    def _co_uninitialize() -> None:
        if os.name != "nt":
            return
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass

    @staticmethod
    def _find_stereo_mix_device() -> int | None:
        default_output = sd.default.device[1]
        default_hostapi = None
        if default_output is not None and default_output >= 0:
            default_hostapi = int(sd.query_devices(default_output)["hostapi"])

        for device in sd.query_devices():
            name = str(device.get("name", "")).lower()
            if "stereo mix" not in name:
                continue
            if int(device.get("max_input_channels", 0)) < 1:
                continue
            if default_hostapi is not None and int(device.get("hostapi", -1)) != default_hostapi:
                continue
            return int(device["index"])

        for device in sd.query_devices():
            name = str(device.get("name", "")).lower()
            if "stereo mix" in name and int(device.get("max_input_channels", 0)) >= 1:
                return int(device["index"])
        return None

    @staticmethod
    def _device_name(backend: str, device: object, fallback: str) -> str:
        if backend == "sounddevice":
            return str(sd.query_devices(int(device)).get("name", fallback))
        return getattr(device, "name", fallback)

    @staticmethod
    def _open_recorder(backend: str, device: object, sample_rate: int, block_frames: int):
        if backend == "sounddevice":
            return sd.InputStream(
                samplerate=sample_rate,
                blocksize=block_frames,
                device=int(device),
                channels=2,
                dtype="float32",
                latency="low",
            )
        return device.recorder(samplerate=sample_rate, blocksize=block_frames)

    @staticmethod
    def _read_block(backend: str, recorder: object, block_frames: int) -> np.ndarray | None:
        if backend == "sounddevice":
            data, overflowed = recorder.read(block_frames)
            if overflowed:
                return None
            return data
        return recorder.record(numframes=block_frames)

    @staticmethod
    def _normalize_audio(data: np.ndarray) -> np.ndarray:
        array = np.asarray(data, dtype=np.float32)
        if array.ndim == 1:
            array = array[:, np.newaxis]
        if array.ndim != 2:
            return np.empty((0, 1), dtype=np.float32)

        if array.shape[1] > 2:
            array = array[:, :2]

        if array.shape[1] == 1:
            array = np.repeat(array, 2, axis=1)

        return np.clip(array, -1.0, 1.0)

    @staticmethod
    def _wav_bytes(audio: np.ndarray, sample_rate: int = 48_000) -> bytes:
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(int(pcm.shape[1]))
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm.tobytes())
            return buffer.getvalue()

    @staticmethod
    def _prepare_for_recognition(audio: np.ndarray, sample_rate: int) -> np.ndarray:
        # Pick the loudest continuous window from the rolling buffer, remove DC offset,
        # and apply only a light gain adjustment to avoid clipping/distortion.
        stereo = audio.astype(np.float32, copy=False)
        mono = stereo.mean(axis=1)
        window = min(len(mono), max(sample_rate * 12, 1))
        mono_window = LiveAudioListener._pick_loudest_window(mono, window)
        stereo_window = LiveAudioListener._pick_matching_stereo_window(stereo, mono, mono_window, window)

        stereo_window = stereo_window - stereo_window.mean(axis=0, keepdims=True)
        peak = float(np.max(np.abs(stereo_window))) if stereo_window.size else 0.0
        if peak > 1e-4 and peak < 0.35:
            gain = min(0.7 / peak, 2.5)
            stereo_window = stereo_window * gain

        return np.clip(stereo_window, -1.0, 1.0)

    @staticmethod
    def _pick_loudest_window(audio: np.ndarray, window_size: int) -> np.ndarray:
        if audio.size <= window_size:
            return audio

        step = max(window_size // 8, 1)
        best_start = 0
        best_score = -1.0

        for start in range(0, audio.size - window_size + 1, step):
            window = audio[start:start + window_size]
            score = float(np.mean(np.square(window)))
            if score > best_score:
                best_score = score
                best_start = start

        return audio[best_start:best_start + window_size]

    @staticmethod
    def _pick_matching_stereo_window(
        stereo: np.ndarray,
        mono: np.ndarray,
        mono_window: np.ndarray,
        window_size: int,
    ) -> np.ndarray:
        if stereo.shape[0] <= window_size:
            return stereo

        target_len = mono_window.shape[0]
        step = max(window_size // 8, 1)
        best_start = 0
        best_score = -1.0

        for start in range(0, mono.shape[0] - target_len + 1, step):
            window = mono[start:start + target_len]
            score = float(np.mean(np.square(window)))
            if score > best_score:
                best_score = score
                best_start = start

        return stereo[best_start:best_start + target_len]

    @staticmethod
    def _wave_points(audio: np.ndarray, target_points: int) -> list[float]:
        if audio.size == 0:
            return [0.0] * target_points
        if audio.size < target_points:
            pad = np.zeros(target_points - audio.size, dtype=np.float32)
            audio = np.concatenate((audio.astype(np.float32, copy=False), pad))
        else:
            idx = np.linspace(0, audio.size - 1, target_points).astype(np.int32)
            audio = audio[idx]
        return np.clip(audio, -1.0, 1.0).astype(np.float32).tolist()

    @staticmethod
    def _write_preview_file(wav_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(prefix="pythonshazzam-preview-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(wav_bytes)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        return path

    def _play_preview(self, audio: np.ndarray, sample_rate: int) -> None:
        try:
            sd.play(audio, samplerate=sample_rate, blocking=True)
        except Exception as exc:
            self.on_status(f"Preview failed: {exc}")
