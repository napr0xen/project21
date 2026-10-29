import os, re, sqlite3, secrets, json, pathlib, mimetypes, base64, time
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime, timezone
from typing import Optional
import httpx
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext

APP_NAME = os.getenv("APP_NAME", "Crisis Management RTA")
DB_PATH = os.getenv("DATABASE_PATH", "/data/app.db")
UPLOAD_DIR = pathlib.Path(os.getenv("UPLOAD_DIR", "/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SECRET_KEY = os.getenv("SECRET_KEY", "unsafe-development-secret-change-me")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeThisNow-123!")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
login_attempts = {}
# Master key for encryption of secrets
MASTER_KEY = os.getenv("MASTER_KEY")

def get_fernet():
    if not MASTER_KEY:
        return None
    # Ensure key is 32 url-safe base64-encoded bytes
    return Fernet(MASTER_KEY)

def encrypt_value(plaintext: str) -> str:
    f = get_fernet()
    if f:
        return f.encrypt(plaintext.encode()).decode()
    return plaintext

def decrypt_value(ciphertext: str) -> str:
    f = get_fernet()
    if f:
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            return ciphertext
    return ciphertext

app = FastAPI(title=APP_NAME)

@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}

    # Let unauthenticated API requests reach their normal authentication
    # checks so they retain the expected 401 response.
    # The login form is validated separately below.
    if (
        request.method in unsafe_methods
        and request.url.path != "/login"
        and request.session.get("uid")
    ):
        expected = request.session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or invalid"},
            )

    return await call_next(request)

# SessionMiddleware must wrap the CSRF middleware so request.session
# is available during CSRF validation.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="strict",
    https_only=COOKIE_SECURE,
    httponly=True
)
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
    return response

