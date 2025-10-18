"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

export default function Page() {
  const [files, setFiles] = useState<File[]>([]);
  const [prompt, setPrompt] = useState<string>(
    `Realistic continuation of the reference image as a forward walking video. The camera moves steadily ahead, maintaining natural height (~1.7m). The environment gradually changes in perspective and depth, with warm golden-hour lighting and soft shadows. Few people visible, peaceful ambiance. Real physical motion only — no zooms or cinematic dolly effects. Feels like walking calmly toward the scene.
Style notes:
forward linear motion, warm golden light, slow pace, natural camera sway, cinematic realism.`
  );
  const [progress, setProgress] = useState<number>(0);
  const [downUrl, setDownUrl] = useState<string>("");

  // URL бэкенда: берём из env, иначе локалка
  const API_BASE: string =
    process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

  // ------ превью выбранных изображений + корректный revoke
  const previews = useMemo(
    () => (files.length ? files.map((f) => URL.createObjectURL(f)) : []),
    [files]
  );
  useEffect(() => {
    return () => previews.forEach((u) => URL.revokeObjectURL(u));
  }, [previews]);

  // ------ чтобы освобождать предыдущий blob-видео
  const lastVideoUrl = useRef<string | null>(null);
  useEffect(() => {
    return () => {
      if (lastVideoUrl.current) URL.revokeObjectURL(lastVideoUrl.current);
    };
  }, []);

  const onFiles = (fs: FileList | null) => {
    if (!fs) return;
    setFiles(Array.from(fs));
  };

  const disabled = useMemo(
    () => !files.length || (progress > 0 && progress < 100),
    [files.length, progress]
  );

  const submit = async () => {
    const ctrl = new AbortController();
    try {
      setProgress(5);
      setDownUrl("");

      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      fd.append("prompt", prompt);

      const resp = await fetch(`${API_BASE}/api/generate`, {
        method: "POST",
        body: fd,
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(text || `HTTP ${resp.status}`);
      }

      setProgress(85);
      const blob = await resp.blob();

      if (lastVideoUrl.current) URL.revokeObjectURL(lastVideoUrl.current);
      const url = URL.createObjectURL(blob);
      lastVideoUrl.current = url;

      setDownUrl(url);
      setProgress(100);
    } catch (e: any) {
      console.error(e);
      alert(`Generation failed: ${e?.message ?? e}`);
      // если не дотянули до 100 — сброс прогресса
      if (progress > 0 && progress < 100) setProgress(0);
    }
  };

  return (
    <div className="container" style={{ padding: 24, maxWidth: 900, margin: "0 auto" }}>
      <div
        className="card"
        style={{
          padding: 20,
          borderRadius: 16,
          border: "1px solid #232327",
          background: "#0f0f12",
          color: "white",
        }}
      >
        <h1 style={{ margin: 0, fontSize: 24 }}>
          Tour I2V — генератор видео-превью для турагентств
        </h1>
        <p style={{ color: "#9aa0a6", marginTop: 8 }}>
          Загрузи фото локаций — получишь единый ролик «как будто идёшь вперёд».
        </p>

        <div style={{ margin: "16px 0" }}>
          <input
            type="file"
            accept="image/*"
            multiple
            onChange={(e) => onFiles(e.target.files)}
          />
        </div>

        {!!previews.length && (
          <div
            className="grid"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
              gap: 12,
              marginBottom: 12,
            }}
          >
            {previews.map((u, i) => (
              <div
                key={i}
                className="thumb"
                style={{
                  border: "1px solid #232327",
                  borderRadius: 12,
                  overflow: "hidden",
                }}
              >
                <img
                  src={u}
                  alt={`image-${i}`}
                  style={{ width: "100%", display: "block" }}
                />
              </div>
            ))}
          </div>
        )}

        <div style={{ marginTop: 16 }}>
          <label style={{ display: "block", marginBottom: 6 }}>Prompt</label>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={6}
            style={{
              width: "100%",
              borderRadius: 12,
              background: "#0f0f12",
              color: "white",
              border: "1px solid #232327",
              padding: 12,
              resize: "vertical",
            }}
          />
        </div>

        <div
          className="progress"
          style={{
            marginTop: 12,
            height: 6,
            background: "#1b1b20",
            borderRadius: 8,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              width: `${progress}%`,
              height: "100%",
              background: "#6ee7b7",
              transition: "width .25s ease",
            }}
          />
        </div>

        <button
          className="btn"
          disabled={disabled}
          onClick={submit}
          style={{
            marginTop: 12,
            padding: "10px 16px",
            background: disabled ? "#2a2a2f" : "#10b981",
            color: disabled ? "#8a8a90" : "#0b0b0e",
            border: "none",
            borderRadius: 12,
            cursor: disabled ? "not-allowed" : "pointer",
            fontWeight: 600,
          }}
        >
          {progress > 0 && progress < 100 ? "Генерируем…" : "Сгенерировать видео"}
        </button>

        {downUrl && (
          <div style={{ marginTop: 16 }}>
            <video
              src={downUrl}
              controls
              style={{
                width: "100%",
                borderRadius: 16,
                border: "1px solid #232327",
              }}
            />
            <div style={{ marginTop: 8 }}>
              <a
                className="btn"
                href={downUrl}
                download="tour-preview.mp4"
                style={{
                  display: "inline-block",
                  padding: "10px 16px",
                  background: "#111827",
                  color: "white",
                  border: "1px solid #232327",
                  borderRadius: 12,
                  textDecoration: "none",
                }}
              >
                Скачать MP4
              </a>
            </div>
          </div>
        )}
      </div>

      <div
        style={{
          opacity: 0.8,
          marginTop: 16,
          fontSize: 12,
          color: "#9aa0a6",
        }}
      >
        <b>Важно:</b> генерация идёт на стороннем API (MiniMax), склейка делается на
        бэкенде.
      </div>
    </div>
  );
}
