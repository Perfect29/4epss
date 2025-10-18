import os
import uuid
import asyncio
import tempfile
import mimetypes
import shutil
import json
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
import httpx
from moviepy import VideoFileClip, concatenate_videoclips
from PIL import Image  # для сжатия изображений



# ================== ENV ==================
HF_API_KEY: Optional[str] = os.getenv("HF_API_KEY")
HF_API_SECRET: Optional[str] = os.getenv("HF_API_SECRET")

# допустимо у MiniMax: duration ∈ {6,10}, resolution ∈ {"512","768","1080"}
HIGGS_DURATION = int(os.getenv("HIGGS_DURATION", "6"))
HIGGS_RESOLUTION = os.getenv("HIGGS_RESOLUTION", "768")

# публичный базовый URL (ngrok/домен). Если не задан —
# будем определять по заголовкам запроса (x-forwarded-*)
BASE_PUBLIC_URL: Optional[str] = os.getenv("BASE_PUBLIC_URL")

HERE = os.path.dirname(__file__)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(HERE, "uploads"))

# Эндпоинты Higgsfield (MiniMax)
HIGGS_SUBMIT_URL = "https://platform.higgsfield.ai/v1/image2video/minimax"

PROMPT_DEFAULT = (
    "Realistic continuation of the reference image as a forward walking video. "
    "The camera moves steadily ahead, maintaining natural height (~1.7m). "
    "The environment gradually changes in perspective and depth, with warm golden-hour lighting and soft shadows. "
    "Few people visible, peaceful ambiance. Real physical motion only — no zooms or cinematic dolly effects. "
    "Feels like walking calmly toward the scene.\n"
    "Style notes:\n"
    "forward linear motion, warm golden light, slow pace, natural camera sway, cinematic realism."
)

# ================== APP ==================
app = FastAPI(title="I2V Stitcher")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # на проде сузить
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# ================== HELPERS ==================
def _require_env():
    if not (HF_API_KEY and HF_API_SECRET):
        raise HTTPException(
            status_code=500,
            detail="Server is missing HF_API_KEY and/or HF_API_SECRET env vars.",
        )


def _normalize_params(duration: int, resolution: str) -> tuple[int, str]:
    allowed_durations = {6, 10}
    allowed_res = {"512", "768", "1080"}
    d = int(duration) if int(duration) in allowed_durations else 6
    r = str(resolution) if str(resolution) in allowed_res else "768"
    return d, r


def _public_url_for_filename(filename: str, request: Request) -> str:
    """
    Собираем публичный URL для /uploads/<file>.
    Приоритет: BASE_PUBLIC_URL -> x-forwarded-* -> host из запроса.
    """
    if BASE_PUBLIC_URL:
        base = BASE_PUBLIC_URL.rstrip("/")
        return f"{base}/uploads/{filename}"

    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host:
        host = f"{request.client.host}:{request.url.port or 8000}"
    return f"{proto}://{host}/uploads/{filename}"


