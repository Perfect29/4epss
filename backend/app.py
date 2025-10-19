import os
import uuid
import asyncio
import tempfile
import shutil
from typing import Optional, List

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv
from PIL import Image
import httpx

# для склейки
from moviepy import VideoFileClip, concatenate_videoclips

# ─────────── ENV ───────────
HERE = os.path.dirname(__file__)
load_dotenv(os.path.join(HERE, ".env"), override=False)

HF_API_KEY: Optional[str]    = os.getenv("HF_API_KEY")
HF_API_SECRET: Optional[str] = os.getenv("HF_API_SECRET")
BASE_PUBLIC_URL: Optional[str] = os.getenv("BASE_PUBLIC_URL")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(HERE, "uploads"))

DEFAULT_MODEL = "kling-v2-5-turbo"
DEFAULT_DURATION = 5           # 5 или 10 — здесь фикс 5 c на кадр
DEFAULT_ENHANCE = True

# фикс-промпт (поле на фронте не нужно)
FIXED_PROMPT = (
    "Cinematic forward movement through the scene. Natural handheld feel, "
    "subtle camera sway, golden-hour lighting, soft shadows, realistic motion only."
)

# ─────────── APP ───────────
app = FastAPI(title="Kling 2.5 — 1-2 images to stitched video")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ─────────── HELPERS ───────────
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

async def _submit_kling_with_image(
    client: httpx.AsyncClient,
    image_url: str,
    model: str = DEFAULT_MODEL,
    duration: int = DEFAULT_DURATION,
    enhance_prompt: bool = DEFAULT_ENHANCE,
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
            "model": model,
            "duration": int(duration),
            "enhance_prompt": bool(enhance_prompt),
            "prompt": FIXED_PROMPT,                       # ← всегда один
            "input_image": { "type": "image_url", "image_url": image_url },
        }
    }
    r = await client.post(url, headers=headers, json=payload, timeout=180)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Submit error: HTTP {r.status_code} — {r.text}")
    return r.json()

def _extract_status_and_url(data: dict):
    status = (data.get("status") or data.get("state") or "").lower()
    url = (
        data.get("video_url")
        or data.get("result_url")
        or (data.get("result") or {}).get("video_url")
        or (data.get("output") or {}).get("video_url")
    )
    jobs = data.get("jobs")
    if isinstance(jobs, list) and jobs:
        j0 = jobs[0] or {}
        status = (j0.get("status") or status or "").lower()
        res = j0.get("results") or {}
        if isinstance(res, dict):
            for v in res.values():
                if isinstance(v, dict) and v.get("url"):
                    if v.get("type") in (None, "video", "mp4") or v["url"].endswith(".mp4"):
                        url = url or v["url"]
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and it.get("url"):
                            if it.get("type") in (None, "video", "mp4") or it["url"].endswith(".mp4"):
                                url = url or it["url"]
    outs = data.get("outputs")
    if isinstance(outs, list) and outs:
        first = outs[0]
        if isinstance(first, dict) and first.get("url"):
            u = first["url"]
            if u.endswith(".mp4") or first.get("type") in ("video", "mp4", None):
                url = url or u
    return status, url

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
            status, video_url = _extract_status_and_url(r.json())
            if i % 10 == 0:
                print("[poll]", base, any_id, "→", status)
            if status in {"succeeded","completed","finished","success","done"}:
                if not video_url:
                    raise HTTPException(status_code=502, detail="Task ready but video url missing.")
                return video_url
            if status in {"failed","error"}:
                raise HTTPException(status_code=502, detail=f"Generation failed: {r.text}")
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

# ─────────── ROUTE ───────────
@app.post("/api/generate")
async def generate_video(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),    # ожидаем 1–2 файла
    model: str = Form(DEFAULT_MODEL),       # на будущее — можно скрыть на фронте
):
    """
    Принимаем 1–2 изображения. Для каждого — Kling → mp4. Потом склеиваем.
    """
    _require_env()
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one image.")
    if len(files) > 2:
        raise HTTPException(status_code=400, detail="Upload at most two images.")

    workdir = tempfile.mkdtemp(prefix="kling_")
    try:
        # 1) сохраним изображения и получим публичные URL
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

        # 2) для каждого изображения генерим видео и скачиваем
        out_paths: List[str] = []
        async with httpx.AsyncClient() as client:
            for url in image_urls:
                submit = await _submit_kling_with_image(client, url, model=model)
                any_id = submit.get("id") or submit.get("task_id") or (submit.get("task") or {}).get("id")
                if not any_id:
                    raise HTTPException(status_code=502, detail=f"Unknown submit response shape: {submit}")
                video_url = await _poll_any(client, any_id)
                local_path = os.path.join(workdir, f"{uuid.uuid4().hex}.mp4")
                await _download_file(client, video_url, local_path)
                out_paths.append(local_path)

        # 3) один файл — отдаём как есть; два — склеиваем
        if len(out_paths) == 1:
            final_path = out_paths[0]
        else:
            final_path = os.path.join(workdir, "stitched.mp4")
            _concat_videos(out_paths, final_path)

        background_tasks.add_task(lambda: shutil.rmtree(workdir, ignore_errors=True))
        print("[done] video ready:", final_path)
        return FileResponse(final_path, media_type="video/mp4", filename="result.mp4")

    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        print("[error]", e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/health")
def health():
    return {"ok": True}
