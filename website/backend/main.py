import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
_face_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="face")
_scan_track_cache = {}
# Persistent cache on disk (survives API restarts)
import pickle as _pk
_CACHE_FILE = "/tmp/scan_track_cache.pkl"
try:
    with open(_CACHE_FILE, "rb") as _f:
        _scan_track_cache = _pk.load(_f)
except:
    _scan_track_cache = {}
def _save_cache():
    try:
        with open(_CACHE_FILE, "wb") as _f:
            _pk.dump(_scan_track_cache, _f)
    except:
        pass
# Async blur jobs: {job_id: {status, progress, result_path, error}}
_blur_jobs = {}
import uuid
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import librosa
import yt_dlp
from pypdf import PdfReader, PdfWriter
from PIL import Image
import io

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import aiofiles
import uvicorn

app = FastAPI(title="Alpha Media API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Filename", "X-Original-Size", "X-Compressed-Size"],
)

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

def temp_path(ext: str = "") -> Path:
    return TEMP_DIR / f"{uuid.uuid4().hex}{ext}"

def cleanup(*paths):
    for p in paths:
        try:
            if Path(p).is_dir():
                shutil.rmtree(p)
            elif Path(p).exists():
                os.remove(p)
        except Exception:
            pass


# ─────────────────────────────── VIDEO DOWNLOADER ───────────────────────────

import urllib.request
import urllib.parse
import json
import time as _time
import socket as _socket

def _pot_server_up(host="127.0.0.1", port=4416, timeout=1.0) -> bool:
    """Check whether the bgutil PO-token server is running. If not, we skip
    its extractor args so yt-dlp falls back to its other PO providers."""
    try:
        s = _socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False

def _yt_extractor_args() -> dict:
    args = {"youtube": ["player_client=web"]}
    if _pot_server_up():
        # bgutil-ytdlp-pot-provider auto-fetches PO tokens from the server on port 4416
        args["youtubepot-bgutil-httpserver"] = ["base_url=http://localhost:4416"]
    return args

def _yt_cookiefile():
    """Fresh cookies (from the Google sign-in) win; repo cookies.txt is fallback."""
    candidates = [
        os.environ.get("YTDLP_COOKIES"),
        Path(__file__).resolve().parent.parent.parent / "cookies.txt",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return str(p)
    return None

def _yt_dlp_opts(**extra) -> dict:
    """Base yt-dlp options that work behind this machine's egress proxy:
    - no-certifi: use the system CA bundle (it trusts the egress proxy's CA)
    - socket_timeout: fail fast instead of hanging on tarpitted requests
    - cookiefile: attach YouTube login cookies when available
    """
    opts = {
        "compat_opts": {"no-certifi"},
        "socket_timeout": 30,
        "extractor_args": _yt_extractor_args(),
    }
    cf = _yt_cookiefile()
    if cf:
        opts["cookiefile"] = cf
    opts.update(extra)
    return opts

@app.get("/api/download")
async def download_video(
    url: str,
    quality: str = "720",
    format: str = "video",  # "video" | "mp3"
    playlist: bool = False,
):
    from fastapi.responses import StreamingResponse
    import tempfile, pathlib

    out_dir = pathlib.Path(tempfile.mkdtemp())
    try:
        if format == "mp3":
            fmt = "bestaudio/best"
            pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            merge = {}
        else:
            fmt = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
            pp = []
            merge = {"merge_output_format": "mp4"}

        ydl_opts = _yt_dlp_opts(
            format=fmt,
            outtmpl=str(out_dir / "%(playlist_index)02d-%(title)s.%(ext)s"),
            noplaylist=not playlist,
            quiet=True,
            no_warnings=True,
            **merge,
        )
        if pp:
            ydl_opts["postprocessors"] = pp

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        files = list(out_dir.iterdir())
        if not files:
            raise HTTPException(500, "yt-dlp produced no output file.")

        if playlist and len(files) > 1:
            # Zip multiple files for playlist download
            import zipfile
            zip_path = out_dir / "playlist.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(files):
                    zf.write(f, f.name)
            out_file = zip_path
        else:
            out_file = files[0]
        safe_filename = urllib.parse.quote(out_file.name)
        media_type = "application/zip" if out_file.suffix == ".zip" else ("audio/mpeg" if format == "mp3" else "video/mp4")
        # FileResponse sets Content-Length and handles Range requests,
        # so browsers can resume interrupted downloads instead of restarting.
        # Cleanup happens via BackgroundTasks after the response completes.
        from fastapi import BackgroundTasks
        def _cleanup():
            try:
                import shutil
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
        return FileResponse(
            path=str(out_file),
            media_type=media_type,
            filename=out_file.name,
            headers={"X-Filename": safe_filename},
            background=BackgroundTasks([_cleanup]),
        )

    except HTTPException:
        raise
    except Exception as e:
        try:
            import shutil
            shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass
        msg = str(e)
        # YouTube throttles datacenter IPs; surface a clear, actionable error.
        if "timed out" in msg.lower() or "sign in to confirm" in msg.lower() or "bot" in msg.lower():
            raise HTTPException(
                502,
                "YouTube refused the download from this server's network. "
                "This is fixed by signing a Google account in on the server (planned next step).",
            )
        raise HTTPException(500, msg)


# ─────────────────────────────── VIDEO INFO ─────────────────────────────────

@app.post("/api/video-info")
async def video_info(url: str = Form(...)):
    try:
        import urllib.request
        import json
        
        # Fast extraction using official YouTube OEmbed API
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        req = urllib.request.Request(oembed_url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req)
        data = json.loads(response.read().decode('utf-8'))
        
        return {
            "title": data.get("title", "YouTube Video"),
            "thumbnail": data.get("thumbnail_url"),
            "uploader": data.get("author_name"),
            "duration": None
        }
    except Exception as e:
        raise HTTPException(500, f"Could not fetch video info. URL might be invalid. ({str(e)})")


# ─────────────────────────────── CLIP TRIMMER ───────────────────────────────

@app.post("/api/trim")
async def trim_video(
    file: UploadFile = File(...),
    start: str = Form(...),  # HH:MM:SS or seconds
    end: str = Form(...),
):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())

        cmd = [
            "ffmpeg", "-y",
            "-i", str(in_path),
            "-ss", str(start),
            "-to", str(end),
            "-c:v", "libx264", "-c:a", "aac",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(500, result.stderr[-500:])

        content = out_path.read_bytes()
        cleanup(out_path)
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="trimmed.mp4"', "X-Filename": "trimmed.mp4"},
        )
    finally:
        cleanup(in_path)


