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

  const API_BASE: string =
    process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

  // preview urls with proper revoke
  const previews = useMemo(() => files.map((f) => URL.createObjectURL(f)), [files]);
  useEffect(() => {
    return () => previews.forEach((u) => URL.revokeObjectURL(u));
  }, [previews]);

  // keep last video url to revoke
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

  const submit = async () => {
    try {
      setProgress(5);
      setDownUrl("");

      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      fd.append("prompt", prompt);

      const resp = await fetch(`${API_BASE}/api/generate`, {
        method: "POST",
        body: fd,
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(text || `HTTP ${resp.status}`);
      }

      setProgress(85);
      const ab = await resp.arrayBuffer();
      const blob = new Blob([ab], { type: "video/mp4" });

      if (lastVideoUrl.current) URL.revokeObjectURL(lastVideoUrl.current);
      const url = URL.createObjectURL(blob);
      lastVideoUrl.current = url;

      setDownUrl(url);
      setProgress(100);
    } catch (e: any) {
      console.error(e);
      alert(`Generation failed: ${e?.message ?? e}`);
      setProgress(0);
    }
  };

  const disabled = !files.length || (progress > 0 && progress < 100);

  return (
    <div className="container">
      <div className="card">
        <h1>Tour I2V — генератор видео-превью для турагентств</h1>
        <p style={{ color: "var(--muted)" }}>
          Загрузи фото локаций — получишь единый ролик «как будто идёшь вперёд».
        </p>

        <div style={{ margin: "16px 0" }}>
          <input type="file" accept="image/*" multiple onChange={(e) => onFiles(e.target.files)} />
        </div>

        {!!previews.length && (
          <div className="grid">
            {previews.map((u, i) => (
              <div key={i} className="thumb">
                <img src={u} alt={`image-${i}`} style={{ width: "100%", display: "block" }} />
              </div>
            ))}
          </div>
        )}

        <div style={{ marginTop: 16 }}>
          <label>Prompt</label>
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
            }}
          />
        </div>

        <div className="progress">
          <div className="progress-bar" style={{ width: `${progress}%` }} />
        </div>

        <button className="btn" disabled={disabled} onClick={submit}>
          {progress > 0 && progress < 100 ? "Генерируем…" : "Сгенерировать видео"}
        </button>

        {downUrl && (
          <div style={{ marginTop: 16 }}>
            <video src={downUrl} controls style={{ width: "100%", borderRadius: 16, border: "1px solid #232327" }} />
            <div style={{ marginTop: 8 }}>
              <a className="btn" href={downUrl} download="tour-preview.mp4">
                Скачать MP4
              </a>
            </div>
          </div>
        )}
      </div>

      <div style={{ opacity: 0.8, marginTop: 16, fontSize: 12, color: "var(--muted)" }}>
        <b>Важно:</b> генерация идёт на стороннем API (MiniMax), склейка делается на бэкенде.
      </div>

      <style jsx>{`
        .container {
          max-width: 900px;
          margin: 40px auto;
          padding: 0 16px;
        }
        .card {
          background: #0b0b0e;
          border: 1px solid #232327;
          border-radius: 16px;
          padding: 20px;
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
          gap: 10px;
        }
        .thumb {
          border: 1px solid #232327;
          border-radius: 8px;
          overflow: hidden;
        }
        .btn {
          margin-top: 12px;
          padding: 10px 14px;
          background: #1d7cf2;
          color: white;
          border: none;
          border-radius: 10px;
          cursor: pointer;
        }
        .btn[disabled] {
          opacity: 0.6;
          cursor: not-allowed;
        }
        .progress {
          margin: 16px 0;
          width: 100%;
          height: 6px;
          background: #1a1a1f;
          border: 1px solid #232327;
          border-radius: 999px;
          overflow: hidden;
        }
        .progress-bar {
          height: 100%;
          background: #1d7cf2;
          transition: width 0.2s ease;
        }
      `}</style>
    </div>
  );
}
