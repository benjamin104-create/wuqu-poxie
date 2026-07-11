"""
《武曲破邪傳》分鏡生圖後端 (Storyboard Image Gateway)
=====================================================
獨立專案，與「潔米爸語寶」完全無關、不共用後端。

功能：把分鏡的文字提示詞送進 Google 官方影像模型，生成港漫電影風分鏡圖。
金鑰只放後端環境變數（GEMINI_API_KEY），永不外露。同一提示詞會快取，省費用。

端點：
  GET  /            → 分鏡生圖工具網頁 (storyboard.html)
  GET  /health      → 健康檢查
  POST /imagine     → { prompt, style?, regen? } 生成一張圖，回傳 data URI

部署：
  pip install -r requirements.txt
  uvicorn server:app --host 0.0.0.0 --port 8000
  （或丟到 Render；拿到網址後直接開該網址就是工具本體）

環境變數：
  GEMINI_API_KEY              Google Gemini 金鑰（必填，需開通影像生成）
  IMAGE_MODEL                 影像模型；預設 gemini-2.5-flash-image
  IMAGE_RESPONSE_MODALITIES   選填；若用 gemini-2.0-flash-preview-image-generation
                              需設為 "TEXT,IMAGE"
"""
import hashlib
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Wuqu Storyboard Gateway")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

HERE = os.path.dirname(os.path.abspath(__file__))
STORYBOARD_HTML = os.path.join(HERE, "storyboard.html")
CACHE_DIR = os.environ.get("IMG_CACHE_DIR", os.path.join(HERE, "img_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image").strip()
IMAGE_RESPONSE_MODALITIES = os.environ.get("IMAGE_RESPONSE_MODALITIES", "").strip()

# 港漫電影分鏡統一風格：確保整套分鏡同一畫風、同一氣質
MANHUA_STYLE = (
    "{subject}. Hong Kong manhua aesthetic, bold heavy ink linework, dramatic chiaroscuro "
    "lighting, dynamic exaggerated anatomy, cinematic film still, epic mythic atmosphere, "
    "highly detailed, widescreen 16:9 cinematic framing, no text, no watermark."
)


def _extract_inline_image(data: dict):
    """從 Gemini generateContent 回應取出第一張內嵌圖片 (mime, base64)。"""
    try:
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                    return mime, inline["data"]
    except Exception:
        pass
    return None, None


@app.get("/")
def index():
    if os.path.exists(STORYBOARD_HTML):
        return FileResponse(STORYBOARD_HTML)
    return JSONResponse({"ok": True, "hint": "storyboard.html 不存在，請確認部署包含該檔"})


@app.get("/health")
def health():
    return {"ok": True, "model": IMAGE_MODEL, "key_set": bool(GEMINI_API_KEY)}


@app.post("/imagine")
async def imagine(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "請傳 JSON")
    subject = (body.get("prompt") or "").strip()
    style = (body.get("style") or "manhua").strip().lower()
    regen = bool(body.get("regen"))

    if not GEMINI_API_KEY:
        return JSONResponse({"error": "尚未設定 GEMINI_API_KEY"}, status_code=500)
    if not subject:
        return JSONResponse({"error": "缺少 prompt"}, status_code=400)
    if len(subject) > 1500:
        subject = subject[:1500]
    # style: raw=原樣用；其餘(預設 manhua)=套港漫電影風統一後綴
    prompt = subject if style == "raw" else MANHUA_STYLE.format(subject=subject)
    # regen：加隨機變異碼，強制生成新圖並略過快取（用於「重新生成」）
    if regen:
        prompt = prompt + f"  [variation {os.urandom(3).hex()}]"

    cache = os.path.join(CACHE_DIR, "img_" + hashlib.md5((IMAGE_MODEL + ":" + prompt).encode()).hexdigest() + ".b64")
    if os.path.exists(cache) and not regen:
        with open(cache, encoding="utf-8") as f:
            return JSONResponse({"image": f.read(), "cached": True})

    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + IMAGE_MODEL + ":generateContent?key=" + GEMINI_API_KEY)
    gen_cfg = {"temperature": 0.7}
    if IMAGE_RESPONSE_MODALITIES:
        gen_cfg["responseModalities"] = [m.strip() for m in IMAGE_RESPONSE_MODALITIES.split(",") if m.strip()]
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
    try:
        async with httpx.AsyncClient(timeout=180) as cli:
            r = await cli.post(url, json=payload)
    except Exception as e:
        print(f"[IMAGINE] 連線影像模型失敗：{e}", flush=True)
        return JSONResponse({"error": f"連線影像模型失敗：{e}"}, status_code=502)
    if r.status_code != 200:
        print(f"[IMAGINE] 影像模型回應 {r.status_code}: {r.text[:400]}", flush=True)
        return JSONResponse({"error": f"影像模型回應 {r.status_code}: {r.text[:200]}"}, status_code=502)

    data = r.json()
    mime, b64 = _extract_inline_image(data)
    if not b64:
        print(f"[IMAGINE] 未取得圖片：{str(data)[:400]}", flush=True)
        return JSONResponse({"error": "影像模型未回傳圖片（可能被安全過濾）"}, status_code=502)
    data_uri = f"data:{mime};base64,{b64}"
    try:
        with open(cache, "w", encoding="utf-8") as f:
            f.write(data_uri)
    except Exception:
        pass
    print(f"[IMAGINE] OK subject={subject[:50]} bytes={len(b64)}", flush=True)
    return JSONResponse({"image": data_uri})