@app.post("/api/trim-url")
async def trim_video_from_url(
    url: str = Form(...),
    start: str = Form(...),   # seconds as float or HH:MM:SS string
    end: str = Form(...),
    quality: str = Form("720"),
):
    """Download ONLY the specified time range from a URL using yt-dlp download_ranges.
    This never downloads the full video — only the requested clip."""
    out_dir = temp_path()
    out_dir.mkdir(parents=True)
    try:
        def parse_time(t: str) -> float:
            """Convert HH:MM:SS or plain seconds string to float seconds."""
            t = t.strip()
            if ":" in t:
                parts = t.split(":")
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
            return float(t)

        start_sec = parse_time(start)
        end_sec = parse_time(end)

        if end_sec <= start_sec:
            raise HTTPException(400, "End time must be after start time.")

        ydl_opts = _yt_dlp_opts(
            format=f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/bestvideo+bestaudio/best",
            outtmpl=str(out_dir / "clip.%(ext)s"),
            noplaylist=True,
            quiet=True,
            no_warnings=True,
            merge_output_format="mp4",
            download_ranges=yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
            force_keyframes_at_cuts=True,
        )

        # Retry up to 3 times (ffmpeg occasionally fails on merge)
        last_err = None
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                last_err = None
                break
            except Exception as e:
                last_err = e
                # Clean partial files before retry
                for f in out_dir.iterdir():
                    try: f.unlink()
                    except: pass
                if attempt < 2:
                    import time; time.sleep(2)
        if last_err:
            raise last_err

        files = list(out_dir.iterdir())
        if not files:
            raise HTTPException(500, "No clip was produced. Check URL and time range.")

        out_file = files[0]
        suffix = out_file.suffix or ".mp4"
        safe_name = f"clip_{int(start_sec)}s-{int(end_sec)}s{suffix}"
        content = out_file.read_bytes()
        cleanup(out_dir)

        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "X-Filename": safe_name,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        cleanup(out_dir)
        raise HTTPException(500, str(e))



# ─────────────────────────────── VIDEO TO GIF ───────────────────────────────

@app.post("/api/gif")
async def video_to_gif(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    fps: int = Form(10),
    width: int = Form(480),
):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    palette_path = temp_path(".png")
    out_path = temp_path(".gif")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())

        # Generate palette for high-quality GIF
        subprocess.run([
            "ffmpeg", "-y", "-i", str(in_path),
            "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,palettegen",
            str(palette_path),
        ], capture_output=True)

        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", str(in_path), "-i", str(palette_path),
            "-filter_complex", f"fps={fps},scale={width}:-1:flags=lanczos[x];[x][1:v]paletteuse",
            str(out_path),
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(500, result.stderr[-500:])

        background_tasks.add_task(cleanup, out_path)
        return FileResponse(str(out_path), filename="output.gif", media_type="image/gif")
    finally:
        cleanup(in_path, palette_path)


# ─────────────────────────────── FACE BLUR ──────────────────────────────────
# Selective face blur: /api/face-scan finds unique faces, user picks which to
# blur, /api/face-blur blurs only the selected identities.

import base64 as _b64

def _mp_detector():
    """MediaPipe face detector (BlazeFace, short-range model)."""
    import mediapipe as mp
    return mp.tasks.vision.FaceDetector.create_from_options(
        mp.tasks.vision.FaceDetectorOptions(
            base_options=mp.tasks.BaseOptions(
                model_asset_path=_mp_model_path()),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            min_detection_confidence=0.3,
        )
    )

_mp_model = None
def _mp_model_path():
    p = Path("models/blaze_face_short_range.tflite")
    if not p.exists():
        import urllib.request
        p.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite",
            str(p))
    return str(p)

def _get_mp_detector():
    global _mp_model
    if _mp_model is None:
        _mp_model = _mp_detector()
    return _mp_model

def _detect_faces_mp(detector, bgr):
    """Return list of (x, y, w, h) boxes in pixel coords."""
    import mediapipe as mp
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    res = detector.detect(mp_img)
    h, w = bgr.shape[:2]
    boxes = []
    if res.detections:
        for d in res.detections:
            bb = d.bounding_box
            x = max(0, int(bb.origin_x)); y = max(0, int(bb.origin_y))
            bw = int(bb.width); bh = int(bb.height)
            boxes.append((x, y, min(bw, w - x), min(bh, h - y)))
    return boxes

_scrfd_app = None
def _get_scrfd():
    """InsightFace SCRFD face detector — much more accurate than MediaPipe/YuNet."""
    global _scrfd_app
    if _scrfd_app is None:
        from insightface.app import FaceAnalysis
        _scrfd_app = FaceAnalysis(name='buffalo_s', providers=['CPUExecutionProvider'])
        _scrfd_app.prepare(ctx_id=0, det_size=(640, 640))
        # Lower threshold for higher recall (catch more faces)
        if hasattr(_scrfd_app.models.get('detection'), 'det_thresh'):
            _scrfd_app.models['detection'].det_thresh = 0.25
    return _scrfd_app

def _detect_faces_scrfd(app, bgr):
    """Return list of (x, y, w, h, embedding) using SCRFD + face recognition.
    Embeddings allow matching the same person across different angles.
    Applies NMS to remove duplicate detections of the same face."""
    faces = app.get(bgr)
    results = []
    h, w = bgr.shape[:2]
    for f in faces:
        x1, y1, x2, y2 = f.bbox.astype(int)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 > x1 and y2 > y1:
            emb = getattr(f, 'normed_embedding', None)
            score = float(getattr(f, 'det_score', 0.5) or 0.5)
            results.append((x1, y1, x2 - x1, y2 - y1, emb, score))
    # NMS: remove duplicates via IoU > 0.4 OR containment (>70% of small box inside large)
    results.sort(key=lambda r: r[5], reverse=True)
    kept = []
    for r in results:
        x, y, bw, bh = r[0], r[1], r[2], r[3]
        dup = False
        for k in kept:
            kx, ky, kw, kh = k[0], k[1], k[2], k[3]
            if _iou((x, y, bw, bh), (kx, ky, kw, kh)) > 0.4:
                dup = True
                break
            # Containment: if small box is >70% inside a larger kept box, it's a duplicate
            ix1, iy1 = max(x, kx), max(y, ky)
            ix2, iy2 = min(x+bw, kx+kw), min(y+bh, ky+kh)
            inter = max(0, ix2-ix1) * max(0, iy2-iy1)
            small_area = min(bw*bh, kw*kh)
            if small_area > 0 and inter / small_area > 0.7:
                dup = True
                break
        if not dup:
            kept.append(r)
    return [(x, y, bw, bh, emb) for (x, y, bw, bh, emb, _) in kept]

def _emb_sim(e1, e2):
    """Cosine similarity between face embeddings (0-1, higher = same person)."""
    import numpy as np
    if e1 is None or e2 is None:
        return 0
    return float(np.dot(e1, e2))