def _shrink_image_if_needed(path: str, max_side: int = 1280, target_mb: float = 4.5) -> str:
    """
    Ужимаем изображение, чтобы не ловить 'Input image too large':
      - конвертируем в JPEG (RGB)
      - ограничиваем длинную сторону до max_side
      - уменьшаем качество, пока файл <= target_mb
    Возвращаем путь к результирующему файлу (может быть .jpg).
    """
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        out_path = path if path.lower().endswith((".jpg", ".jpeg")) else path + ".jpg"
        quality = 90
        while True:
            img.save(
                out_path,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            if size_mb <= target_mb or quality <= 60:
                break
            quality -= 5

        if out_path != path:
            try:
                os.remove(path)
            except Exception:
                pass
        return out_path
    except Exception as e:
        print(f"[warn] shrink failed for {path}: {e}")
        return path


def _find_any_video_url(data: dict) -> Optional[str]:
    """Извлекаем video url из разных форматов ответов."""
    # простые ключи
    for key in ("video_url", "result_url"):
        if isinstance(data.get(key), str):
            return data[key]

    # вложенные словари
    for node_key in ("result", "output"):
        node = data.get(node_key)
        if isinstance(node, dict):
            if isinstance(node.get("video_url"), str):
                return node["video_url"]

    # список outputs
    outs = data.get("outputs")
    if isinstance(outs, list):
        for o in outs:
            if isinstance(o, dict) and isinstance(o.get("url"), str):
                return o["url"]

    # job-sets: jobs -> results -> { "url": ... }
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        for j in jobs:
            res = j.get("results") if isinstance(j, dict) else None
            if isinstance(res, dict):
                for v in res.values():
                    if isinstance(v, dict) and isinstance(v.get("url"), str):
                        return v["url"]

    # рекурсивный поиск строки с .mp4 (на всякий случай)
    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                u = _walk(v)
                if u:
                    return u
        elif isinstance(x, list):
            for v in x:
                u = _walk(v)
                if u:
                    return u
        elif isinstance(x, str):
            if ".mp4" in x:
                return x
        return None

    return _walk(data)


async def _poll_higgs_any(client: httpx.AsyncClient, any_id: str) -> str:
    """
    Универсальный поллинг результата:
      1) пытаемся /v1/tasks/{id}
      2) если 404 — /v1/job-sets/{id}
    Возвращаем прямой video_url.
    """
    headers = {"hf-api-key": HF_API_KEY, "hf-secret": HF_API_SECRET}

    async def _try_poll(url_base: str) -> Optional[str]:
        for _ in range(240):  # ~12 минут при sleep=3
            r = await client.get(f"{url_base}/{any_id}", headers=headers, timeout=60)
            if r.status_code == 404:
                return None  # попробуем другой эндпоинт
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Poll error: HTTP {r.status_code} — {r.text}")

            data = r.json()
            status = (data.get("status") or data.get("state") or "").lower()

            # если status отсутствует у job-sets — смотрим статусы jobs
            if not status and isinstance(data.get("jobs"), list):
                job_statuses = [ (j or {}).get("status","").lower() for j in data["jobs"] ]
                if job_statuses and all(s in {"completed","succeeded","finished","done"} for s in job_statuses):
                    vu = _find_any_video_url(data)
                    if not vu:
                        raise HTTPException(status_code=502, detail=f"Task ready but video url missing: {data}")
                    return vu

            if status in {"succeeded","completed","success","finished","done"}:
                vu = _find_any_video_url(data)
                if not vu:
                    raise HTTPException(status_code=502, detail=f"Task ready but video url missing: {data}")
                return vu

            if status in {"failed","error"}:
                raise HTTPException(status_code=502, detail=f"Generation failed: {json.dumps(data, ensure_ascii=False)}")

            await asyncio.sleep(3)

        raise HTTPException(status_code=504, detail="Generation timed out (task/job-set)")

    # 1) tasks
    vu = await _try_poll("https://platform.higgsfield.ai/v1/tasks")
    if vu:
        return vu
    # 2) job-sets
    vu = await _try_poll("https://platform.higgsfield.ai/v1/job-sets")
    if vu:
        return vu

    raise HTTPException(status_code=502, detail="Poll error: unknown resource id (neither task nor job-set)")


async def _submit_image_url_to_higgsfield(
    client: httpx.AsyncClient,
    image_url: str,
    prompt: str,
    duration: int = HIGGS_DURATION,
    resolution: str = HIGGS_RESOLUTION,
) -> dict:
    """
    Сабмит строго JSON-ом:
      params.input_image = {"type":"image_url","image_url": "..."}
    Делаем префлайт, чтобы поймать 404/не image до запроса в Higgsfield.
    """
    # префлайт доступности изображения
    try:
        pre = await client.get(image_url, timeout=15)
        if pre.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Preflight image_url failed: HTTP {pre.status_code} ({image_url})")
        ctype = pre.headers.get("content-type", "")
        if not ctype.startswith("image/"):
            raise HTTPException(status_code=502, detail=f"Preflight: not an image content-type={ctype} for {image_url}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Preflight to image_url error: {e}")

    d, r = _normalize_params(duration, resolution)

    headers = {
        "hf-api-key": HF_API_KEY,
        "hf-secret": HF_API_SECRET,
        "accept": "application/json",
        "content-type": "application/json",
    }
    payload = {
        "params": {
            "prompt": prompt,
            "duration": d,
            "resolution": r,
            "input_image": {"type": "image_url", "image_url": image_url},
        }
    }

    resp = await client.post(HIGGS_SUBMIT_URL, headers=headers, json=payload, timeout=300)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Submit error: HTTP {resp.status_code} — {resp.text or '<no body>'}")

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"Submit non-JSON response: {resp.text[:500]}")