templates = Jinja2Templates(directory="app/templates")

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chats(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          title TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'ollama',
          model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS settings(
          user_id INTEGER PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'ollama',
          model TEXT NOT NULL DEFAULT 'qwen3.5:9b', api_base TEXT NOT NULL DEFAULT '',
          api_key TEXT NOT NULL DEFAULT '', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS notes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          title TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS memories(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          content TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS documents(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          filename TEXT NOT NULL, stored_name TEXT NOT NULL, extracted_text TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS audit_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT, outcome TEXT, detail TEXT, created_at TEXT,
          FOREIGN KEY(actor_id) REFERENCES users(id) ON DELETE SET NULL
        );
        """)
        existing = c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()
        if not existing:
            c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                      (ADMIN_USERNAME, pwd.hash(ADMIN_PASSWORD), "admin", now()))
            uid = c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (uid,))

def log_event(actor_id: Optional[int], action: str, outcome: str, detail: str = ""):
    with db() as c:
        c.execute("INSERT INTO audit_log(actor_id, action, outcome, detail, created_at) VALUES(?,?,?,?,?)",
                  (actor_id, action, outcome, detail, now()))

def startup():
    init_db()

def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with db() as c:
        u = c.execute("SELECT id,username,role,active FROM users WHERE id=?", (uid,)).fetchone()
    if not u or not u["active"]:
        request.session.clear()
        return None
    return dict(u)

def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Please sign in")
    return u

def page(request, name, **kwargs):
    csrf_token = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "request": request,
            "app_name": APP_NAME,
            "csrf_token": csrf_token,
            **kwargs,
        },
    )

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    return page(request, "index.html", user=u)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", error=None)

@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    expected = request.session.get("csrf_token", "")
    if not expected or not csrf_token or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

    ip = request.client.host
    attempt = login_attempts.get(ip, {"count":0, "first":0})
    if attempt["count"] >=5 and time.time() - attempt["first"] < 900:
        raise HTTPException(429, "Too many login attempts. Try again later.")
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not u or not u["active"] or not pwd.verify(password, u["password_hash"]):
        # Record failure
        attempt["count"] += 1
        if attempt["first"] == 0:
            attempt["first"] = time.time()
        login_attempts[ip] = attempt
        log_event(None, "login", "failure", "Invalid credentials")
        return page(request, "login.html", error="Invalid username or password")
    # Successful login
    login_attempts.pop(ip, None)
    log_event(u["id"], "login", "success")
    request.session.clear()
    request.session["uid"] = u["id"]
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)

@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    expected = request.session.get("csrf_token", "")
    if not expected or not csrf_token or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    request.session.clear()
    log_event(u["id"], "logout", "success")
    return RedirectResponse("/login", status_code=303)

@app.get("/api/me")
def me(request: Request):
    return require_user(request)

@app.get("/api/chats")
def list_chats(request: Request):
    u = require_user(request)
    with db() as c:
        rows = c.execute("SELECT id,title,provider,model,created_at,updated_at FROM chats WHERE user_id=? ORDER BY updated_at DESC", (u["id"],)).fetchall()
    return [dict(x) for x in rows]

@app.post("/api/chats")
async def create_chat(request: Request):
    u = require_user(request)
    data = await request.json()
    title = (data.get("title") or "New conversation")[:120]
    provider = data.get("provider", "ollama")
    model = (data.get("model") or "qwen3.5:9b")[:160]
    with db() as c:
        cur = c.execute("INSERT INTO chats(user_id,title,provider,model,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (u["id"], title, provider, model, now(), now()))
        cid = cur.lastrowid
    return {"id": cid, "title": title, "provider": provider, "model": model}

@app.get("/api/chats/{chat_id}")
def get_chat(request: Request, chat_id: int):
    u = require_user(request)
    with db() as c:
        chat = c.execute("SELECT * FROM chats WHERE id=? AND user_id=?", (chat_id, u["id"])).fetchone()
        if not chat: raise HTTPException(404, "Conversation not found")
        messages = c.execute("SELECT role,content,created_at FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    return {"chat": dict(chat), "messages": [dict(m) for m in messages]}

@app.delete("/api/chats/{chat_id}")
def delete_chat(request: Request, chat_id: int):
    u = require_user(request)
    with db() as c:
        cur = c.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, u["id"]))
        if cur.rowcount == 0: raise HTTPException(404, "Conversation not found")
    return {"ok": True}

async def model_reply(provider, model, api_base, api_key, messages):
    if provider == "ollama":
        url = OLLAMA_BASE_URL + "/api/chat"
        payload = {"model": model or "qwen3.5:9b", "messages": messages, "stream": False}
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")
    base = (api_base or "https://api.openai.com/v1").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key: headers["Authorization"] = "Bearer " + api_key
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(base + "/chat/completions", headers=headers,
                              json={"model": model, "messages": messages, "stream": False})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

@app.post("/api/chats/{chat_id}/send")
async def send_message(request: Request, chat_id: int):
    u = require_user(request)
    data = await request.json()
    content = str(data.get("content", "")).strip()
    if not content: raise HTTPException(400, "Message cannot be empty")
    if len(content) > 50000: raise HTTPException(413, "Message too long")
    with db() as c:
        chat = c.execute("SELECT * FROM chats WHERE id=? AND user_id=?", (chat_id, u["id"])).fetchone()
        if not chat: raise HTTPException(404, "Conversation not found")
        setting = c.execute("SELECT * FROM settings WHERE user_id=?", (u["id"],)).fetchone()
        provider = data.get("provider") or (setting["provider"] if setting else "ollama") or chat["provider"]
        model = data.get("model") or (setting["model"] if setting else chat["model"])
        api_base = setting["api_base"] if setting else ""
        api_key = setting["api_key"] if setting else ""
        c.execute("INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)", (chat_id, "user", content, now()))
        history = c.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 24", (chat_id,)).fetchall()
        c.execute("UPDATE chats SET updated_at=?,provider=?,model=? WHERE id=?", (now(), provider, model, chat_id))
    messages = [{"role": row["role"], "content": row["content"]} for row in reversed(history)]
    try:
        answer = await model_reply(provider, model, api_base, api_key, messages)
    except Exception as e:
        answer = f"**Model connection failed.** Check your provider settings and that the model is running.\n\n`{type(e).__name__}: {str(e)[:300]}`"
    with db() as c:
        c.execute("INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)", (chat_id, "assistant", answer, now()))
        title = c.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()["title"]
        if title == "New conversation":
            c.execute("UPDATE chats SET title=? WHERE id=?", (content[:48], chat_id))
    return {"role": "assistant", "content": answer}

@app.get("/api/settings")
def get_settings(request: Request):
    u = require_user(request)
    with db() as c:
        s = c.execute("SELECT provider,model,api_base,CASE WHEN api_key='' THEN 0 ELSE 1 END AS has_api_key FROM settings WHERE user_id=?", (u["id"],)).fetchone()
        if not s:
            c.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (u["id"],))
            s = c.execute("SELECT provider,model,api_base,0 AS has_api_key FROM settings WHERE user_id=?", (u["id"],)).fetchone()
    return dict(s)

@app.put("/api/settings")
async def put_settings(request: Request):
    u = require_user(request)
    data = await request.json()
    provider = data.get("provider", "ollama")
    if provider not in ("ollama", "openai"): raise HTTPException(400, "Unsupported provider")
    model = str(data.get("model", "qwen3.5:9b"))[:160]
    api_base = str(data.get("api_base", ""))[:500]
    api_key = data.get("api_key")
    with db() as c:
        old = c.execute("SELECT api_key FROM settings WHERE user_id=?", (u["id"],)).fetchone()
        if api_key is None: api_key = old["api_key"] if old else ""
        api_key_encrypted = encrypt_value(api_key)
        c.execute("""INSERT INTO settings(user_id,provider,model,api_base,api_key) VALUES(?,?,?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider,model=excluded.model,api_base=excluded.api_base,api_key=excluded.api_key""",
          (u["id"], provider, model, api_base, api_key_encrypted))
    return {"ok": True}

@app.get("/api/notes")
def get_notes(request: Request):
    u = require_user(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,title,content,updated_at FROM notes WHERE user_id=? ORDER BY updated_at DESC", (u["id"],)).fetchall()]

@app.post("/api/notes")
async def save_note(request: Request):
    u = require_user(request); data = await request.json()
    title = str(data.get("title", "Untitled"))[:160]
    content = str(data.get("content", ""))[:50000]
    with db() as c:
        nid = data.get("id")
        if nid:
            cur = c.execute("UPDATE notes SET title=?,content=?,updated_at=? WHERE id=? AND user_id=?", (title,content,now(),nid,u["id"]))
            if not cur.rowcount: raise HTTPException(404, "Note not found")
        else:
            cur = c.execute("INSERT INTO notes(user_id,title,content,updated_at) VALUES(?,?,?,?)", (u["id"],title,content,now()))
            nid = cur.lastrowid
    return {"id": nid, "title": title, "content": content}

@app.delete("/api/notes/{note_id}")
def delete_note(request: Request, note_id: int):
    u = require_user(request)
    with db() as c:
        cur = c.execute("DELETE FROM notes WHERE id=? AND user_id=?", (note_id,u["id"]))
        if not cur.rowcount: raise HTTPException(404, "Note not found")
    return {"ok": True}

@app.get("/api/memories")
def get_memories(request: Request):
    u = require_user(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,content,created_at FROM memories WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()]

@app.post("/api/memories")
async def add_memory(request: Request):
    u = require_user(request); data = await request.json()
    content = str(data.get("content", "")).strip()[:3000]
    if not content: raise HTTPException(400, "Memory cannot be empty")
    with db() as c:
        cur = c.execute("INSERT INTO memories(user_id,content,created_at) VALUES(?,?,?)", (u["id"],content,now()))
    return {"id": cur.lastrowid, "content": content}

@app.delete("/api/memories/{memory_id}")
def delete_memory(request: Request, memory_id: int):
    u = require_user(request)
    with db() as c:
        cur = c.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id,u["id"]))
        if not cur.rowcount: raise HTTPException(404, "Memory not found")
    return {"ok": True}

@app.post("/api/documents")
async def upload_document(request: Request, file: UploadFile = File(...)):
    u = require_user(request)
    name = pathlib.Path(file.filename or "upload.txt").name[:180]
    suffix = pathlib.Path(name).suffix.lower()
    allowed = {".txt", ".md", ".csv", ".json", ".log"}
    if suffix not in allowed: raise HTTPException(400, "Quick build supports TXT, MD, CSV, JSON and LOG files")
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024: raise HTTPException(413, "File limit is 5 MB")
    guess = mimetypes.guess_type(name)[0]
    if guess and file.content_type and not file.content_type.startswith('text/') and file.content_type != guess:
        raise HTTPException(400, "File MIME type mismatch")
    text = data.decode("utf-8", errors="replace")
    stored = secrets.token_hex(16) + suffix
    (UPLOAD_DIR / stored).write_bytes(data)
    with db() as c:
        cur = c.execute("INSERT INTO documents(user_id,filename,stored_name,extracted_text,created_at) VALUES(?,?,?,?,?)", (u["id"],name,stored,text,now()))
    return {"id": cur.lastrowid, "filename": name}

@app.get("/api/documents")
def list_documents(request: Request):
    u = require_user(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,filename,created_at FROM documents WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()]

@app.delete("/api/documents/{doc_id}")
def delete_document(request: Request, doc_id: int):
    u = require_user(request)
    with db() as c:
        row = c.execute("SELECT stored_name FROM documents WHERE id=? AND user_id=?", (doc_id,u["id"])).fetchone()
        if not row: raise HTTPException(404, "Document not found")
        c.execute("DELETE FROM documents WHERE id=? AND user_id=?", (doc_id,u["id"]))
    try: (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
    except OSError: pass
    return {"ok": True}

@app.get("/api/search")
def search_docs(request: Request, q: str):
    u = require_user(request)
    q = q.strip()[:200]
    if not q: return []
    terms = [x for x in re.findall(r"\w+", q.lower()) if len(x) > 1][:12]
    with db() as c:
        docs = c.execute("SELECT id,filename,extracted_text FROM documents WHERE user_id=?", (u["id"],)).fetchall()
    out = []
    for d in docs:
        lower = d["extracted_text"].lower()
        score = sum(lower.count(t) for t in terms)
        if score:
            at = min([lower.find(t) for t in terms if lower.find(t) >= 0] or [0])
            out.append({"id": d["id"], "filename": d["filename"], "score": score, "excerpt": d["extracted_text"][max(0,at-100):at+350]})
    return sorted(out, key=lambda x: x["score"], reverse=True)[:10]

@app.get("/api/admin/users")
def admin_users(request: Request):
    u = require_user(request)
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,username,role,active,created_at FROM users ORDER BY id").fetchall()]

@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    u = require_user(request)
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    data = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = data.get("role", "user")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username): raise HTTPException(400, "Username must be 3–40 characters: letters, numbers, dot, underscore or hyphen")
    if len(password) < 12: raise HTTPException(400, "Password must be at least 12 characters")
    if role not in ("user", "admin"): raise HTTPException(400, "Invalid role")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)", (username,pwd.hash(password),role,now()))
            c.execute("INSERT INTO settings(user_id) VALUES(?)", (cur.lastrowid,))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Username already exists")
    return {"ok": True}

@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(request: Request, user_id: int):
    u = require_user(request)
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    data = await request.json()
    with db() as c:
        target = c.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
        if not target: raise HTTPException(404, "User not found")
        if user_id == u["id"] and data.get("active") is False: raise HTTPException(400, "Cannot deactivate your own account")
        c.execute("UPDATE users SET active=? WHERE id=?", (1 if data.get("active", True) else 0,user_id))
    return {"ok": True}

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}