def _face_hist(bgr, box):
    """Normalized color histogram of a face crop (for identity matching)."""
    x, y, w, h = box
    crop = bgr[y:y+h, x:x+w]
    if crop.size == 0:
        return None
    crop = cv2.resize(crop, (64, 64))
    hist = cv2.calcHist([crop], [0, 1, 2], None, [8, 8, 8], [0, 256]*3)
    cv2.normalize(hist, hist)
    return hist

def _iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax+aw, bx+bw), min(ay+ah, by+bh)
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    union = aw*ah + bw*bh - inter
    return inter / union if union else 0

@app.post("/api/face-scan")
async def face_scan(file: UploadFile = File(...)):
    """Analyze a video/image, return unique faces with thumbnails.
    Response: {faces: [{id, thumbnail (base64 jpeg), count}]}"""
    # Read upload first (async), then run CPU-heavy work in thread pool
    # so the API stays responsive to other requests (monitor, etc.)
    data = await file.read()
    fname = file.filename
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_face_pool, _do_face_scan, data, fname)

def _do_face_scan(data: bytes, filename: str):
    suffix = Path(filename).suffix.lower()
    in_path = temp_path(suffix if suffix else ".mp4")
    try:
        in_path.write_bytes(data)
        scrfd = _get_scrfd()
        # tracks: [{hist, thumb_b64, count, last_box}]
        tracks = []
        is_video = suffix in [".mp4", ".mov", ".avi", ".mkv", ".webm"]
        frames = []
        if is_video:
            cap = cv2.VideoCapture(str(in_path))
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            # sample ~3 frames per second, max 60 samples — enough for identity
            # clustering while keeping scan fast (SCRFSD is CPU-heavy)
            n_samples = min(60, max(15, int(total / fps * 3)))
            step = max(1, total // n_samples)
            for idx in range(0, total, step):
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    # Downscale to max 640px width for faster SCRFD inference
                    h, w = frame.shape[:2]
                    if w > 640:
                        scale = 640 / w
                        frame = cv2.resize(frame, (640, int(h * scale)))
                    frames.append(frame)
                if len(frames) >= 60:
                    break
            cap.release()
        else:
            img = cv2.imread(str(in_path))
            if img is None:
                raise HTTPException(400, "Invalid image")
            frames = [img]
        _dbg_frames = len(frames)
        _dbg_detections = 0
        for frame in frames:
            for (x, y, bw, bh, emb) in _detect_faces_scrfd(scrfd, frame):
                box = (x, y, bw, bh)
                _dbg_detections += 1
                hist = _face_hist(frame, box)
                # match to existing track: face embedding similarity (works across
                # angles), falling back to IoU + histogram for same-frame continuity
                best, best_score = -1, 0
                for i, t in enumerate(tracks):
                    sim = _emb_sim(emb, t.get("emb"))
                    iou = _iou(box, t["last_box"])
                    corr = cv2.compareHist(hist, t["hist"], cv2.HISTCMP_CORREL) if hist is not None else 0
                    # Strict: embeddings are the identity signal. Only match if
                    # embedding similarity is high, OR (no embedding AND high IoU/histogram
                    # for same-position continuity). Never match different people via histogram alone.
                    if emb is not None and t.get("emb") is not None:
                        score = sim  # embedding-only for identity
                    else:
                        score = max(iou * 1.5, corr)  # fallback when no embeddings
                    if score > best_score:
                        best_score, best = score, i
                if best >= 0 and best_score > 0.35:
                    t = tracks[best]
                    if hist is not None:
                        t["hist"] = cv2.addWeighted(t["hist"], 0.7, hist, 0.3, 0)
                    # Update embedding with running average (adapts to angles)
                    if emb is not None:
                        if t.get("emb") is not None:
                            # L2-normalized running average
                            avg = t["emb"] * 0.7 + emb * 0.3
                            import numpy as _np
                            t["emb"] = avg / _np.linalg.norm(avg)
                        else:
                            t["emb"] = emb
                    t["last_box"] = box
                    t["count"] += 1
                else:
                    thumb = frame[y:y+bh, x:x+bw]
                    _, enc = cv2.imencode(".jpg", cv2.resize(thumb, (96, 96)) if thumb.size else thumb)
                    tracks.append({
                        "hist": hist, "emb": emb, "last_box": box, "count": 1,
                        "thumb_b64": _b64.b64encode(enc.tobytes()).decode(),
                    })
        # drop single-appearance tracks only for videos (false positives);
        # for still images there's just one frame so keep everything
        if is_video:
            tracks = [t for t in tracks if t["count"] >= 2]
        faces = [{"id": i, "thumbnail": t["thumb_b64"], "appearances": t["count"]}
                 for i, t in enumerate(tracks)]
        # Cache track embeddings by video hash for blur to reuse (ensures ID match)
        import hashlib as _hh2
        _vh2 = _hh2.md5(data).hexdigest()
        _scan_track_cache[_vh2] = [t.get("emb") for t in tracks]
        _scan_track_cache[_vh2 + "_full"] = tracks
        _save_cache()
        if len(_scan_track_cache) > 20:
            _scan_track_cache.pop(next(iter(_scan_track_cache)))
        return JSONResponse({"faces": faces, "count": len(faces),
                                 "_debug": {"frames_sampled": _dbg_frames,
                                            "total_detections": _dbg_detections,
                                            "tracks_before_filter": len(tracks) + len([t for t in tracks if t["count"] < 2]) if is_video else len(tracks)}})
    finally:
        cleanup(in_path)

@app.post("/api/face-blur")
async def face_blur(
    file: UploadFile = File(...),
    intensity: int = Form(30),
    face_ids: str = Form(""),  # JSON list of track IDs to blur; empty = blur all
    async_job: bool = Form(False, alias="async"),  # New frontend sends async=true
):
    """Blur faces. If async=true, returns {job_id} immediately (avoids Cloudflare timeout).
    Poll GET /api/face-blur-status/{job_id}, then download from /api/face-blur-result/{job_id}.
    If async=false (old frontend), processes synchronously and returns the video blob."""
    data = await file.read()
    fname = file.filename
    if async_job:
        job_id = uuid.uuid4().hex[:12]
        _blur_jobs[job_id] = {"status": "processing", "progress": 0, "result_path": None, "error": None}
        loop = asyncio.get_event_loop()
        loop.run_in_executor(_face_pool, _run_blur_job, job_id, data, fname, intensity, face_ids)
        return {"job_id": job_id, "status": "processing"}
    else:
        # Sync mode for old frontend (may timeout on long videos via Cloudflare)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_face_pool, _do_face_blur, data, fname, intensity, face_ids)

def _run_blur_job(job_id: str, data: bytes, filename: str, intensity: int, face_ids: str):
    try:
        _blur_jobs[job_id]["progress"] = 10
        result = _do_face_blur(data, filename, intensity, face_ids)
        # _do_face_blur returns a Response; we need the file path instead
        # For now, store the response and extract path
        _blur_jobs[job_id]["status"] = "done"
        _blur_jobs[job_id]["progress"] = 100
        # The result is a Response with the file content; save job ref
        _blur_jobs[job_id]["_response"] = result
    except Exception as e:
        _blur_jobs[job_id]["status"] = "error"
        _blur_jobs[job_id]["error"] = str(e)

@app.get("/api/face-blur-status/{job_id}")
async def face_blur_status(job_id: str):
    job = _blur_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "status": job["status"], "progress": job["progress"], "error": job["error"]}