async def _download_file(client: httpx.AsyncClient, url: str, dest_path: str):
    async with client.stream("GET", url, timeout=600) as r:
        if r.status_code >= 400:
            body = await r.aread()
            raise HTTPException(status_code=502, detail=f"Download error: HTTP {r.status_code} — {body[:500]}")
        with open(dest_path, "wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)


def _concat_videos_mp4(inputs: List[str], output_path: str):
    clips = [VideoFileClip(p) for p in inputs]
    try:
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            output_path,
            codec="libx264",
            audio=False,
            fps=25,
            ffmpeg_params=[
                "-preset", "medium",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-threads", "4",
            ],
        )
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        try:
            final.close()  # type: ignore
        except Exception:
            pass


# ================== ROUTES ==================
@app.post("/api/generate")
async def generate_video(
    background_tasks: BackgroundTasks,
    request: Request,  # нужен для автодетекта публичного хоста
    files: List[UploadFile] = File(...),
    prompt: str = Form(PROMPT_DEFAULT),
    duration: int = Form(HIGGS_DURATION),
    resolution: str = Form(HIGGS_RESOLUTION),
):
    """
    1) принимаем 1..N изображений
    2) сохраняем в /uploads + ужимаем
    3) сабмитим каждое как input_image.image_url
    4) поллим результат (tasks или job-sets)
    5) склеиваем клипы в один MP4 и отдаём файл
    """
    _require_env()

    workdir = tempfile.mkdtemp(prefix="i2v_")
    saved_filenames: List[str] = []

    try:
        # 1) сохранить и ужать
        for uf in files:
            ext = (os.path.splitext(uf.filename or "")[1] or ".jpg").lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                ext = ".jpg"
            filename = f"{uuid.uuid4().hex}{ext}"
            dest = os.path.join(UPLOAD_DIR, filename)
            with open(dest, "wb") as f:
                f.write(await uf.read())

            new_path = _shrink_image_if_needed(dest, max_side=1280, target_mb=4.5)
            saved_filenames.append(os.path.basename(new_path))

        if not saved_filenames:
            raise HTTPException(status_code=400, detail="No input images.")

        # 2) сабмит + поллинг
        out_paths: List[str] = []
        async with httpx.AsyncClient() as client:
            job_ids: List[str] = []

            for fname in saved_filenames:
                img_url = _public_url_for_filename(fname, request)
                print("[submit image_url]", img_url)

                res = await _submit_image_url_to_higgsfield(client, img_url, prompt, duration, resolution)
                print("[submit raw]", json.dumps(res, ensure_ascii=False))

                jid = res.get("task_id") or (res.get("task") or {}).get("id") or res.get("id")
                if not jid:
                    raise HTTPException(status_code=502, detail=f"Unexpected submit result (no id): {res}")
                job_ids.append(jid)

            for jid in job_ids:
                url = await _poll_higgs_any(client, jid)
                vid_path = os.path.join(workdir, f"{uuid.uuid4().hex}.mp4")
                await _download_file(client, url, vid_path)
                out_paths.append(vid_path)

        if not out_paths:
            raise HTTPException(status_code=500, detail="No clips were generated.")

        # 3) склейка
        final_path = os.path.join(workdir, "final.mp4")
        _concat_videos_mp4(out_paths, final_path)

        # подчистка после отдачи
        background_tasks.add_task(lambda: shutil.rmtree(workdir, ignore_errors=True))

        return FileResponse(final_path, media_type="video/mp4", filename="tour-agency-preview.mp4")

    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/health")
def health():
    return {"ok": True}
