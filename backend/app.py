import os
import uuid
import asyncio
import tempfile
import shutil
from typing import Optional, List, Tuple

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv
from PIL import Image
import httpx
from moviepy import VideoFileClip, concatenate_videoclips

# ── ENV ────────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(__file__)
load_dotenv(os.path.join(HERE, ".env"), override=False)

HF_API_KEY: Optional[str]    = os.getenv("HF_API_KEY")
HF_API_SECRET: Optional[str] = os.getenv("HF_API_SECRET")
BASE_PUBLIC_URL: Optional[str] = os.getenv("BASE_PUBLIC_URL")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(HERE, "uploads"))

# Разрешённые параметры у Minimax
MM_DURATIONS = (6, 10)
MM_RESOLUTIONS = ("512", "768", "1080")

# Kling по умолчанию (ENHANCE — выкл, это часто лечит фейлы)
DEFAULT_MODEL = "kling-v2-5-turbo"
DEFAULT_DURATION = 5
DEFAULT_ENHANCE = False

# Промпты
PROMPT_FIXED = (
    "Cinematic forward walking camera. Natural handheld sway. "
    "Golden-hour lighting, soft shadows, realistic physical motion only. No zooms."
)
PROMPT_MINIMAL = "walking forward"

# ── APP ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="I2V Stitcher — resilient fallback ladder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ── helpers ───────────────────────────────────────────────────────────────────
def _require_env():
    if not (HF_API_KEY and HF_API_SECRET):
        raise HTTPException(status_code=500, detail="Server missing HF_API_KEY and/or HF_API_SECRET.")

def _public_url_for_filename(filename: str, request: Request) -> str:
    if BASE_PUBLIC_URL:
        return f"{BASE_PUBLIC_URL.rstrip('/')}/uploads/{filename}"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host  = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}/uploads/{filename}"

def _shrink_image_if_needed(path: str, max_side: int = 1280, target_mb: float = 4.5) -> str:
    """Сжать до разумного веса, чтобы не ловить 'Input image too large' либо скрытые фейлы."""
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w*scale), int(h*scale)), Image.Resampling.LANCZOS)
        out_path = path if path.lower().endswith((".jpg", ".jpeg")) else path + ".jpg"
        quality = 90
        while True:
            img.save(out_path, format="JPEG", quality=quality, optimize=True, progressive=True)
            size_mb = os.path.getsize(out_path)/(1024*1024)
            if size_mb <= target_mb or quality <= 60:
                break
            quality -= 5
        if out_path != path:
            try: os.remove(path)
            except: pass
        return out_path
    except Exception as e:
        print("[warn] shrink failed:", e)
        return path

def _extract_status_and_url(data: dict) -> Tuple[str, Optional[str]]:
    status = (data.get("status") or data.get("state") or "").lower()
    url = (
        data.get("video_url")
        or data.get("result_url")
        or (data.get("result") or {}).get("video_url")
        or (data.get("output") or {}).get("video_url")
    )
    # job set / jobs
    jobs = data.get("jobs")
    if isinstance(jobs, list) and jobs:
        j0 = jobs[0] or {}
        status = (j0.get("status") or status or "").lower()
        res = j0.get("results") or {}
        if isinstance(res, dict):
            for v in res.values():
                if isinstance(v, dict) and v.get("url"):
                    u = v["url"]
                    if u.endswith(".mp4") or v.get("type") in (None, "video", "mp4"):
                        url = url or u
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and it.get("url"):
                            u = it["url"]
                            if u.endswith(".mp4") or it.get("type") in (None, "video", "mp4"):
                                url = url or u
    # outputs[]
    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        first = outs[0]
        if isinstance(first, dict) and first.get("url"):
            u = first["url"]
            if u.endswith(".mp4") or first.get("type") in ("video", "mp4", None):
                url = url or u
    return status, url

def _best_error_text(text: str, json_data: Optional[dict]) -> str:
    if json_data:
        # попробуем вытащить более точный сигнал
        st, _ = _extract_status_and_url(json_data)
        if st in {"failed", "error"}:
            return f"provider_status={st}; raw={json_data}"
    return text