@app.get("/api/face-blur-result/{job_id}")
async def face_blur_result(job_id: str):
    job = _blur_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(400, f"Job not ready: {job['status']}")
    resp = job.get("_response")
    if resp:
        # Clean up job after download
        del _blur_jobs[job_id]
        return resp
    raise HTTPException(500, "Result not available")

def _do_selective_blur_clean(in_path, out_path, final_video_path, intensity, selected_ids):
    """Clean two-pass selective blur using ONLY SCRFD + embeddings.
    Pass 1: Identify all persons by clustering embeddings from sampled frames.
    Pass 2: For each frame, detect faces, match embeddings to persons, blur selected.
    No MediaPipe, no histograms, no IoU tracking, no cache — self-contained and correct."""
    import numpy as _np
    cap = cv2.VideoCapture(str(in_path))
    if not cap.isOpened():
        raise HTTPException(400, "Could not open video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    scrfd = _get_scrfd()

    # PASS 1: Build person gallery — {pid: [embeddings]}
    persons = []  # list of {'embs': [...], 'count': n}
    n_samples = min(40, max(15, int(total / fps * 2)))
    step = max(1, total // n_samples)
    for idx in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            continue
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (640, int(h*640/w))) if w > 640 else frame
        for (x, y, bw, bh, emb) in _detect_faces_scrfd(scrfd, small):
            if emb is None:
                continue
            best, bs = -1, 0
            for i, p in enumerate(persons):
                # Max similarity to any stored embedding (handles angle variations)
                sim = max(_emb_sim(emb, e) for e in p['embs'])
                if sim > bs:
                    bs, best = sim, i
            if best >= 0 and bs > 0.35:
                p = persons[best]
                p['embs'].append(emb)
                if len(p['embs']) > 10:
                    p['embs'] = p['embs'][-10:]  # keep last 10
                p['count'] += 1
            else:
                persons.append({'embs': [emb], 'count': 1})
    cap.release()
    # Filter weak persons
    persons = [p for p in persons if p['count'] >= 2]
    # Map selected_ids to person indices (they should align with scan order)
    # If counts differ, use all persons
    valid_ids = [i for i in selected_ids if i < len(persons)]
    if not valid_ids:
        raise HTTPException(400, "Selected faces not found in video")

    # PASS 2: Blur frames with smooth tracking and elliptical blur
    cap = cv2.VideoCapture(str(in_path))
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))
    k = max(1, intensity // 2)
    frame_idx = 0
    # Smooth box tracking: {pid: (x, y, w, h)} with exponential moving average
    smooth_boxes = {}
    def _blur_oval(frm, x, y, bw, bh):
        # Always use oval/elliptical blur
        sx = max(0, x - int(bw*0.2)); sy = max(0, y - int(bh*0.2))
        ex = min(width, x + bw + int(bw*0.2)); ey = min(height, y + bh + int(bh*0.2))
        if ex <= sx or ey <= sy:
            return
        roi = frm[sy:ey, sx:ex]
        blurred = cv2.GaussianBlur(roi, (k*2+1, k*2+1), 0)
        mask = _np.zeros((ey-sy, ex-sx), dtype=_np.uint8)
        # Oval shape: wider than tall for natural face coverage
        cv2.ellipse(mask, ((ex-sx)//2, (ey-sy)//2),
                    (int((ex-sx)*0.45), int((ey-sy)*0.55)), 0, 0, 360, 255, -1)
        mask = cv2.GaussianBlur(mask, (21, 21), 0)
        m3 = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR) / 255.0
        frm[sy:ey, sx:ex] = (roi*(1-m3) + blurred*m3).astype(_np.uint8)

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        # Detect every 3rd frame (smoother, less jitter)
        if frame_idx % 3 == 0:
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (640, int(h*640/w))) if w > 640 else frame
            sc = 640 / w if w > 640 else 1.0
            detected = {}  # pid -> (x, y, w, h)
            for (sx, sy, sw, sh, emb) in _detect_faces_scrfd(scrfd, small):
                if emb is None:
                    continue
                for pid in valid_ids:
                    p = persons[pid]
                    if max(_emb_sim(emb, e) for e in p['embs']) > 0.30:
                        detected[pid] = (int(sx/sc), int(sy/sc), int(sw/sc), int(sh/sc))
                        break
            # Update smooth boxes with exponential moving average (alpha=0.4)
            for pid, (nx, ny, nw, nh) in detected.items():
                if pid in smooth_boxes:
                    ox, oy, ow, oh = smooth_boxes[pid]
                    smooth_boxes[pid] = (
                        int(ox*0.6 + nx*0.4), int(oy*0.6 + ny*0.4),
                        int(ow*0.6 + nw*0.4), int(oh*0.6 + nh*0.4),
                    )
                else:
                    smooth_boxes[pid] = (nx, ny, nw, nh)
            # Remove persons not seen for a while (keep for 6 frames = 2 detection cycles)
            # (simple: keep all, they fade naturally if not re-detected)
        # Apply oval blur using smoothed boxes (every frame, no blinking)
        for pid, (x, y, bw, bh) in smooth_boxes.items():
            _blur_oval(frame, x, y, bw, bh)
        out.write(frame)
        frame_idx += 1
    cap.release()
    out.release()

    # Combine with audio
    cmd = [
        "ffmpeg", "-y",
        "-i", str(out_path),
        "-i", str(in_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart",
        str(final_video_path),
    ]
    subprocess.run(cmd, capture_output=True)
    content = final_video_path.read_bytes()
    from fastapi.responses import Response
    return Response(
        content=content,
        media_type="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="face_blurred.mp4"',
            "X-Filename": "face_blurred.mp4",
        },
    )


