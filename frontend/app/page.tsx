"use client";

import React, { useMemo, useRef, useState } from "react";

export default function Page() {
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState(0);
  const [err, setErr] = useState("");
  const [videoUrl, setVideoUrl] = useState("");

  const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

  const previews = useMemo(() => files.map(f => URL.createObjectURL(f)), [files]);
  React.useEffect(() => () => previews.forEach(u => URL.revokeObjectURL(u)), [previews]);

  const lastVideo = useRef<string | null>(null);

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files) return;
    const fs = Array.from(e.target.files).slice(0, 2); // максимум 2
    setFiles(fs);
    setErr("");
    setVideoUrl("");
  };

  const submit = async () => {
    try {
      setErr("");
      if (!files.length) throw new Error("Добавьте хотя бы 1 фото (максимум 2).");
      setProgress(10);

      const fd = new FormData();
      files.forEach(f => fd.append("files", f));
      // модель фикс — можно не слать; оставлю явным:
      fd.append("model", "kling-v2-5-turbo");

      const resp = await fetch(`${API_BASE}/api/generate`, { method: "POST", body: fd });
      if (!resp.ok) throw new Error(await resp.text());

      setProgress(85);
      const ab = await resp.arrayBuffer();
      const blob = new Blob([ab], { type: "video/mp4" });
      if (lastVideo.current) URL.revokeObjectURL(lastVideo.current);
      const url = URL.createObjectURL(blob);
      lastVideo.current = url;
      setVideoUrl(url);
      setProgress(100);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
      setProgress(0);
    }
  };

  const disabled = (!files.length || files.length > 2) || (progress > 0 && progress < 100);

  return (
    <div style={wrap}>
      <div style={card}>
        <h1 style={h1}>Kling 2.5 — 1–2 фото → единое видео</h1>
        <p style={{ opacity: 0.75, marginTop: 4 }}>Загрузи до двух картинок. Каждая конвертируется в 5-сек. ролик и склеивается.</p>

        <div style={drop}>
          <input id="pick" type="file" multiple accept="image/*" onChange={onPick} style={{ display: "none" }} />
          <label htmlFor="pick" style={{ cursor: "pointer" }}>
            <b>Выбрать до 2 файлов</b>
          </label>
        </div>

        {!!previews.length && (
          <div style={thumbs}>
            {previews.map((u, i) => (
              <img key={i} src={u} style={thumb} alt={`p-${i}`} />
            ))}
          </div>
        )}

        <div style={{ marginTop: 16, display: "flex", gap: 12, alignItems: "center" }}>
          <button onClick={submit} disabled={disabled} style={btn}>
            {progress > 0 && progress < 100 ? "Генерируем…" : "Сгенерировать"}
          </button>
          {progress > 0 && (
            <div style={barWrap}><div style={{ ...bar, width: `${progress}%` }} /></div>
          )}
        </div>

        {!!err && <div style={errBox}>{err}</div>}

        {!!videoUrl && (
          <div style={{ marginTop: 16 }}>
            <video src={videoUrl} controls style={video} />
            <a href={videoUrl} download="tour-preview.mp4" style={{ ...btn, display: "inline-block", marginTop: 8 }}>
              Скачать MP4
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

/* стили */
const wrap: React.CSSProperties = { minHeight: "100vh", background: "#0b0d14", color: "white", padding: 24 };
const card: React.CSSProperties = { maxWidth: 1100, margin: "0 auto", background: "#0e1324", border: "1px solid #1f2a44", borderRadius: 16, padding: 20 };
const h1: React.CSSProperties = { margin: 0, fontSize: 28 };
const drop: React.CSSProperties = { marginTop: 14, padding: 16, border: "1px dashed #2b3245", background: "#0f1423", borderRadius: 10, textAlign: "center" };
const thumbs: React.CSSProperties = { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 12 };
const thumb: React.CSSProperties = { width: "100%", borderRadius: 12, border: "1px solid #232b3d" };
const btn: React.CSSProperties = { padding: "10px 16px", background: "#1d7cf2", color: "white", border: "none", borderRadius: 10, cursor: "pointer" };
const barWrap: React.CSSProperties = { flex: 1, height: 8, background: "#10182a", border: "1px solid #1f2a44", borderRadius: 999, overflow: "hidden" };
const bar: React.CSSProperties = { height: "100%", background: "#1d7cf2", transition: "width 0.3s" };
const errBox: React.CSSProperties = { marginTop: 12, padding: 10, background: "#3a0e12", border: "1px solid #702028", borderRadius: 10, color: "#ffb3b6", whiteSpace: "pre-wrap" };
const video: React.CSSProperties = { width: "100%", borderRadius: 12, border: "1px solid #1f2a44" };
