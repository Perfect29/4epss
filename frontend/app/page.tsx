"use client";

import React, { useMemo, useRef, useState } from "react";

export default function Page() {
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState(0);
  const [step, setStep] = useState<[number, number]>([0, 0]); // [i, N]
  const [err, setErr] = useState("");
  const [videoUrl, setVideoUrl] = useState("");

  const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

  const previews = useMemo(() => files.map(f => Object.assign(URL.createObjectURL(f), { _name: f.name })), [files]);
  React.useEffect(() => () => previews.forEach((u: any) => URL.revokeObjectURL(u as string)), [previews]);

  const lastVideo = useRef<string | null>(null);

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files) return;
    const fs = Array.from(e.target.files);
    setFiles(prev => [...prev, ...fs]); // добавляем к уже существующим
    setErr("");
    setVideoUrl("");
  };

  const removeAt = (idx: number) => setFiles(prev => prev.filter((_, i) => i !== idx));
  const clearAll = () => { setFiles([]); setVideoUrl(""); setErr(""); setProgress(0); setStep([0,0]); };

  const submit = async () => {
    try {
      setErr("");
      if (!files.length) throw new Error("Добавьте хотя бы 1 фото.");
      setProgress(8);
      setStep([0, files.length]);

      const fd = new FormData();
      files.forEach(f => fd.append("files", f));
      fd.append("model", "kling-v2-5-turbo");

      const resp = await fetch(`${API_BASE}/api/generate`, { method: "POST", body: fd });
      if (!resp.ok) throw new Error(await resp.text());

      // имитируем «пошаговый» прогресс: сервер делает много клипов внутри одного запроса,
      // снаружи можно плавно добежать до 85%
      const pump = setInterval(() => setProgress(p => (p < 85 ? p + 1 : p)), 150);

      const ab = await resp.arrayBuffer();
      clearInterval(pump);

      setProgress(93);
      const blob = new Blob([ab], { type: "video/mp4" });
      if (lastVideo.current) URL.revokeObjectURL(lastVideo.current);
      const url = URL.createObjectURL(blob);
      lastVideo.current = url;
      setVideoUrl(url);
      setProgress(100);
      setStep([files.length, files.length]);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
      setProgress(0);
      setStep([0,0]);
    }
  };

  const disabled = (!files.length) || (progress > 0 && progress < 100);

  return (
    <div style={page}>
      {/* HERO */}
      <section style={hero}>
        <div style={heroInner}>
          <div style={pill}>Tour agencies</div>
          <h1 style={title}>Create immersive walk-throughs<br/>for your next signature tour.</h1>
          <p style={subtitle}>
            Upload location shots — we’ll turn each into a forward-walking clip and stitch them into a single teaser.
          </p>
          <div style={{display:"flex", gap:12, marginTop:14}}>
            <label htmlFor="pick" style={{...btn, background:"#1d7cf2"}}>Add photos</label>
            <button onClick={clearAll} disabled={!files.length} style={{...btn, background:"transparent", border:"1px solid #2a3553"}}>
              Clear
            </button>
          </div>
          <input id="pick" type="file" multiple accept="image/*" onChange={onPick} style={{ display: "none" }} />
        </div>
      </section>

      {/* CARD */}
      <section style={card}>
        <h2 style={{margin:"0 0 8px 0"}}>Upload or choose images</h2>
        <p style={{opacity:.7, marginTop:0}}>You can add as many locations as you want — we’ll stitch them in order.</p>

        {!!previews.length && (
          <div style={grid}>
            {previews.map((u: any, i) => (
              <div key={i} style={thumbWrap}>
                <img src={u as string} alt={`p-${i}`} style={thumb}/>
                <div style={thumbBar}>
                  <span style={{opacity:.8, fontSize:12, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>{files[i].name}</span>
                  <button onClick={() => removeAt(i)} style={xBtn}>×</button>
                </div>
              </div>
            ))}
          </div>
        )}

        <div style={{display:"flex", alignItems:"center", gap:12, marginTop:16}}>
          <button onClick={submit} disabled={disabled} style={cta}>
            {progress > 0 && progress < 100 ? "Generating…" : "Generate"}
          </button>
          {progress > 0 && (
            <div style={barWrap}>
              <div style={{ ...bar, width: `${progress}%` }} />
            </div>
          )}
          {step[1] > 0 && (
            <div style={{opacity:.75, fontSize:13, minWidth:90, textAlign:"right"}}>
              clip {step[0]}/{step[1]}
            </div>
          )}
        </div>

        {!!err && <div style={errBox}>{err}</div>}

        {!!videoUrl && (
          <div style={{ marginTop: 18 }}>
            <video src={videoUrl} controls style={video}/>
            <a href={videoUrl} download="tour-preview.mp4" style={{...btn, display:"inline-block", marginTop:10}}>
              Download MP4
            </a>
          </div>
        )}
      </section>
    </div>
  );
}

/* ── styles ─────────────────────────────────── */
const page: React.CSSProperties = {
  minHeight: "100vh",
  background: "radial-gradient(1200px 600px at 60% -10%, #111b3a 0%, #0a0e16 60%)",
  color: "white",
};

const hero: React.CSSProperties = {
  padding: "56px 24px 16px",
  borderBottom: "1px solid #1b2440",
  background: "linear-gradient(180deg, rgba(17,27,58,.6), rgba(10,14,22,0) 60%)",
};
const heroInner: React.CSSProperties = { maxWidth: 1100, margin: "0 auto" };
const pill: React.CSSProperties = {
  display:"inline-block", padding:"6px 10px", borderRadius:999,
  background:"#11192f", border:"1px solid #1f2a44", fontSize:12, opacity:.9, marginBottom:10
};
const title: React.CSSProperties = { margin:0, fontSize:40, lineHeight:1.15, letterSpacing:.2 };
const subtitle: React.CSSProperties = { margin:"10px 0 0 0", opacity:.8, maxWidth:720 };

const card: React.CSSProperties = {
  maxWidth: 1100, margin: "18px auto 40px", background:"#0b1022",
  border: "1px solid #1f2a44", borderRadius: 16, padding: 20,
  boxShadow: "0 10px 30px rgba(0,0,0,.35)"
};

const grid: React.CSSProperties = {
  display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(180px, 1fr))", gap:12, marginTop:12
};
const thumbWrap: React.CSSProperties = { border:"1px solid #222c49", borderRadius:12, overflow:"hidden", background:"#0e1430" };
const thumb: React.CSSProperties = { width:"100%", height:160, objectFit:"cover", display:"block" };
const thumbBar: React.CSSProperties = { display:"flex", alignItems:"center", justifyContent:"space-between", padding:"6px 8px" };
const xBtn: React.CSSProperties = { background:"transparent", border:"1px solid #2a3553", color:"#9fb2ff", borderRadius:8, cursor:"pointer", width:26, height:26, lineHeight:"22px" };

const btn: React.CSSProperties = { padding:"10px 16px", borderRadius:10, border:"none", background:"#1a2242", color:"white", cursor:"pointer" };
const cta: React.CSSProperties = { ...btn, background:"#1d7cf2" };

const barWrap: React.CSSProperties = { flex:1, height:8, background:"#0c1230", border:"1px solid #1f2a44", borderRadius:999, overflow:"hidden" };
const bar: React.CSSProperties = { height:"100%", background:"#1d7cf2", transition:"width .3s" };

const errBox: React.CSSProperties = { marginTop:12, padding:10, background:"#3a0e12", border:"1px solid #702028", borderRadius:10, color:"#ffb3b6", whiteSpace:"pre-wrap" };
const video: React.CSSProperties = { width:"100%", borderRadius:12, border:"1px solid #1f2a44" };