def _do_face_blur(data: bytes, filename: str, intensity: int, face_ids: str):
    suffix = Path(filename).suffix.lower()
    is_video = suffix in [".mp4", ".mov", ".avi", ".mkv", ".webm"]
    in_path = temp_path(suffix if suffix else (".mp4" if is_video else ".jpg"))
    out_path = temp_path(suffix if suffix else (".mp4" if is_video else ".jpg"))
    final_video_path = temp_path(".mp4")
    
    try:
        in_path.write_bytes(data)

        # For selective video blur, use the clean two-pass implementation
        import json as _js
        try:
            _sids = set(_js.loads(face_ids)) if face_ids.strip() else None
        except:
            _sids = None
        if is_video and _sids is not None:
            return _do_selective_blur_clean(in_path, out_path, final_video_path, intensity, _sids)

        k = max(intensity | 1, 3)


        if not is_video:
            # Handle Image
            img = cv2.imread(str(in_path))
            if img is None: raise HTTPException(400, "Invalid image")
            
            h, w = img.shape[:2]
            mp_detector_img = _get_mp_detector()
            boxes_img = _detect_faces_mp(mp_detector_img, img)
            k_img = max(1, intensity // 2)
            for (x, y, bw, bh) in boxes_img:
                sx = max(0, x - int(bw * 0.15)); sy = max(0, y - int(bh * 0.15))
                ex = min(w, x + bw + int(bw * 0.15)); ey = min(h, y + bh + int(bh * 0.15))
                if ex > sx and ey > sy:
                    roi = img[sy:ey, sx:ex]
                    blurred = cv2.GaussianBlur(roi, (k_img*2+1, k_img*2+1), 0)
                    mask = np.zeros((ey-sy, ex-sx), dtype=np.uint8)
                    cv2.ellipse(mask, ((ex-sx)//2, (ey-sy)//2), ((ex-sx)//2, (ey-sy)//2), 0, 0, 360, 255, -1)
                    mask = cv2.GaussianBlur(mask, (15, 15), 0)
                    m3 = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR) / 255.0
                    img[sy:ey, sx:ex] = (roi*(1-m3) + blurred*m3).astype(np.uint8)
            cv2.imwrite(str(out_path), img)
            
            content = out_path.read_bytes()
            from fastapi.responses import Response
            return Response(
                content=content, media_type="image/jpeg",
                headers={"Content-Disposition": 'attachment; filename="face_blurred.jpg"', "X-Filename": "face_blurred.jpg"}
            )
        else:
            # Handle Video
            cap = cv2.VideoCapture(str(in_path))
            if not cap.isOpened(): raise HTTPException(400, "Could not open video")
            
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            
            out = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))
            import json as _json
            try:
                selected_ids = set(_json.loads(face_ids)) if face_ids.strip() else None
            except Exception:
                selected_ids = None  # invalid -> blur all
            mp_detector_blur = _get_mp_detector()
            scrfd_ref = _get_scrfd()
            # Build reference tracks if selective mode (same embedding clustering as face-scan)
            ref_tracks = []  # [{hist, emb}]
            if selected_ids is not None:
                cap2 = cv2.VideoCapture(str(in_path))
                total2 = int(cap2.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                fps2 = cap2.get(cv2.CAP_PROP_FPS) or 30
                n2 = min(40, max(12, int(total2 / fps2 * 3)))
                step2 = max(1, total2 // n2)
                for idx2 in range(0, total2, step2):
                    cap2.set(cv2.CAP_PROP_POS_FRAMES, idx2)
                    ret2, f2 = cap2.read()
                    if not ret2: break
                    h2, w2 = f2.shape[:2]
                    if w2 > 640:
                        s2 = 640 / w2
                        f2 = cv2.resize(f2, (640, int(h2 * s2)))
                    for (x, y, bw, bh, emb) in _detect_faces_scrfd(scrfd_ref, f2):
                        box = (x, y, bw, bh)
                        hist = _face_hist(f2, box)
                        best, bs = -1, 0
                        for i, t in enumerate(ref_tracks):
                            sim = _emb_sim(emb, t.get("emb"))
                            corr = cv2.compareHist(hist, t["hist"], cv2.HISTCMP_CORREL) if hist is not None else 0
                            score = max(sim, corr)
                            if score > bs: bs, best = score, i
                        if best >= 0 and bs > 0.5:
                            if hist is not None:
                                ref_tracks[best]["hist"] = cv2.addWeighted(ref_tracks[best]["hist"], 0.7, hist, 0.3, 0)
                            if emb is not None and ref_tracks[best].get("emb") is None:
                                ref_tracks[best]["emb"] = emb
                        else:
                            ref_tracks.append({"hist": hist, "emb": emb})
                    if len(ref_tracks) >= 20:
                        break
                cap2.release()

            def _should_blur_face(frame, box):
                """Check if a face box matches any selected face via embedding."""
                if not SELECTIVE:
                    return True
                if not _selected_embs:
                    return True
                x, y, bw, bh = box
                h, w = frame.shape[:2]
                sx = max(0, x - int(bw*0.1)); sy = max(0, y - int(bh*0.1))
                ex = min(w, x + bw + int(bw*0.1)); ey = min(h, y + bh + int(bh*0.1))
                if ex <= sx or ey <= sy:
                    return False
                crop = frame[sy:ey, sx:ex]
                if crop.size == 0:
                    return False
                # Get embedding via SCRFD on crop (fast for small crop)
                faces = _detect_faces_scrfd(scrfd_ref, crop)
                if not faces:
                    # Fallback: if no face in crop, don't blur (avoid false positives)
                    return False
                emb = faces[0][4]
                if emb is None:
                    return False
                for sel_emb in _selected_embs:
                    if _emb_sim(emb, sel_emb) > 0.25:
                        return True
                # Fallback: histogram matching for different angles/lighting after cuts
                hist = _face_hist(frame, box)
                if hist is not None:
                    import hashlib as _hh5
                    _vh5 = _hh5.md5(data).hexdigest()
                    _tracks = _scan_track_cache.get(_vh5 + "_full", [])
                    for sid in selected_ids:
                        if sid < len(_tracks):
                            th = _tracks[sid].get("hist")
                            if th is not None:
                                c = cv2.compareHist(hist, th, cv2.HISTCMP_CORREL)
                                if c > 0.45:
                                    return True
                return False

            # Both modes use MediaPipe every 3rd frame (fast). Selective mode adds
            # IoU tracking to keep identities stable across frames.
            SELECTIVE = selected_ids is not None
            # Load selected face embeddings: from cache if available, else build via scan
            _selected_embs = []
            if SELECTIVE:
                import hashlib as _hh
                _vh = _hh.md5(data).hexdigest()
                _cached = _scan_track_cache.get(_vh, [])
                if _cached:
                    for _sid in selected_ids:
                        if _sid < len(_cached) and _cached[_sid] is not None:
                            _selected_embs.append(_cached[_sid])
                # If no cache, build tracks now via scan logic
                if not _selected_embs:
                    # Build tracks from video (same as scan)
                    _tmp_cap = cv2.VideoCapture(str(in_path))
                    _tmp_total = int(_tmp_cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    _tmp_fps = _tmp_cap.get(cv2.CAP_PROP_FPS) or 30
                    _tmp_n = min(60, max(15, int(_tmp_total / _tmp_fps * 3)))
                    _tmp_step = max(1, _tmp_total // _tmp_n)
                    _tmp_frames = []
                    for _idx in range(0, _tmp_total, _tmp_step):
                        _tmp_cap.set(cv2.CAP_PROP_POS_FRAMES, _idx)
                        _ret, _f = _tmp_cap.read()
                        if _ret:
                            _h, _w = _f.shape[:2]
                            if _w > 640:
                                _f = cv2.resize(_f, (640, int(_h*640/_w)))
                            _tmp_frames.append(_f)
                        if len(_tmp_frames) >= 60:
                            break
                    _tmp_cap.release()
                    _tmp_tracks = []
                    # Inline clustering (same as scan)
                    for _fr in _tmp_frames:
                        for (_x, _y, _bw, _bh, _emb) in _detect_faces_scrfd(scrfd_ref, _fr):
                            _box = (_x, _y, _bw, _bh)
                            _hist = _face_hist(_fr, _box)
                            _best, _bs = -1, 0
                            for _i, _t in enumerate(_tmp_tracks):
                                _sim = _emb_sim(_emb, _t.get("emb"))
                                if _emb is not None and _t.get("emb") is not None:
                                    _score = _sim
                                else:
                                    _score = 0
                                if _score > _bs:
                                    _bs, _best = _score, _i
                            if _best >= 0 and _bs > 0.35:
                                _t = _tmp_tracks[_best]
                                if _emb is not None and _t.get("emb") is not None:
                                    import numpy as _npp
                                    _avg = _t["emb"] * 0.7 + _emb * 0.3
                                    _t["emb"] = _avg / _npp.linalg.norm(_avg)
                                elif _emb is not None:
                                    _t["emb"] = _emb
                                _t["count"] = _t.get("count", 0) + 1
                            else:
                                _tmp_tracks.append({"emb": _emb, "hist": _hist, "count": 1})
                    _tmp_tracks = [t for t in _tmp_tracks if t.get("count", 0) >= 2]
                    for _sid in selected_ids:
                        if _sid < len(_tmp_tracks) and _tmp_tracks[_sid].get("emb") is not None:
                            _selected_embs.append(_tmp_tracks[_sid]["emb"])
                if not _selected_embs:
                    SELECTIVE = False
            DETECT_EVERY = 2 if SELECTIVE else 3
            cached_boxes = []  # [(x, y, bw, bh, track_id or None)]
            frame_idx = 0
            k = max(1, intensity // 2)
            while True:
                ret, frame = cap.read()
                if not ret: break
                if frame_idx % DETECT_EVERY == 0:
                    small_blur = cv2.resize(frame, (480, int(frame.shape[0]*480/frame.shape[1]))) if frame.shape[1] > 480 else frame
                    _sc = 480 / frame.shape[1] if frame.shape[1] > 480 else 1.0
                    mp_boxes = [(int(x/_sc), int(y/_sc), int(w_/_sc), int(h_/_sc)) for x, y, w_, h_ in _detect_faces_mp(mp_detector_blur, small_blur)]
                    # Every 15th frame, also run SCRFD to catch faces MediaPipe misses (stronger recall)
                    if frame_idx % 15 == 0:
                        h_s, w_s = frame.shape[:2]
                        sc_small = cv2.resize(frame, (640, int(h_s*640/w_s))) if w_s > 640 else frame
                        sc_factor = 640 / w_s if w_s > 640 else 1.0
                        for (sx, sy, sw, sh, _) in _detect_faces_scrfd(scrfd_ref, sc_small):
                            sbox = (int(sx/sc_factor), int(sy/sc_factor), int(sw/sc_factor), int(sh/sc_factor))
                            # Add if not overlapping existing MediaPipe boxes
                            overlap = False
                            for (mx, my, mw, mh) in mp_boxes:
                                if _iou(sbox, (mx, my, mw, mh)) > 0.3:
                                    overlap = True
                                    break
                            if not overlap:
                                mp_boxes.append(sbox)
                    if SELECTIVE:
                        # Direct: check each box against selected embeddings (no tracking)
                        new_cached = []
                        for (nx, ny, nw, nh) in mp_boxes:
                            if _should_blur_face(frame, (nx, ny, nw, nh)):
                                new_cached.append((nx, ny, nw, nh, 1))
                            # else: skip (don't blur)
                        cached_boxes = new_cached
                    else:
                        cached_boxes = [(x, y, bw, bh, None) for (x, y, bw, bh) in mp_boxes]
                h_f, w_f = frame.shape[:2]
                for (x, y, bw, bh, tid) in cached_boxes:
                    # For selective, boxes are already filtered; for blur-all, blur everything
                    startX = max(0, x - int(bw * 0.15)); startY = max(0, y - int(bh * 0.15))
                    endX = min(w_f, x + bw + int(bw * 0.15)); endY = min(h_f, y + bh + int(bh * 0.15))
                    if endX > startX and endY > startY:
                        roi = frame[startY:endY, startX:endX]
                        blurred = cv2.GaussianBlur(roi, (k*2+1, k*2+1), 0)
                        mask = np.zeros((endY-startY, endX-startX), dtype=np.uint8)
                        cv2.ellipse(mask, ((endX-startX)//2, (endY-startY)//2),
                                    ((endX-startX)//2, (endY-startY)//2), 0, 0, 360, 255, -1)
                        mask = cv2.GaussianBlur(mask, (15, 15), 0)
                        m3 = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR) / 255.0
                        frame[startY:endY, startX:endX] = (roi*(1-m3) + blurred*m3).astype(np.uint8)
                out.write(frame)
                frame_idx += 1
                
            cap.release()
            out.release()
            
            # Combine blurred video with original audio using ffmpeg
            cmd = [
                "ffmpeg", "-y",
                "-i", str(out_path),
                "-i", str(in_path),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0?",
                str(final_video_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                # If audio merging fails (e.g., no audio), fallback to just the video
                if out_path.exists():
                    content = out_path.read_bytes()
                else:
                    raise HTTPException(500, "Video processing failed")
            else:
                content = final_video_path.read_bytes()
                
            from fastapi.responses import Response
            return Response(
                content=content, media_type="video/mp4",
                headers={"Content-Disposition": 'attachment; filename="face_blurred.mp4"', "X-Filename": "face_blurred.mp4"}
            )
            
    finally:
        cleanup(in_path, out_path, final_video_path)


# ─────────────────────────────── BPM DETECTOR ───────────────────────────────

@app.post("/api/bpm")
async def detect_bpm(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".mp3")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())

        y, sr = librosa.load(str(in_path), sr=None, mono=True, duration=60)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(round(float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo), 2))

        return {"bpm": bpm}
    finally:
        cleanup(in_path)


# ─────────────────────────────── PDF TOOLKIT ────────────────────────────────

@app.post("/api/pdf/merge")
async def pdf_merge(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    paths = []
    out_path = temp_path(".pdf")
    try:
        writer = PdfWriter()
        for f in files:
            p = temp_path(".pdf")
            paths.append(p)
            async with aiofiles.open(p, "wb") as fh:
                await fh.write(await f.read())
            reader = PdfReader(str(p))
            for page in reader.pages:
                writer.add_page(page)

        with open(out_path, "wb") as fh:
            writer.write(fh)

        background_tasks.add_task(cleanup, out_path)
        return FileResponse(str(out_path), filename="merged.pdf", media_type="application/pdf")
    finally:
        cleanup(*paths)


@app.post("/api/pdf/split")
async def pdf_split(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    in_path = temp_path(".pdf")
    out_dir = temp_path()
    out_dir.mkdir()
    zip_path = temp_path(".zip")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())

        reader = PdfReader(str(in_path))
        for i, page in enumerate(reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            page_path = out_dir / f"page_{i+1}.pdf"
            with open(page_path, "wb") as fh:
                writer.write(fh)

        shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(out_dir))
        background_tasks.add_task(cleanup, zip_path)
        return FileResponse(str(zip_path), filename="split_pages.zip", media_type="application/zip")
    finally:
        cleanup(in_path, out_dir)


@app.post("/api/pdf/compress")
async def pdf_compress(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    in_path = temp_path(".pdf")
    out_path = temp_path(".pdf")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())

        reader = PdfReader(str(in_path))
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        writer.add_metadata(reader.metadata)

        for page in writer.pages:
            page.compress_content_streams()

        with open(out_path, "wb") as fh:
            writer.write(fh)

        original_size = in_path.stat().st_size
        compressed_size = out_path.stat().st_size

        background_tasks.add_task(cleanup, out_path)
        return FileResponse(
            str(out_path),
            filename="compressed.pdf",
            media_type="application/pdf",
            headers={
                "X-Original-Size": str(original_size),
                "X-Compressed-Size": str(compressed_size),
            },
        )
    finally:
        cleanup(in_path)

# ─────────────────────────────── VOCAL REDUCER (KARAOKE) ────────────────────

import soundfile as sf

@app.post("/api/vocal-reducer")
async def vocal_reducer(file: UploadFile = File(...)):
    """Simple phase cancellation to remove center-panned vocals. Very lightweight!"""
    in_path = temp_path(Path(file.filename).suffix or ".mp3")
    out_path = temp_path(".wav")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        y, sr = librosa.load(str(in_path), sr=None, mono=False)
        if y.ndim == 1 or y.shape[0] < 2:
            raise HTTPException(400, "Audio must be stereo to reduce vocals.")
            
        # Left minus right channel removes center-panned audio (usually vocals)
        instrumental = y[0] - y[1]
        
        # Save as wav
        sf.write(str(out_path), instrumental, sr)
        
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="audio/wav",
            headers={
                "Content-Disposition": 'attachment; filename="karaoke_instrumental.wav"',
                "X-Filename": "karaoke_instrumental.wav"
            }
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── IMAGE CARTOONIFIER ─────────────────────────

@app.post("/api/cartoonify")
async def cartoonify(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".jpg")
    out_path = temp_path(".jpg")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        img = cv2.imread(str(in_path))
        if img is None:
            raise HTTPException(400, "Invalid image")
            
        # 1. Edge detection
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.medianBlur(gray, 5)
        edges = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9)
        
        # 2. Color quantization using bilateral filter
        color = cv2.bilateralFilter(img, 9, 300, 300)
        
        # 3. Combine
        cartoon = cv2.bitwise_and(color, color, mask=edges)
        
        cv2.imwrite(str(out_path), cartoon)
        
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": 'attachment; filename="cartoonified.jpg"',
                "X-Filename": "cartoonified.jpg"
            }
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── VIDEO COMPRESSOR ───────────────────────────

@app.post("/api/compress-video")
async def compress_video(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        # Compress using ffmpeg with CRF 28 (very compressed but decent quality) and fast preset
        cmd = [
            "ffmpeg", "-y",
            "-i", str(in_path),
            "-vcodec", "libx264",
            "-crf", "28",
            "-preset", "veryfast",
            "-acodec", "aac",
            str(out_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(500, result.stderr[-500:])
            
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="video/mp4",
            headers={
                "Content-Disposition": 'attachment; filename="compressed.mp4"',
                "X-Filename": "compressed.mp4"
            }
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── COLOR PALETTE EXTRACTOR ────────────────────

@app.post("/api/palette")
async def extract_palette(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".jpg")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        img = Image.open(str(in_path))
        img = img.convert("RGB")
        img = img.resize((150, 150)) # resize for speed
        
        # Quantize to 6 colors
        q_img = img.quantize(colors=6, method=2) # 2 = Fast Octree
        palette = q_img.getpalette()[:18] # 6 colors * 3 (RGB)
        
        colors = []
        for i in range(0, 18, 3):
            r, g, b = palette[i], palette[i+1], palette[i+2]
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            colors.append(hex_color)
            
        return {"colors": colors}
    finally:
        cleanup(in_path)

# ─────────────────────────────── SUBTITLE EXTRACTOR ─────────────────────────

@app.post("/api/subtitles")
async def extract_subtitles(url: str = Form(...)):
    out_dir = temp_path()
    out_dir.mkdir(parents=True)
    try:
        ydl_opts = _yt_dlp_opts(
            skip_download=True,
            writeautomaticsub=True,
            writesubtitles=True,
            subtitlesformat="srt",
            outtmpl=str(out_dir / "%(title)s.%(ext)s"),
            quiet=True,
        )
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            
        files = list(out_dir.iterdir())
        if not files:
            raise HTTPException(404, "No subtitles found for this video.")
            
        # find the subtitle file
        sub_file = None
        for f in files:
            if f.suffix in [".srt", ".vtt"]:
                sub_file = f
                break
                
        if not sub_file:
            raise HTTPException(404, "Subtitle downloaded but format is not srt/vtt.")
            
        content = sub_file.read_bytes()
        safe_name = f"subtitles_{sub_file.stem}.srt"
        
        cleanup(out_dir)
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "X-Filename": safe_name
            }
        )
    except Exception as e:
        cleanup(out_dir)
        raise HTTPException(500, str(e))

# ─────────────────────────────── BACKGROUND REMOVER (REMBG) ─────────────────────────

from rembg import remove, new_session
# u2net (~170MB) instead of the 1GB default model: runs safely on small CPUs
# without risking an out-of-memory kill of the whole service.
_BG_SESSION = None

def _bg_session():
    global _BG_SESSION
    if _BG_SESSION is None:
        _BG_SESSION = new_session("u2net")
    return _BG_SESSION

@app.post("/api/bg-remove")
async def bg_remove(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".jpg")
    out_path = temp_path(".png")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        with open(in_path, 'rb') as i:
            with open(out_path, 'wb') as o:
                input_data = i.read()
                output_data = remove(input_data, session=_bg_session())
                o.write(output_data)
                
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="bg_removed.png"',
                "X-Filename": "bg_removed.png"
            }
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── VIDEO TO MP3 ───────────────────────────────

@app.post("/api/video-to-mp3")
async def video_to_mp3(file: UploadFile = File(...)):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    out_path = temp_path(".mp3")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        cmd = ["ffmpeg", "-y", "-i", str(in_path), "-q:a", "0", "-map", "a", str(out_path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(500, f"Conversion failed: {result.stderr[-500:]}")
            
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content, media_type="audio/mpeg",
            headers={"Content-Disposition": 'attachment; filename="audio.mp3"', "X-Filename": "audio.mp3"}
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── VIDEO SPEED CHANGER ────────────────────────

@app.post("/api/video-speed")
async def video_speed(
    file: UploadFile = File(...),
    speed: float = Form(...)
):
    if speed <= 0 or speed > 4.0:
        raise HTTPException(400, "Speed must be between 0.1 and 4.0")
        
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        # Audio tempo filter (atempo) only supports 0.5 to 100.0, video (setpts) is inverse
        v_pts = 1.0 / speed
        a_tempo = speed
        
        # If speed < 0.5, we must chain atempo filters (e.g. 0.25 -> atempo=0.5,atempo=0.5)
        # For simplicity in this implementation, we will clamp audio tempo between 0.5 and 2.0 
        # or use multiple filters if outside the bounds.
        a_filter = f"atempo={a_tempo}"
        if a_tempo < 0.5:
            a_filter = f"atempo=0.5,atempo={a_tempo/0.5}"
        elif a_tempo > 2.0:
            a_filter = f"atempo=2.0,atempo={a_tempo/2.0}"
            
        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-filter_complex", f"[0:v]setpts={v_pts}*PTS[v];[0:a]{a_filter}[a]",
            "-map", "[v]", "-map", "[a]",
            str(out_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # If audio fails (e.g. video has no audio), try just video
        if result.returncode != 0:
            cmd = ["ffmpeg", "-y", "-i", str(in_path), "-filter:v", f"setpts={v_pts}*PTS", str(out_path)]
            result2 = subprocess.run(cmd, capture_output=True, text=True)
            if result2.returncode != 0:
                raise HTTPException(500, f"Speed change failed: {result.stderr[-500:]}")

        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content, media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="speed_changed.mp4"', "X-Filename": "speed_changed.mp4"}
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── REMOVE WATERMARK ───────────────────────────

@app.post("/api/remove-watermark")
async def remove_watermark(
    file: UploadFile = File(...),
    x: int = Form(...),
    y: int = Form(...),
    w: int = Form(...),
    h: int = Form(...)
):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
            
        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-vf", f"delogo=x={x}:y={y}:w={w}:h={h}",
            "-c:a", "copy",
            str(out_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(500, f"Watermark removal failed: {result.stderr[-500:]}")
            
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content, media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="no_watermark.mp4"', "X-Filename": "no_watermark.mp4"}
        )
    finally:
        cleanup(in_path, out_path)

# ─────────────────────────────── ADD SUBTITLES ──────────────────────────────

@app.post("/api/add-subtitles")
async def add_subtitles(
    file: UploadFile = File(...),
    srt: UploadFile = File(...)
):
    in_path = temp_path(Path(file.filename).suffix or ".mp4")
    srt_path = temp_path(".srt")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(in_path, "wb") as f:
            await f.write(await file.read())
        async with aiofiles.open(srt_path, "wb") as f:
            await f.write(await srt.read())
            
        # Fix paths for FFmpeg subtitles filter on Windows (needs escaped backslashes and colons)
        safe_srt = str(srt_path).replace('\\', '/').replace(':', '\\:')
            
        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-vf", f"subtitles='{safe_srt}'",
            "-c:a", "copy",
            str(out_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(500, f"Subtitle burn-in failed: {result.stderr[-500:]}")
            
        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content, media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="subtitled.mp4"', "X-Filename": "subtitled.mp4"}
        )
    finally:
        cleanup(in_path, srt_path, out_path)

# ─────────────────────────────── MERGE TWO VIDEOS ───────────────────────────

@app.post("/api/merge-videos")
async def merge_videos(
    file1: UploadFile = File(...),
    file2: UploadFile = File(...)
):
    path1 = temp_path(Path(file1.filename).suffix or ".mp4")
    path2 = temp_path(Path(file2.filename).suffix or ".mp4")
    out_path = temp_path(".mp4")
    try:
        async with aiofiles.open(path1, "wb") as f:
            await f.write(await file1.read())
        async with aiofiles.open(path2, "wb") as f:
            await f.write(await file2.read())
            
        # Re-encode to ensure different codecs/resolutions merge perfectly
        cmd = [
            "ffmpeg", "-y",
            "-i", str(path1), "-i", str(path2),
            "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
            "-map", "[outv]", "-map", "[outa]",
            str(out_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Fallback if audio fails (e.g. one video has no audio)
        if result.returncode != 0:
            cmd_no_audio = [
                "ffmpeg", "-y",
                "-i", str(path1), "-i", str(path2),
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1[outv]",
                "-map", "[outv]",
                str(out_path)
            ]
            result2 = subprocess.run(cmd_no_audio, capture_output=True, text=True)
            if result2.returncode != 0:
                raise HTTPException(500, f"Merge failed: {result.stderr[-500:]}")

        content = out_path.read_bytes()
        from fastapi.responses import Response
        return Response(
            content=content, media_type="video/mp4",
            headers={"Content-Disposition": 'attachment; filename="merged.mp4"', "X-Filename": "merged.mp4"}
        )
    finally:
        cleanup(path1, path2, out_path)

import new_tools
app.include_router(new_tools.router)

import monitor as monitor_mod
app.include_router(monitor_mod.router)

# ─────────────────────────────── SERVE BUILT FRONTEND ───────────────────────
# The Vite build (website/dist) is served from the same origin as the API, so
# the site works on any public URL with zero baked-in API host configuration.
from fastapi.staticfiles import StaticFiles
DIST_DIR = Path(__file__).resolve().parent.parent / "dist"
if DIST_DIR.is_dir():
    _assets = DIST_DIR / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        candidate = DIST_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST_DIR / "index.html")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

