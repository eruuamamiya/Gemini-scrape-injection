#!/usr/bin/env python3
import asyncio, json, os, re, time
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

DATA_DIR     = os.environ.get("GEMINI_DATA", os.path.expanduser("~/gemini-data"))
COOKIE_FILE  = os.path.join(DATA_DIR, "cookies.json")
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
TOKEN_FILE   = os.path.join(DATA_DIR, "token")
PORT         = int(os.environ.get("PORT", 3464))
DEFAULT_ROOM = os.environ.get("DEFAULT_ROOM", "").strip()   # contoh: "hermes-sesi-utama"
MAX_WAIT     = int(os.environ.get("MAX_WAIT", 85))
MAX_TABS_PER_ACC = 5
UA           = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

app = FastAPI()
state = {"pw": None, "pw_lock": asyncio.Lock(), "pick_lock": asyncio.Lock()}
ACCOUNTS = []


def get_token():
    try:
        return open(TOKEN_FILE).read().strip()
    except FileNotFoundError:
        return None


def _read_key(fp):
    """Key per akun: file <nama>.key di sebelah <nama>.json (isi = 1 baris key)."""
    kf = os.path.splitext(fp)[0] + ".key"
    try:
        return open(kf).read().strip() or None
    except FileNotFoundError:
        return None


def bearer(authorization):
    return authorization.split(" ", 1)[-1].strip() if authorization else None


def auth(token):
    """Return (ok, acc). acc = akun yang terkunci ke key itu; None = key global (bebas pilih akun)."""
    if token:
        for a in ACCOUNTS:
            if a.get("key") and a["key"] == token:
                return True, a
    tok = get_token()
    if tok:
        return token == tok, None
    return (not any(a.get("key") for a in ACCOUNTS)), None


def _norm_cookies(raw):
    out = []
    exp = int(time.time()) + 30 * 24 * 3600
    if isinstance(raw, dict):
        raw = [{"name": n, "value": v} for n, v in raw.items()]
    for c in raw:
        c = dict(c)
        c.setdefault("domain", ".google.com")
        c.setdefault("path", "/")
        c.setdefault("secure", c["name"].startswith("__Secure-"))
        c.setdefault("httpOnly", c["name"].startswith(
            ("SID", "HSID", "SSID", "APISID", "SAPISID", "OSID", "__Secure-")))
        c.setdefault("expires", exp)
        out.append(c)
    return out


def load_accounts():
    global ACCOUNTS
    ACCOUNTS = []
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    files = sorted(os.path.join(ACCOUNTS_DIR, f)
                   for f in os.listdir(ACCOUNTS_DIR) if f.endswith(".json"))
    if not files and os.path.exists(COOKIE_FILE):
        files = [COOKIE_FILE]
    for fp in files:
        with open(fp) as f:
            text = f.read().strip()
        try:
            raw = json.loads(text)
        except Exception:
            raw = {}
            for part in text.replace("\n", ";").split(";"):
                if "=" in part:
                    n, v = part.strip().split("=", 1)
                    raw[n] = v
            if not raw:
                print(f"[ACCOUNTS] {fp}: bukan JSON & bukan cookie header — dilewati", flush=True)
                continue
            print(f"[ACCOUNTS] {fp}: cookie mentah diparse ({len(raw)} cookie)", flush=True)
        ACCOUNTS.append({
            "name": os.path.basename(fp).replace(".json", ""),
            "cookies": raw,
            "key": _read_key(fp),
            "ctx": None,
            "lock": asyncio.Lock(),
            "room_pages": {}
        })
    print(f"[ACCOUNTS] {len(ACCOUNTS)} akun dimuat: {[(a['name'], 'key' if a['key'] else 'NO-KEY') for a in ACCOUNTS]}"
          + (f" | DEFAULT_ROOM={DEFAULT_ROOM}" if DEFAULT_ROOM else ""), flush=True)


async def pick_account(room_id: str, only=None):
    async with state["pick_lock"]:
        if not ACCOUNTS:
            raise RuntimeError("Tidak ada akun yang dimuat di folder accounts.")
        if only is not None:
            only["room_pages"].setdefault(room_id, None)
            return only
        for a in ACCOUNTS:
            if room_id in a["room_pages"]:
                return a
        a = min(ACCOUNTS, key=lambda x: (x["lock"].locked(), len(x["room_pages"])))
        a["room_pages"][room_id] = None
        return a