async def _poll_any(client: httpx.AsyncClient, any_id: str) -> str:
    headers = {"hf-api-key": HF_API_KEY, "hf-secret": HF_API_SECRET}

    async def _poll(base: str):
        url = f"{base}/{any_id}"
        for i in range(240):
            r = await client.get(url, headers=headers, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Poll error: HTTP {r.status_code} — {r.text}")
            data = r.json()
            status, video_url = _extract_status_and_url(data)
            if i % 10 == 0:
                print("[poll]", base, any_id, "→", status)
            if status in {"succeeded","completed","finished","success","done"}:
                if not video_url:
                    raise HTTPException(status_code=502, detail="Task ready but video url missing.")
                return video_url
            if status in {"failed","error"}:
                raise HTTPException(status_code=502, detail=_best_error_text("Generation failed", data))
            await asyncio.sleep(3)
        raise HTTPException(status_code=504, detail="Generation timed out")

    for base in [
        "https://platform.higgsfield.ai/v1/job-sets",
        "https://platform.higgsfield.ai/v1/tasks",
        "https://platform.higgsfield.ai/v1/jobs",
        "https://platform.higgsfield.ai/v1/generations",
    ]:
        got = await _poll(base)
        if got: return got
    raise HTTPException(status_code=502, detail="Could not resolve status endpoint")

# ── submitters ────────────────────────────────────────────────────────────────
async def _submit_kling(
    client: httpx.AsyncClient,
    image_url: str,
    duration: int = DEFAULT_DURATION,
    enhance: bool = DEFAULT_ENHANCE,
    prompt: str = PROMPT_FIXED,
) -> dict:
    url = "https://platform.higgsfield.ai/generate/kling-2-5"
    headers = {
        "Content-Type": "application/json",
        "hf-api-key": HF_API_KEY,
        "hf-secret": HF_API_SECRET,
        "accept": "application/json",
    }
    payload = {
        "params": {
            "model": DEFAULT_MODEL,
            "duration": int(duration),
            "enhance_prompt": bool(enhance),
            "prompt": prompt,
            "input_image": {"type": "image_url", "image_url": image_url},
        }
    }
    r = await client.post(url, headers=headers, json=payload, timeout=180)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Submit error: HTTP {r.status_code} — {r.text}")
    return r.json()

async def _submit_minimax(
    client: httpx.AsyncClient,
    image_url: str,
    duration: int,
    resolution: str,
    prompt: str,
    enhance_prompt: bool = True,
) -> dict:
    url = "https://platform.higgsfield.ai/v1/image2video/minimax"
    headers = {
        "Content-Type": "application/json",
        "hf-api-key": HF_API_KEY,
        "hf-secret": HF_API_SECRET,
        "accept": "application/json",
    }
    payload = {
        "params": {
            "prompt": prompt,
            "duration": duration,          # 6 | 10
            "resolution": resolution,      # "512" | "768" | "1080"
            "enhance_prompt": bool(enhance_prompt),
            "input_image": {"type": "image_url", "image_url": image_url},
        }
    }
    r = await client.post(url, headers=headers, json=payload, timeout=180)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Submit error: HTTP {r.status_code} — {r.text}")
    return r.json()

async def _submit_then_poll_with_fallback(client: httpx.AsyncClient, image_url: str) -> str:
    """
    Лестница стратегий (чтобы пробить редкие фейлы провайдера):
      1) Kling enhance=false
      2) Kling enhance=false (повтор)
      3) Minimax res=768 dur=6 prompt=fixed enhance=true
      4) Minimax res=512 dur=6 prompt=fixed enhance=true
      5) Minimax res=512 dur=10 prompt=fixed enhance=true
      6) Minimax res=512 dur=6 prompt=minimal enhance=false
    """
    # 1
    try:
        sub = await _submit_kling(client, image_url, duration=5, enhance=False, prompt=PROMPT_FIXED)
        any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
        return await _poll_any(client, any_id)
    except HTTPException as e:
        print("[kling #1 failed]", e.detail)

    # 2
    try:
        sub = await _submit_kling(client, image_url, duration=5, enhance=False, prompt=PROMPT_FIXED)
        any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
        return await _poll_any(client, any_id)
    except HTTPException as e:
        print("[kling #2 failed]", e.detail)

    # 3
    try:
        sub = await _submit_minimax(client, image_url, duration=6, resolution="768", prompt=PROMPT_FIXED, enhance_prompt=True)
        any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
        return await _poll_any(client, any_id)
    except HTTPException as e:
        print("[minimax 768/6 failed]", e.detail)

    # 4
    try:
        sub = await _submit_minimax(client, image_url, duration=6, resolution="512", prompt=PROMPT_FIXED, enhance_prompt=True)
        any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
        return await _poll_any(client, any_id)
    except HTTPException as e:
        print("[minimax 512/6 failed]", e.detail)

    # 5
    try:
        sub = await _submit_minimax(client, image_url, duration=10, resolution="512", prompt=PROMPT_FIXED, enhance_prompt=True)
        any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
        return await _poll_any(client, any_id)
    except HTTPException as e:
        print("[minimax 512/10 failed]", e.detail)

    # 6 — максимально «простая» версия
    sub = await _submit_minimax(client, image_url, duration=6, resolution="512", prompt=PROMPT_MINIMAL, enhance_prompt=False)
    any_id = sub.get("id") or sub.get("task_id") or (sub.get("task") or {}).get("id")
    return await _poll_any(client, any_id)

# ── video utils ───────────────────────────────────────────────────────────────
async def _download_file(client: httpx.AsyncClient, url: str, dest_path: str):
    async with client.stream("GET", url, timeout=600) as r:
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Download error: HTTP {r.status_code} — {await r.aread()}")
        with open(dest_path, "wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)

def _concat_videos(paths: List[str], out_path: str):
    clips = [VideoFileClip(p) for p in paths]
    try:
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(out_path, codec="libx264", audio=False, fps=25, ffmpeg_params=["-pix_fmt", "yuv420p"])
    finally:
        for c in clips:
            try: c.close()
            except: pass

# ── route ────────────────────────────────────────────────────────────────────
@app.post("/api/generate")
async def generate_video(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),          # 1..N изображений
    model: str = Form(DEFAULT_MODEL),             # не используем, оставлено на будущее
):
    _require_env()
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one image.")

    workdir = tempfile.mkdtemp(prefix="i2v_")
    try:
        # 1) сохранить и получить публичные URLы
        image_urls: List[str] = []
        for uf in files:
            ext = (os.path.splitext(uf.filename or "")[1] or ".jpg").lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"
            fname = f"{uuid.uuid4().hex}{ext}"
            dest = os.path.join(UPLOAD_DIR, fname)
            with open(dest, "wb") as f:
                f.write(await uf.read())
            shrunk = _shrink_image_if_needed(dest)
            image_urls.append(_public_url_for_filename(os.path.basename(shrunk), request))

        # 2) для каждого изображения — submit+poll с фоллбэком и скачивание
        out_paths: List[str] = []
        async with httpx.AsyncClient() as client:
            for url in image_urls:
                print("[submit image_url]", url)
                video_url = await _submit_then_poll_with_fallback(client, url)
                local_path = os.path.join(workdir, f"{uuid.uuid4().hex}.mp4")
                await _download_file(client, video_url, local_path)
                out_paths.append(local_path)

        # 3) один клип — отдаём; несколько — склейка
        final_path = out_paths[0] if len(out_paths) == 1 else os.path.join(workdir, "stitched.mp4")
        if len(out_paths) > 1:
            _concat_videos(out_paths, final_path)

        background_tasks.add_task(lambda: shutil.rmtree(workdir, ignore_errors=True))
        print("[done] video ready:", final_path)
        return FileResponse(final_path, media_type="video/mp4", filename="result.mp4")

    except HTTPException as he:
        shutil.rmtree(workdir, ignore_errors=True)
        # отдадим ровно то, что важно фронту
        return JSONResponse(status_code=502, content={"detail": he.detail})
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        print("[error]", e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/health")
def health():
    return {"ok": True}
