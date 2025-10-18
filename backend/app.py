import os
import uuid
import asyncio
import tempfile
import mimetypes
import shutil
from typing import List

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from moviepy.editor import VideoFileClip, concatenate_videoclips

MINIMAX_GROUP_ID = os.getenv("MINIMAX_GROUP_ID")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_I2V_MODEL = os.getenv("MINIMAX_I2V_MODEL", "I2V-01-Director")

PROMPT_DEFAULT = (
    "Realistic continuation of the reference image as a forward walking video. "
    "The camera moves steadily ahead, maintaining natural height (~1.7m). "
    "The environment gradually changes in perspective and depth, with warm golden-hour lighting and soft shadows. "
    "Few people visible, peaceful ambiance. Real physical motion only — no zooms or cinematic dolly effects. "
    "Feels like walking calmly toward the scene.\n"
    "Style notes:\n"
    "forward linear motion, warm golden light, slow pace, natural camera sway, cinematic realism."
)

app = FastAPI(title="I2V Stitcher")

# CORS (tighten in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MINIMAX_BASE = "https://api.minimax.chat/video"  # confirm in your account/docs


def _require_env():
    if not (MINIMAX_GROUP_ID and MINIMAX_API_KEY):
        raise HTTPException(
            status_code=500,
            detail="Server is missing MINIMAX_GROUP_ID and/or MINIMAX_API_KEY env vars."
        )


async def submit_i2v(client: httpx.AsyncClient, image_path: str, prompt: str) -> str:
    """
    Submit one image to MiniMax I2V, return task_id.
    """
    url = f"{MINIMAX_BASE}/v1/tasks/i2v"  # verify endpoint
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "X-Group-Id": MINIMAX_GROUP_ID,
    }

    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    # Keep the file open only for the duration of the request
    with open(image_path, "rb") as f:
        files = {"image": (os.path.basename(image_path), f, mime)}
        data = {
            "model": MINIMAX_I2V_MODEL,
            "prompt": prompt,
            "duration": 5,
            "resolution": "720p",
            "fps": 25,
        }
        r = await client.post(url, headers=headers, data=data, files=files, timeout=300)
    r.raise_for_status()
    j = r.json()
    task_id = j.get("task_id") or j.get("id")
    if not task_id:
        raise RuntimeError(f"Unexpected submit response: {j}")
    return task_id


async def poll_result(client: httpx.AsyncClient, task_id: str) -> str:
    """
    Poll task until it succeeds or fails. Return downloadable URL.
    """
    status_url = f"{MINIMAX_BASE}/v1/tasks/{task_id}"
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "X-Group-Id": MINIMAX_GROUP_ID,
    }
    for _ in range(180):  # ~9 minutes total if sleep=3
        r = await client.get(status_url, headers=headers, timeout=60)
        r.raise_for_status()
        j = r.json()
        state = (j.get("status") or j.get("state") or "").lower()
        if state in {"succeeded", "completed", "success", "finished"}:
            url = j.get("result_url") or (j.get("outputs", [{}])[0].get("url"))
            if url:
                return url
            raise RuntimeError(f"Result ready but URL missing: {j}")
        if state in {"failed", "error"}:
            raise RuntimeError(f"Generation failed: {j}")
        await asyncio.sleep(3)
    raise TimeoutError("Generation timed out")


async def download_file(client: httpx.AsyncClient, url: str, dest_path: str):
    async with client.stream("GET", url, timeout=600) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            async for chunk in r.aiter_bytes():
                f.write(chunk)


def concat_videos_mp4(inputs: List[str], output_path: str):
    """
    Re-encode to a consistent format and concatenate. Compatible output (yuv420p).
    """
    clips = [VideoFileClip(p) for p in inputs]
    try:
        final = concatenate_videoclips(clips, method="compose")
        # MoviePy v2: use ffmpeg_params for threading/preset/pix_fmt/faststart
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
            c.close()
        try:
            final.close()  # type: ignore[has-type]
        except Exception:
            pass


@app.post("/api/generate")
async def generate_video(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    prompt: str = Form(PROMPT_DEFAULT),
):
    _require_env()

    # Save uploads to a temp folder
    workdir = tempfile.mkdtemp(prefix="i2v_")
    img_paths: List[str] = []
    try:
        for uf in files:
            ext = os.path.splitext(uf.filename or "")[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                # default to jpg if unknown
                ext = ".jpg"
            p = os.path.join(workdir, f"{uuid.uuid4().hex}{ext}")
            with open(p, "wb") as f:
                f.write(await uf.read())
            img_paths.append(p)

        out_paths: List[str] = []
        async with httpx.AsyncClient() as client:
            # Submit all tasks concurrently
            task_ids = await asyncio.gather(*[submit_i2v(client, p, prompt) for p in img_paths])
            # Poll all
            result_urls = await asyncio.gather(*[poll_result(client, tid) for tid in task_ids])
            # Download all videos
            for url in result_urls:
                vid_path = os.path.join(workdir, f"{uuid.uuid4().hex}.mp4")
                await download_file(client, url, vid_path)
                out_paths.append(vid_path)

        if not out_paths:
            raise HTTPException(status_code=500, detail="No clips were generated.")

        final_path = os.path.join(workdir, "final.mp4")
        concat_videos_mp4(out_paths, final_path)

        # Clean up the whole temp dir after the response is sent
        background_tasks.add_task(lambda: shutil.rmtree(workdir, ignore_errors=True))

        return FileResponse(final_path, media_type="video/mp4", filename="tour-agency-preview.mp4")
    except HTTPException:
        # propagate FastAPI errors
        raise
    except Exception as e:
        # clean temp dir and report
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/health")
def health():
    return {"ok": True}