async def ensure_pw():
    async with state["pw_lock"]:
        if not state["pw"]:
            state["pw"] = await async_playwright().start()
        return state["pw"]


async def ensure_room_page(acc, room_id: str):
    pw = await ensure_pw()
    if acc["ctx"] is None:
        profile_dir = os.path.join(ACCOUNTS_DIR, acc["name"] + "_profile")
        os.makedirs(profile_dir, exist_ok=True)
        seed = not os.listdir(profile_dir) and acc.get("cookies")
        acc["ctx"] = await pw.chromium.launch_persistent_context(
            profile_dir, headless=True,
            user_agent=UA, viewport={"width": 1280, "height": 800},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        if seed:
            await acc["ctx"].add_cookies(_norm_cookies(acc["cookies"]))
            print(f"[ACCOUNTS] {acc['name']}: cookie bibit di-inject ke profil baru", flush=True)

    page = acc["room_pages"].get(room_id)
    if page is not None and not page.is_closed():
        await page.bring_to_front()
        return page

    active_rooms = [r for r, p in acc["room_pages"].items() if p is not None and not p.is_closed()]
    if len(active_rooms) >= MAX_TABS_PER_ACC:
        oldest_room = active_rooms[0]
        old_page = acc["room_pages"].pop(oldest_room, None)
        if old_page and not old_page.is_closed():
            await old_page.close()

    if len(acc["ctx"].pages) == 1 and acc["ctx"].pages[0].url == "about:blank":
        page = acc["ctx"].pages[0]
    else:
        page = await acc["ctx"].new_page()

    acc["room_pages"][room_id] = page
    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    return page


def _guess_code_lang(code_str: str) -> str:
    """Deteksi bahasa pemrograman dari isi kode jika DOM Gemini tidak menampilkan label bahasa."""
    s = code_str.strip()
    if not s:
        return ""
    if s.startswith("<!DOCTYPE html") or re.search(r"<html[\s>]", s, re.I):
        return "html"
    if s.startswith("{") or s.startswith("["):
        try:
            json.loads(s)
            return "json"
        except Exception:
            pass
    if s.startswith("<?php"):
        return "php"
    if re.search(r"^(import\s+\w+|from\s+\w+\s+import|def\s+\w+\s*\(|class\s+\w+.*:)", s, re.M):
        return "python"
    if re.search(r"^(const|let|var|function|import\s+.*from|export\s+(default|const|function)|require\()", s, re.M):
        return "javascript"
    if re.search(r"^(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE)\b", s, re.I | re.M):
        return "sql"
    if re.search(r"^(#!/bin/(ba)?sh|sudo\s+|apt(-get)?\s+|npm\s+|pip3?\s+|docker\s+|systemctl\s+)", s, re.M):
        return "bash"
    if re.search(r"^(\.|#)[a-zA-Z0-9_-]+\s*\{", s, re.M) and ":" in s and ";" in s:
        return "css"
    return ""


def _clean_extracted_markdown(text: str) -> str:
    """Pastikan blok kode ``` selalu punya baris baru yang bersih dan bahasa yang terdeteksi."""
    text = re.sub(r"([^\r\n`])\s*(`{3,})([a-zA-Z0-9_.:-]*)\s*\n", r"\1\n\n\2\3\n", text)
    text = re.sub(r"([^\r\n`])(`{3,})(?=\s*(?:\n|$))", r"\1\n\2", text)

    def repl(match):
        fence = match.group(1)
        lang = match.group(2).strip()
        body = match.group(3)
        if not lang:
            guessed = _guess_code_lang(body)
            if guessed:
                lang = guessed
        return f"\n{fence}{lang}\n{body.strip(chr(10))}\n{fence}\n"

    text = re.sub(r"(`{3,})([^\n`]*)\n([\s\S]*?)\1", repl, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def ask_gemini(prompt: str, room_id: Optional[str] = None, only=None) -> str:
    effective_room = room_id or DEFAULT_ROOM or ""
    is_temp_room = not bool(effective_room)
    if not effective_room:
        effective_room = f"_temp-{time.time()}-{os.urandom(3).hex()}"
    acc = await pick_account(effective_room, only)

    async with acc["lock"]:
        try:
            return await _ask_gemini_inner(prompt, effective_room, acc)
        finally:
            if is_temp_room:
                temp_page = acc["room_pages"].pop(effective_room, None)
                if temp_page and not temp_page.is_closed():
                    await temp_page.close()


async def _ask_gemini_inner(prompt: str, room_id: str, acc) -> str:
    page = await ensure_room_page(acc, room_id)
    try:
        await page.wait_for_selector(".ql-editor", state="visible", timeout=25000)
    except Exception:
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded")
        await page.wait_for_selector(".ql-editor", state="visible", timeout=25000)

    prev_count = await page.evaluate("() => document.querySelectorAll('message-content').length")

    ed = page.locator(".ql-editor").first
    await ed.click()
    await page.evaluate(
        """(text) => { const ed = document.querySelector('.ql-editor');
             ed.focus(); document.execCommand('insertText', false, text); }""", prompt)
    await page.wait_for_timeout(400)

    sent = await page.evaluate("""() => {
        const btns = document.querySelectorAll('button');
        for (const b of btns) {
            const label = (b.getAttribute('aria-label')||'') + ' ' + (b.className||'');
            if (/send|submit/i.test(label) && !b.disabled) { b.click(); return true; }
        }
        return false; }""")
    if not sent:
        await page.keyboard.press("Enter")

    last, stable, t0 = "", 0, time.time()
    while time.time() - t0 < MAX_WAIT:
        await page.wait_for_timeout(1500)
        txt = await page.evaluate("""(prevCount) => {
            const els = document.querySelectorAll('message-content');
            if (els.length <= prevCount) return '';
            const root = els[els.length - 1];
            const clone = root.cloneNode(true);
            const validLangs = /^(python|py|javascript|js|html|htm|css|scss|json|java|php|go|rust|c|cpp|c\\+\\+|cs|csharp|ruby|swift|kotlin|dart|sql|bash|sh|shell|yaml|yml|xml|md|markdown|typescript|ts|tsx|jsx|vue|svelte|perl|r|scala|lua|txt|text|powershell|ps1|dockerfile)$/;
            clone.querySelectorAll('code-block').forEach(cb => {
                const code = cb.querySelector('code') || cb;
                let lang = '';
                const candidates = cb.querySelectorAll('.code-block-decoration, .code-block-decoration span, [class*="language"], [class*="lang"], header span, div > span');
                for (const el of candidates) {
                    if (code.contains(el)) continue;
                    const t = (el.textContent || '').trim().toLowerCase();
                    if (validLangs.test(t)) { lang = t; break; }
                }
                if (!lang && code.className) {
                    const m = String(code.className).match(/language-([a-z0-9_+-]+)/i);
                    if (m && validLangs.test(m[1].toLowerCase())) lang = m[1].toLowerCase();
                }
                const rawCode = (code.textContent || code.innerText || '').replace(/^\\n+|\\n+$/g, '');
                const pre = document.createElement('pre');
                pre.textContent = '\\n\\n```' + lang + '\\n' + rawCode + '\\n```\\n\\n';
                cb.replaceWith(pre);
            });
            return clone.innerText; }""", prev_count)

        if txt and txt == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last = txt

    if not last:
        raise TimeoutError(f"Gemini tidak merespons dalam {MAX_WAIT} detik (sesi login habis atau antrean penuh)")
    return _clean_extracted_markdown(last)


def _messages_to_prompt(messages):
    parts = []
    for m in messages:
        role = {"system": "Instruksi", "user": "User", "assistant": "Asisten"}.get(m.get("role"), "User")
        parts.append(f"{role}: {m.get('content','')}")
    parts.append("Jawab bagian terakhir dengan natural. "
                 "JANGAN konfirmasi atau merangkum instruksi — langsung kerjakan permintaan user terakhir sekarang. "
                 "Jika membuatkan file codingan, wajib gunakan format blok kode ```bahasa:namafile.ekstensi (contoh: ```python:app.py atau ```html:index.html).")
    return "\n\n".join(parts)


class AskBody(BaseModel):
    prompt: str
    room_id: Optional[str] = None


@app.post("/ask")
async def ask(body: AskBody, x_api_key: Optional[str] = Header(default=None)):
    ok, only = auth(x_api_key)
    if not ok:
        raise HTTPException(401, "Invalid API key")
    if not body.prompt.strip():
        raise HTTPException(400, "prompt kosong")
    try:
        answer = await ask_gemini(body.prompt.strip(), body.room_id, only)
    except Exception as e:
        raise HTTPException(502, f"Gagal memanggil Gemini: {e}")
    return {"model": "gemini-hydrogen", "room_id": body.room_id or DEFAULT_ROOM or None, "answer": answer}


@app.get("/health")
async def health():
    return {"ok": True, "accounts": len(ACCOUNTS), "default_room": DEFAULT_ROOM or None}


@app.get("/accounts")
async def accounts(x_api_key: Optional[str] = Header(default=None)):
    ok, only = auth(x_api_key)
    if not ok:
        raise HTTPException(401, "Invalid API key")
    return {
        "accounts": [
            {
                "name": a["name"],
                "active_rooms": list(a["room_pages"].keys()),
                "busy": a["lock"].locked()
            }
            for a in ([only] if only else ACCOUNTS)
        ]
    }


@app.get("/v1/models")
async def oai_models(authorization: Optional[str] = Header(default=None)):
    ok, _ = auth(bearer(authorization))
    if not ok:
        raise HTTPException(401, "Invalid API key")
    return {"object": "list", "data": [
        {"id": "gemini-hydrogen", "object": "model", "owned_by": "gemini-web",
         "capabilities": {"vision": False, "tools": False, "reasoning": True,
                          "contextWindow": 1000000, "maxOutput": 8192},
         "context_length": 1000000, "max_completion_tokens": 8192}]}


@app.post("/v1/chat/completions")
async def oai_chat(request: Request, authorization: Optional[str] = Header(default=None)):
    ok, only = auth(bearer(authorization))
    if not ok:
        raise HTTPException(401, "Invalid API key")
    body = await request.json()
    msgs = body.get("messages", [])
    room_id = request.headers.get("x-room-id") or DEFAULT_ROOM or None

    has_existing_tab = any(room_id in a["room_pages"] and a["room_pages"][room_id] is not None for a in ([only] if only else ACCOUNTS)) if room_id else False

    if room_id and has_existing_tab and msgs:
        last_user = [m for m in msgs if m.get("role") == "user"]
        prompt = last_user[-1].get("content", "") if last_user else _messages_to_prompt(msgs)
    else:
        prompt = _messages_to_prompt(msgs)

    want_stream = False  
    now = int(time.time())
    cid = "chatcmpl-gw-" + os.urandom(6).hex()

    if want_stream:
        async def gen():
            task = asyncio.create_task(ask_gemini(prompt.strip(), room_id, only))
            while True:
                done, _pending = await asyncio.wait({task}, timeout=4)
                if done:
                    break
                yield ":\n\n"
            try:
                answer = task.result()
            except Exception as e:
                err = {"id": cid, "object": "chat.completion.chunk", "created": now,
                       "model": "gemini-hydrogen",
                       "choices": [{"index": 0,
                                    "delta": {"content": f"Gemini gagal: {e}"},
                                    "finish_reason": None}]}
                yield f"data: {json.dumps(err)}\n\n"
                answer = ""
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": now,
                     "model": "gemini-hydrogen",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer},
                                  "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            done_chunk = {"id": cid, "object": "chat.completion.chunk", "created": now,
                          "model": "gemini-hydrogen",
                          "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(done_chunk)}\n\ndata: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    try:
        answer = await ask_gemini(prompt.strip(), room_id, only)
    except Exception as e:
        return JSONResponse({"error": {"message": f"Gemini gagal: {e}"}}, status_code=502)

    return {"id": cid, "object": "chat.completion", "created": now, "model": "gemini-hydrogen",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer,
                                                 "refusal": None}, "finish_reason": "stop",
                         "logprobs": None}],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(answer) // 4,
                      "total_tokens": (len(prompt) + len(answer)) // 4}}


@app.on_event("startup")
async def _startup():
    load_accounts()


if __name__ == "__main__":
    import uvicorn
    HOST = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(app, host=HOST, port=PORT)
