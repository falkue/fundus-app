"""Video files of the media archive (fundus.photos): probing, transcoding, frames – all through ffmpeg/ffprobe.

A video is stored like a photo (content-addressed under data/photos/<sha[:2]>/): the original as imported
(<sha>.orig.<ext>), a playable copy for slides and presentations (<sha>.video.mp4: H.264 + AAC, long edge at most
1920 px, faststart – plays in every browser, on the iPad and in PowerPoint), a copy without sound made on first request
(<sha>.mute.mp4, for videos whose speech is not cleared), the poster frame as thumb/view/slide JPEG (as for photos) and a
contact sheet of four frames (<sha>.sheet.jpg) that the local vision model annotates. Nothing here leaves the Spark.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

VIDEO_EXT = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "mts", "m2ts", "3gp", "mpg", "mpeg", "wmv"}
MAX_VIDEO_BYTES = 8 * 1024 ** 3
MAX_EDGE = 1920
ENCODER = os.environ.get("FUNDUS_VIDEO_ENCODER", "libx264")      # 'h264_nvenc' uses the GPU (faster, larger files)
CRF = os.environ.get("FUNDUS_VIDEO_CRF", "23")
TIMEOUT = 3 * 3600


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    """duration (s), width/height (as displayed, rotation applied), fps, has_audio, created (ISO, from the container),
    camera."""
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ValueError(f"not a readable video ({(r.stderr.strip().splitlines() or ['ffprobe failed'])[-1][:120]})")
    d = json.loads(r.stdout)
    vs = [s for s in d.get("streams", []) if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    if not vs:
        raise ValueError("no video stream")
    v = vs[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(float((v.get("tags") or {}).get("rotate") or 0))
    if abs(rot) % 180 == 90:
        w, h = h, w
    fps = None
    try:
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = round(float(num) / float(den), 2) if float(den) else None
    except (ValueError, ZeroDivisionError):
        pass
    fmt = d.get("format") or {}
    tags = {k.lower(): val for k, val in (fmt.get("tags") or {}).items()}
    created = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    camera = " ".join(x for x in (tags.get("com.apple.quicktime.make"), tags.get("com.apple.quicktime.model")) if x) or None
    return {"duration": round(float(fmt.get("duration") or v.get("duration") or 0), 2), "width": w, "height": h, "fps": fps,
            "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", [])),
            "created": created, "camera": camera}


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(r.stderr.strip().splitlines() or ['?'])[-1][:200]}")


def _tmp(dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst.with_name(f"{dst.stem}.tmp{os.getpid()}{dst.suffix}")


def transcode(src: Path, dst: Path, sound: bool = True, start: float | None = None, end: float | None = None,
              encoder: str | None = None) -> Path:
    """Playable H.264/AAC MP4 (long edge ≤ 1920, yuv420p, faststart); sound=False drops the audio track;
    start/end (s) cut a clip."""
    if dst.exists():
        return dst
    enc = encoder or ENCODER
    tmp = _tmp(dst)
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if end:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    vf = f"scale=w={MAX_EDGE}:h={MAX_EDGE}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p"
    cmd += ["-map", "0:v:0", "-vf", vf, "-c:v", enc]
    cmd += ["-preset", "p5", "-cq", CRF] if enc == "h264_nvenc" else ["-preset", "faster", "-crf", CRF, "-profile:v", "high"]
    cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"] if sound else ["-an"]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", str(tmp)]
    try:
        _run(cmd)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    os.chmod(dst, 0o644)
    return dst


def mute(src_mp4: Path, dst: Path) -> Path:
    """Copy of a transcoded video without its audio track (no re-encoding)."""
    if dst.exists():
        return dst
    tmp = _tmp(dst)
    try:
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src_mp4), "-map", "0:v:0", "-c:v", "copy", "-an",
              "-movflags", "+faststart", str(tmp)])
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)
    os.chmod(dst, 0o644)
    return dst


def frame(src: Path, t: float):
    """PIL image of the frame at t seconds (rotation applied by ffmpeg)."""
    import io
    from PIL import Image
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(src), "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=300)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"no frame at {t:.1f}s")
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def poster_time(duration: float) -> float:
    return min(max(duration * 0.2, 0.0), max(duration - 0.1, 0.0)) if duration > 1 else 0.0


def contact_sheet(src: Path, duration: float, edge: int = 1024):
    """2×2 grid of frames at 1/8, 3/8, 5/8, 7/8 of the video, in time order (left to right, top to bottom)."""
    from PIL import Image
    frames = []
    for k in (1, 3, 5, 7):
        try:
            frames.append(frame(src, duration * k / 8 if duration > 1 else 0))
        except RuntimeError:
            pass
    if not frames:
        raise RuntimeError("no frames")
    while len(frames) < 4:
        frames.append(frames[-1])
    w, h = frames[0].size
    s = edge / (2 * max(w, h))
    cw, ch = max(1, int(w * s)), max(1, int(h * s))
    sheet = Image.new("RGB", (2 * cw + 6, 2 * ch + 6), (255, 255, 255))
    for i, f in enumerate(frames):
        sheet.paste(f.resize((cw, ch)), ((i % 2) * (cw + 6), (i // 2) * (ch + 6)))
    return sheet


def duration_label(sec: float | None) -> str:
    if not sec:
        return ""
    s = int(round(sec))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
