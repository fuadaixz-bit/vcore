"""V-CORE: raw Qwen + web search + file create. Telegram webhook on Vercel."""
import os, json, time, threading, re, base64, requests
import fpdf
from flask import Flask, request, jsonify

TG_TOKEN = os.environ["TG_TOKEN"]
API_BASE = os.environ.get("OPENAI_BASE", "https://api.xkiro.com/v1")
XKIRO_KEYS = [k for k in (os.environ.get("XKIRO_KEY"), os.environ.get("XKIRO_KEY2"), os.environ.get("GROQ_KEY")) if k]
# Groq primary: if GROQ_KEY set and no explicit override, use Groq brain
if os.environ.get("GROQ_KEY") and not os.environ.get("OPENAI_BASE"):
    API_BASE = "https://api.groq.com/openai/v1"
API_KEY = XKIRO_KEYS[0] if XKIRO_KEYS else ""
MODEL = os.environ.get("MODEL", "qwen/qwen3.8-max:free")
if os.environ.get("GROQ_KEY") and not os.environ.get("MODEL"): MODEL = "gpt-oss-120b"
API = "https://api.telegram.org/bot" + TG_TOKEN

app = Flask(__name__)
HIST = {}
TAGS = "WEB|FILE|REM"

SYS = """You are V-CORE, a smart direct assistant. Reply in the same language/style the user uses (casual Banglish if they write Banglish). No markdown stars.
LANGUAGE RULE: NEVER use Bengali/Arabic/Urdu/Hindi script. ALWAYS reply in roman letters only - Banglish (Bengali written in English letters, like "ajke btc er dam koto") if the user writes Banglish, or plain English. This applies to every reply, no exceptions.
Current date and time will be given in the user message context when available - trust it for "today" questions. NEVER guess prices: if search results are provided, use ONLY those numbers. If no results provided and the question is about prices/current facts, start your reply with WEB:<query>.

Tools - start your reply with a tag on its own line when needed:
WEB:<search query> - live/current info (prices, news, weather, scores). Use whenever the user asks anything current or you're unsure of a fact.
FILE:<filename> | <description of what to create> - create a TEXT DOCUMENT only: txt, pdf, csv, md. Use when the user asks for a file, document, ebook, notes, list etc. NEVER ask clarifying questions - create immediately with rich, complete content ("20 page ebook" = full multi-chapter book).
REM:<time>|<text> - set a reminder for the user. time: 10m / 2h / 3d / HH:MM (Dhaka time). Use when the user asks to be reminded (even casually: "5 min por Wake koro"). NEVER invent reminders the user did not ask for. After setting, confirm with the exact time.
WEBSITE/APP RULE: you do NOT build websites, web pages, HTML files, web apps or programs/apps. If asked, politely refuse: "ami sudhu txt/pdf/document type file banate pari, website/app banaite pari na". No exceptions.
Otherwise just answer directly. Never mention tags."""

def tg(method, **kw):
    try: return requests.post(API + "/" + method, timeout=25, **{"json": kw} if "files" not in kw else {"files": kw.pop("files"), "data": kw.pop("data", {})})
    except Exception: return None

def tg_json(method, **kw):
    try: return requests.post(API + "/" + method, json=kw, timeout=25)
    except Exception: return None

_key_idx = [0]
def _next_key():
    _key_idx[0] = (_key_idx[0] + 1) % len(XKIRO_KEYS)
    return XKIRO_KEYS[_key_idx[0]]

def llm(msgs, timeout=25):
    """Rotate across the whole key pool (main + admin-added keys) on failure/rate-limit."""
    pool = key_pool()
    last = None
    for k in pool:
        try:
            r = requests.post(API_BASE + "/chat/completions",
                headers={"Authorization": "Bearer " + k},
                json={"model": MODEL, "messages": msgs, "temperature": 0.8}, timeout=timeout)
            # 429/rate limit -> try next key immediately
            if r.status_code == 429:
                last = "rate limited"; continue
            data = r.json()
            out = (data["choices"][0]["message"].get("content") or "").strip()
            if not out and data["choices"][0]["message"].get("reasoning_content"):
                out = data["choices"][0]["message"]["reasoning_content"].strip()
            if out: return out
        except Exception as e:
            last = e
    if last: raise last
    return ""

def web_search(q):
    # news queries: Google News RSS first (fresh headlines, bing serves stale)
    if any(w in q.lower() for w in ("news", "khobor", "today", "ajke", "latest")):
        try:
            r = requests.get("https://news.google.com/rss/search?q=" + requests.utils.quote(q + " Bangladesh"), timeout=12)
            items = re.findall(r"<item>.*?<title>(.*?)</title>.*?</item>", r.text, re.S)[:6]
            if items:
                return "latest news headlines (" + q + "):\n" + "\n".join("- " + t for t in items)
        except Exception: pass
    try:
        r = requests.get("https://www.bing.com/search", params={"q": q, "count": 8},
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=12)
        items = re.findall(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>(.{0,400}?)</li>', r.text, re.S)
        out = []
        for url, title, snip in items[:6]:
            txt = re.sub(r"<[^>]+>", " ", title + " " + snip)
            txt = re.sub(r"\s+", " ", txt).strip()[:220]
            if url.startswith("http"): out.append("- " + txt + " [" + url[:100] + "]")
        if out: return "search results for '" + q + "':\n" + "\n".join(out)
    except Exception: pass
    try:
        r = requests.get("https://news.google.com/rss/search?q=" + requests.utils.quote(q), timeout=12)
        items = re.findall(r"<item>.*?<title>(.*?)</title>.*?</item>", r.text, re.S)[:6]
        if items: return "news results for '" + q + "':\n" + "\n".join("- " + re.sub(r"\s+", " ", t) for t in items)
    except Exception: pass
    return "search e kichu paoa jayni"

def extract_tag(text):
    m = re.search(r"^\s*(WEB|FILE|REM)\s*:\s*(.+)$", str(text or "").strip(), re.I | re.M)
    if not m: return None, None
    return m.group(1).upper(), m.group(2).strip()

def make_real_pdf(fname, title, content):
    """Real PDF via fpdf2: the brain's text is NOT a pdf, wrapping it in a .pdf name corrupts the file."""
    pdf = fpdf.FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.multi_cell(0, 10, title[:80])
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 12)
    for para in content.split("\n"):
        para = para.strip()
        pdf.set_x(pdf.l_margin)
        if not para:
            pdf.ln(4); continue
        # keep only latin-1 chars helvetica can render
        safe = para.encode("latin-1", "replace").decode("latin-1")
        if para.startswith("#"):
            pdf.set_font("Helvetica", "B", 15)
            pdf.multi_cell(0, 8, safe.lstrip("# "))
            pdf.set_font("Helvetica", "", 12)
        else:
            pdf.multi_cell(0, 7, safe)
    return pdf.output()

def run_file(payload, chat_id, code_override=None, user_text=None):
    try:
        fname, desc = payload.split("|", 1)
    except ValueError:
        fname, desc = "file.txt", payload
    fname = re.sub(r"[^a-zA-Z0-9._\-]", "_", fname.strip())[:40] or "file.txt"
    if code_override:
        content = code_override
    else:
        brief = (user_text or desc).strip()
        try:
            content = llm([{"role": "user", "content": "Create the complete file content for this request: " + brief +
                            "\nDo NOT ask questions or refuse - just produce the file. Rich, complete, professional content (ebooks: full chapters with headings, 3000+ words)."
                            "\nOutput ONLY the raw file content, no explanation, no code fences."}], timeout=90)
            # quality floor: a clarifying question or a stub is NOT file content - regenerate once, hard
            if len(content) < 400 or content.strip().endswith("?"):
                content = llm([{"role": "user", "content": "Write the COMPLETE content for: " + brief +
                                "\nFull depth, multiple chapters/sections, thousands of words. Output ONLY the raw content."}], timeout=90)
        except Exception as e:
            return "file banate fail: " + str(e)
    if not content: return "file banate fail: content khali"
    tg_json("sendChatAction", chat_id=chat_id, action="upload_document")
    try:
        if fname.lower().endswith(".pdf"):
            data = make_real_pdf(fname, desc.strip()[:60] or fname, content)
        else:
            data = content.encode()
        requests.post(API + "/sendDocument",
            data={"chat_id": chat_id, "caption": fname},
            files={"document": (fname, data)}, timeout=60)
        return ""
    except Exception as e:
        return "file pathate fail: " + str(e)

_LIVE_HINTS = ("price", "dam", "koto taka", "koto", "today", "ajke", "ajk", "current", "rate", "news", "score", "weather", "btc", "bitcoin", "crypto", "dollar", "usd")


def explicit_reminder_request(text):
    """Only allow reminder actions when the user's message directly asks for one."""
    low = re.sub(r"\s+", " ", (text or "").lower()).strip()
    # Routine/status mentions are not reminder instructions, even if they include a time.
    if re.search(r"\bwake\s+up\s+(kori|kor|kore|hoi|hoy|hoyechi)\b", low):
        return False
    if re.search(r"\b(don't|dont|do not|no need to|not)\s+(remind|reminder|wake)\b", low):
        return False
    patterns = (
        r"\b(remind|reminder)\s+me\b",
        r"\b(set|make|create)\s+(me\s+)?a?\s*reminder\b",
        r"\b(reminder)\s+(set|dao|daw|koro|kor|diyo|dio)\b",
        r"\b(wake|wakeup)\s+me\b",
        r"\b(wake|wakeup)\s+(me\s+)?(koro|kor|diyo|dio|up)\b",
        r"\bwake\s+up\b.{0,24}\b(at|in)\s+\d+\b",
        r"\b(mone\s+koriye|mone\s+koraiye|mone\s+korie)\s*(dio|diba|diyo|korio|koro|korba|kore\s+dio)\b",
        r"\b(mone\s+kore)\s+(dio|diba|diyo|korio|koro|korba)\b",
        r"\bremind\s+koria\s+(dio|diba|diyo|koro)\b",
    )
    return any(re.search(pattern, low) for pattern in patterns)


def explicit_file_request(text):
    """Only allow file delivery when the user explicitly asks to create or send a file."""
    low = re.sub(r"\s+", " ", (text or "").lower()).strip()
    file_word = r"(file|document|doc|pdf|txt|csv|ebook|notes?)"
    action = r"(banao|banai|banay|bana|create|make|write|generate|send|pathao|dao|daw)"
    if re.search(r"\b(context|memory|profile)\s+document\b", low):
        return False
    return bool(
        re.search(r"\b" + action + r"\b.{0,50}\b" + file_word + r"\b", low)
        or re.search(r"\b" + file_word + r"\b.{0,35}\b(" + action + r"|chai|lagbe|dorkar)\b", low)
        or re.search(r"\b(send|pathao|dao|daw)\s+(it|that|this|the\s+above)\b", low)
    )


def _ordinary_reply(msgs, fallback="bujhlam"):
    """Convert a rejected tool tag into ordinary conversation without executing it."""
    try:
        reply = llm(msgs + [{"role": "user", "content": "Reply naturally to the user's original message. Do not use WEB, FILE, or REM tags, and do not take any tool action."}])
        if reply and not extract_tag(reply)[0]:
            return reply
    except Exception:
        pass
    return fallback

def ask(chat_id, text):
    h = HIST.setdefault(str(chat_id), [])
    h.append(("user", text))
    h[:] = h[-30:]
    sys_full = SYS
    md = mem_load()
    facts = [f["text"] for f in (md.get("facts") or [])][-40:]
    if facts:
        sys_full += "\n\nThings you remember about this user (from long-term memory):\n" + "\n".join("- " + f for f in facts)
    open_rems = md.get("rems") or []
    if open_rems:
        sys_full += "\n\nActive reminders (do not re-set these):\n" + "\n".join("- " + r["text"] for r in open_rems)
    msgs = [{"role": "system", "content": sys_full}] + [{"role": r, "content": c} for r, c in h]
    # Only create a reminder when the user's own message explicitly requests one.
    if explicit_reminder_request(text):
        got = parse_banglish_rem(text)
        if got:
            at, desc = got
            mem_add_rem(at, text.strip()[:200], chat_id)
            w = _dtmod.datetime.fromtimestamp(at, TZV).strftime("%I:%M %p") if TZV else _dtmod.datetime.fromtimestamp(at).strftime("%I:%M %p")
            reply = "reminder set: " + text.strip()[:120] + " — " + w + " e mone koria dibo ✅"
            h.append(("assistant", reply)); h[:] = h[-30:]
            return reply
    # deterministic live-fact search: numbers come ONLY from real search results, never the model
    low = (text or "").lower()
    if any(k in low for k in _LIVE_HINTS) and any(w in low for w in ("price", "dam", "koto", "rate", "news", "score", "weather", "btc", "bitcoin", "crypto", "dollar")):
        try:
            q_en = llm([{"role": "user", "content": "Translate this to a short English web search query (reply with ONLY the query, max 8 words): " + text[:120]}], timeout=8)
            q_en = (q_en or text).strip().strip('"')[:120]
        except Exception:
            q_en = text[:120]
        result = web_search(q_en)
        msgs.append({"role": "system", "content": "LIVE SEARCH RESULT (use ONLY these numbers, cite nothing else):\n" + result})
    try:
        out = llm(msgs)
    except Exception:
        try:
            time.sleep(2)
            out = llm(msgs)
        except Exception:
            return "brain ekehon slow hocche (free API), ek minute por abar pathao"
    # WEBSITE/APP BLOCK (owner rule): refuse before anything else
    if re.search(r"website|web ?site|web page|webpage|web app|webapp|\bapp\b|html|\.com|site banai", low):
        reply = ("ami sudhu txt/pdf/document type file banate pari, website/app banaite pari na 🙏")
        h.append(("assistant", reply)); h[:] = h[-30:]
        return reply
    tag, payload = extract_tag(out)
    if tag == "REM" and explicit_reminder_request(text):
        parts = payload.split("|", 1)
        at = parse_rem_time(parts[0]) if len(parts) > 1 else None
        if not at:
            reply = "time ta ektu clear koro - koto por mone koraite chi?"
        else:
            txt = parts[1].strip()[:200]
            mem_add_rem(at, txt, cid)
            w = _dtmod.datetime.fromtimestamp(at, TZV).strftime("%I:%M %p") if TZV else _dtmod.datetime.fromtimestamp(at).strftime("%I:%M %p")
            reply = "reminder set: " + txt + " — " + w + " e"
        h.append(("assistant", reply)); h[:] = h[-30:]
        return reply
    if tag == "REM":
        out = _ordinary_reply(msgs, "bujhlam")
        h.append(("assistant", out)); h[:] = h[-30:]
        return out
    if tag == "WEB":
        result = web_search(payload[:150])
        msgs += [{"role": "assistant", "content": out},
                 {"role": "user", "content": "Tool result:\n" + result + "\n\nAnswer the user from this. If it doesn't contain the answer, say the search didn't find it."}]
        try: out = llm(msgs)
        except Exception: pass
        h.append(("assistant", out)); h[:] = h[-30:]
        return out
    if tag == "FILE" and explicit_file_request(text):
        err = run_file(payload, chat_id, user_text=text)
        reply = err or ("file ready 📄")
        h.append(("assistant", reply)); h[:] = h[-30:]
        return reply
    if tag == "FILE":
        out = _ordinary_reply(msgs, "bujhlam")
        h.append(("assistant", out)); h[:] = h[-30:]
        return out
    # HARD GUARD: user asked for a file/website but the brain pasted code as text instead of using FILE.
    # intercept the code and send it as a document anyway - never dump code in chat.
    _make_ask = explicit_file_request(text)
    if _make_ask:
        m_code = re.search(r"```(?:\w+)?\n(.*?)```", out, re.S)
        if m_code:
            code = m_code.group(1).strip()
            fname = "file.csv" if re.search(r",.*\n.*,", code) else "file.txt"
            mf = re.search(r"(?:file|filename)\s*[:\-]?\s*([\w\-.]+\.[a-z]+)", out, re.I)
            if mf: fname = re.sub(r"[^a-zA-Z0-9._\-]", "_", mf.group(1))
            err = run_file(fname + " | " + low[:80], chat_id, code_override=code, user_text=text)
            reply = err or ("file ready 📄")
            h.append(("assistant", reply)); h[:] = h[-30:]
            return reply
        # brain pasted code without guard trigger: strip the answer back and retry forcing FILE
        if not tag:
            out2 = llm(msgs + [{"role": "user", "content": "DO NOT write the file content in chat. Reply ONLY with: FILE:<filename> | <one line description>. The system will generate and send the file."}])
            tag2, payload2 = extract_tag(out2)
            if tag2 == "FILE":
                err = run_file(payload2, chat_id, user_text=text)
                reply = err or ("file ready 📄")
                h.append(("assistant", reply)); h[:] = h[-30:]
                return reply
    h.append(("assistant", out)); h[:] = h[-30:]
    threading.Thread(target=_auto_remember, args=(chat_id, text, out), daemon=True).start()
    return out

def _auto_remember(cid, user_text, bot_reply):
    try:
        out = llm([{"role": "user", "content": "User said: " + user_text[:600] + "\n\nExtract EVERYTHING durable this reveals about the user: name, age/class/study, job/work, money matters, people (who they are to him), habits, schedule, goals, preferences, likes/dislikes, plans, addresses, contact info.\nReply with up to 3 SHORT memory facts, one per line. Be specific (save names, numbers, times).\nIf nothing worth remembering, reply with exactly: NONE"}], timeout=30)
        out = (out or "").strip()
        if out and out.upper() != "NONE":
            for line in out.split("\n"):
                f = line.strip().lstrip("-•*0123456789. ")
                if f and len(f) > 3 and not f.upper().startswith("NONE"):
                    mem_remember(f[:300])
    except Exception:
        pass


# ---------- persistent memory / reminders (GitHub file as database) ----------
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "fuadaixz-bit/vcore")
GH_API = "https://api.github.com/repos/" + GH_REPO + "/contents/memory.json"
GH_RAW = "https://raw.githubusercontent.com/" + GH_REPO + "/main/memory.json"
_MEM_CACHE = {"data": None, "at": 0}
_MEM_LOCK = threading.Lock()

def _default_mem():
    return {"facts": [], "rems": [], "last_auto": 0}

def mem_load():
    with _MEM_LOCK:
        if _MEM_CACHE["data"] is not None and time.time() - _MEM_CACHE.get("at", 0) < 60:
            return _MEM_CACHE["data"]
        try:
            r = requests.get(GH_API, headers={"Authorization": "Bearer " + GH_TOKEN, "Cache-Control": "no-cache"}, timeout=10)
            if r.ok:
                _MEM_CACHE["data"] = json.loads(base64.b64decode(r.json()["content"]).decode() or "{}") or _default_mem()
                _MEM_CACHE["at"] = time.time()
        except Exception: pass
        if not _MEM_CACHE["data"]:
            try:
                r = requests.get(GH_RAW + "?t=" + str(int(time.time())), timeout=10)
                if r.ok and r.text.strip():
                    _MEM_CACHE["data"] = json.loads(r.text); _MEM_CACHE["at"] = time.time()
            except Exception: pass
        if not _MEM_CACHE["data"]:
            _MEM_CACHE["data"] = _default_mem()
        return _MEM_CACHE["data"]

def _merge(base, new):
    """Union-merge lists by identity so a stale instance can't wipe fresh data."""
    out = dict(new)
    for k in ("users", "keys", "facts", "rems", "convs"):
        b, n = base.get(k) or [], new.get(k) or []
        if k == "keys":
            out[k] = list(dict.fromkeys(n + b))
        elif k == "users":
            ids = {u["id"] for u in n}; out[k] = n + [u for u in b if u.get("id") not in ids]
        elif k == "facts":
            seen = {f.get("text") for f in n}; out[k] = n + [f for f in b if f.get("text") not in seen][-300:]
        elif k == "rems":
            out[k] = n + [r for r in b if r not in n]
        elif k == "convs":
            ids = {c["id"] for c in n}; out[k] = n + [c for c in b if c.get("id") not in ids]
    return out

def mem_save(data):
    with _MEM_LOCK:
        _MEM_CACHE["data"] = data; _MEM_CACHE["at"] = time.time()
        # SYNCHRONOUS write: Vercel freezes background threads after the response, async writes never landed
        try:
            r = requests.get(GH_API, headers={"Authorization": "Bearer " + GH_TOKEN, "Cache-Control": "no-cache"}, timeout=10)
            sha, remote = (r.json().get("sha"), None) if r.ok else (None, None)
            if r.ok:
                remote = json.loads(base64.b64decode(r.json()["content"]).decode() or "{}")
            merged = _merge(remote or {}, data) if remote else data
            requests.put(GH_API, headers={"Authorization": "Bearer " + GH_TOKEN},
                json={"message": "vcore memory update", "content": base64.b64encode(json.dumps(merged, ensure_ascii=False).encode()).decode(), "sha": sha}, timeout=15)
        except Exception:
            pass

def mem_remember(fact):
    if not fact or len(fact) < 4: return
    d = mem_load()
    if any(fact.lower() == f["text"].lower() for f in d.get("facts", [])): return
    d.setdefault("facts", []).append({"text": fact[:300], "at": int(time.time())})
    d["facts"] = d["facts"][-300:]
    mem_save(d)

def mem_add_rem(at_ts, text, cid):
    d = mem_load()
    d.setdefault("rems", []).append({"at": at_ts, "text": text, "chat": cid})
    mem_save(d)

def mem_rem_done(r, sha=None):
    """Atomically remove ONE reminder from GitHub (sha-retry). Marking first prevents
    parallel cron/webhook instances from double-firing the same reminder."""
    key = (r["at"], r["text"], r["chat"])
    for _ in range(4):
        d = mem_load()
        d["rems"] = [x for x in d.get("rems", []) if not (x["at"], x["text"], x["chat"]) == key]
        try:
            rr = requests.get(GH_API, headers={"Authorization": "Bearer " + GH_TOKEN, "Cache-Control": "no-cache"}, timeout=10)
            cur_sha = rr.json().get("sha") if rr.ok else None
            remote = json.loads(base64.b64decode(rr.json()["content"]).decode() or "{}") if rr.ok else {}
            if any((x["at"], x["text"], x["chat"]) == key for x in remote.get("rems", [])):
                remote["rems"] = [x for x in remote.get("rems", []) if not (x["at"], x["text"], x["chat"]) == key]
                pr = requests.put(GH_API, headers={"Authorization": "Bearer " + GH_TOKEN},
                    json={"message": "rem fired", "content": base64.b64encode(json.dumps(remote, ensure_ascii=False).encode()).decode(), "sha": cur_sha}, timeout=15)
                if pr.status_code in (200, 204): return True
                continue  # 409 race -> retry with fresh sha
        except Exception:
            return False
        return True
    return False

import datetime as _dtmod
try:
    from zoneinfo import ZoneInfo
    TZV = ZoneInfo("Asia/Dhaka")
except Exception:
    TZV = None

def parse_rem_time(s):
    s = (s or "").strip().lower()
    try:
        if s.endswith("m"): return time.time() + float(s[:-1]) * 60
        if s.endswith("h"): return time.time() + float(s[:-1]) * 3600
        if s.endswith("d"): return time.time() + float(s[:-1]) * 86400
        h, m = map(int, s.split(":"))
        n = _dtmod.datetime.now(TZV)
        t = n.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= n: t += _dtmod.timedelta(days=1)
        return t.timestamp()
    except Exception:
        return None



_BN = {"0":"0","1":"1","2":"2","3":"3","4":"4","5":"5","6":"6","7":"7","8":"8","9":"9","০":"0","১":"1","২":"2","৩":"3","৪":"4","৫":"5","৬":"6","৭":"7","৮":"8","৯":"9"}

def _bn_num(s):
    return "".join(_BN.get(c, c) for c in s)

def parse_banglish_rem(text):
    """Deterministic: parse time straight from the user's words. Returns (epoch, source_desc) or None."""
    t = _bn_num((text or "").lower().strip())
    now = _dtmod.datetime.now(TZV)
    # "4 ta 15 tey" / "4 the 15 te" / "4:15 e" -> clock time H:M (next occurrence)
    m = re.search(r"(\d{1,2})\s*(?:ta|ta|the|\:|:)\s*(\d{1,2})\s*(?:tey|te|tay|e)?\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mi < 60:
            cands = []
            for hh in {h, h + 12} & set(range(24)):
                c = now.replace(hour=hh, minute=mi, second=0, microsecond=0)
                if c <= now: c += _dtmod.timedelta(days=1)
                cands.append(c)
            if h < 12 and any(w in t for w in ("asor", "asr", "magrib", "isya", "isha")):
                cands = [c for c in cands if c.hour >= 12] or cands
            cand = min(cands)
            return cand.timestamp(), cand.strftime("%I:%M %p")
    # "4 ta e" / "4 baje" -> clock hour at :00
    m = re.search(r"\b(\d{1,2})\s*(?:ta|baje|ghorite)\s*(?:e)?\b", t)
    if m and any(w in t for w in ("remind", "reminder", "wake", "mone", "namaz")):
        h = int(m.group(1))
        if 0 <= h < 24:
            cands = []
            for hh in {h, h + 12} & set(range(24)):
                c = now.replace(hour=hh, minute=0, second=0, microsecond=0)
                if c <= now: c += _dtmod.timedelta(days=1)
                cands.append(c)
            if h < 12 and any(w in t for w in ("asor", "asr", "magrib", "isya", "isha")):
                cands = [c for c in cands if c.hour >= 12] or cands
            cand = min(cands)
            return cand.timestamp(), cand.strftime("%I:%M %p")
    # "15 min por" / "5 minute por" / "2 ghonta por" / "in 10m"
    m = re.search(r"(\d{1,3})\s*(min|minute|ra|hh|hour|ghonta|m|h)\w*\s*por\b|\bin\s+(\d{1,3})\s*(m|min|h|hour)\b", t)
    if m:
        n = int(m.group(1) or m.group(3)); unit = (m.group(2) or m.group(4) or "m")[0]
        secs = n * (3600 if unit == "h" else 60)
        return time.time() + secs, "+%d%s" % (n, unit)
    return None

# ---------- conversation history (saved to GitHub memory.json) ----------
def conv_save(cid, user_msg, bot_reply):
    d = mem_load()
    convs = d.setdefault("convs", [])
    key = str(cid)
    c = next((x for x in convs if x["id"] == key), None)
    if not c:
        title = user_msg[:48] + ("..." if len(user_msg) > 48 else "")
        c = {"id": key, "title": title, "msgs": [], "at": int(time.time())}
        convs.insert(0, c)
    c["msgs"].append({"r": "u", "t": user_msg, "at": int(time.time())})
    c["msgs"].append({"r": "b", "t": bot_reply, "at": int(time.time())})
    c["msgs"] = c["msgs"][-200:]
    c["at"] = int(time.time())
    d["convs"] = convs[:40]
    mem_save(d)

def conv_get(cid):
    d = mem_load()
    c = next((x for x in d.get("convs", []) if x["id"] == str(cid)), None)
    return c or {"id": str(cid), "title": "", "msgs": []}


# ---------- usage limits + API key pool ----------
def _today(): return _dtmod.datetime.now(TZV).strftime("%Y-%m-%d")

def usage_check_and_add(uid, chars):
    """Returns (ok, used, limit). Adds usage if under limit."""
    d = mem_load()
    u = next((x for x in d.get("users", []) if x["id"] == uid), None)
    if not u: return (True, 0, -1)
    limit = u.get("limit", 100000)
    day = _today()
    usage = u.get("usage", {})
    used = usage.get(day, 0) + chars // 4
    if limit >= 0 and used > limit:
        u["usage"] = usage; mem_save(d); return (False, used, limit)
    usage[day] = used
    u["usage"] = usage
    mem_save(d)
    return (True, used, limit)

def key_pool():
    """Main keys + all admin-added keys, preserving order and avoiding duplicates."""
    d = mem_load()
    return list(dict.fromkeys(k for k in (XKIRO_KEYS + (d.get("keys") or [])) if k))
_typing_on = set()

def _typing_loop(cid):
    while cid in _typing_on:
        tg_json("sendChatAction", chat_id=cid, action="typing")
        for _ in range(8):
            if cid not in _typing_on: return
            time.sleep(0.5)

# ---------- Telegram owner-only admin panel ----------
TELEGRAM_ADMIN_OWNER = "8602407906"

def _admin_owner(update_message):
    chat = update_message.get("chat") or {}
    sender = update_message.get("from") or {}
    return str(chat.get("id", "")) == TELEGRAM_ADMIN_OWNER and str(sender.get("id", "")) == TELEGRAM_ADMIN_OWNER

def _admin_keyboard(rows=None):
    if rows is None:
        rows = [
            [{"text": "create web user", "callback_data": "adm:create"}, {"text": "list users", "callback_data": "adm:users"}],
            [{"text": "set daily limit", "callback_data": "adm:limits"}, {"text": "add Groq key", "callback_data": "adm:addkey"}],
            [{"text": "list key pool", "callback_data": "adm:keys"}, {"text": "view stats", "callback_data": "adm:stats"}],
        ]
    return {"inline_keyboard": rows}

def _admin_edit(chat_id, message_id, text, rows=None):
    return tg_json("editMessageText", chat_id=chat_id, message_id=message_id,
                   text=text, reply_markup=_admin_keyboard(rows))

def _admin_pending_set(chat_id, value):
    # Pending prompts live in memory.json so the next webhook instance can resume them.
    with _MEM_LOCK:
        _MEM_CACHE["data"] = None
        _MEM_CACHE["at"] = 0
    d = mem_load()
    pending = d.setdefault("admin_pending", {})
    if value is None:
        pending.pop(str(chat_id), None)
    else:
        pending[str(chat_id)] = value
    mem_save(d)

def _admin_pending_get(chat_id):
    with _MEM_LOCK:
        _MEM_CACHE["data"] = None
        _MEM_CACHE["at"] = 0
    return (mem_load().get("admin_pending") or {}).get(str(chat_id))

def _admin_home(chat_id, message_id, notice=""):
    text = "V-CORE admin panel"
    if notice: text += "\n\n" + notice
    _admin_edit(chat_id, message_id, text)

def _admin_users(chat_id, message_id):
    d = mem_load(); today = _today(); users = d.get("users", [])
    if not users: text = "users\n\nno web users yet"
    else:
        lines = ["web users"]
        for u in users[:35]:
            used = (u.get("usage") or {}).get(today, 0)
            lim = u.get("limit", 100000)
            lines.append("• " + str(u.get("id", "?")) + " | " + str(used) + "/" + ("unlimited" if lim < 0 else str(lim)))
        if len(users) > 35: lines.append("...and " + str(len(users) - 35) + " more")
        text = "\n".join(lines)
    _admin_edit(chat_id, message_id, text, [[{"text": "back", "callback_data": "adm:home"}]])

def _admin_limit_choices(chat_id, message_id):
    users = mem_load().get("users", [])
    rows = [[{"text": str(u.get("id", "?"))[:32], "callback_data": "adm:limit:" + str(u.get("id", ""))}] for u in users[:35]]
    rows.append([{"text": "back", "callback_data": "adm:home"}])
    _admin_edit(chat_id, message_id, "choose a user to set their daily character limit", rows)

def _admin_keys(chat_id, message_id):
    d = mem_load(); keys = d.get("keys") or []
    lines = ["Groq key pool", "active keys including environment key: " + str(len(key_pool()))]
    if keys:
        lines.extend(str(i + 1) + ". " + k[:8] + "..." + k[-4:] for i, k in enumerate(keys))
    else:
        lines.append("no extra keys added")
    _admin_edit(chat_id, message_id, "\n".join(lines), [[{"text": "back", "callback_data": "adm:home"}]])

def _admin_stats(chat_id, message_id):
    d = mem_load(); now = time.time()
    users = len(d.get("users") or [])
    keys = len(key_pool())
    rems = sum(1 for r in (d.get("rems") or []) if r.get("at", 0) > now)
    _admin_edit(chat_id, message_id, "stats\n\nusers: " + str(users) + "\nkeys: " + str(keys) + "\nactive reminders: " + str(rems),
                [[{"text": "back", "callback_data": "adm:home"}]])

def _admin_create_user(chat_id, message_id):
    d = mem_load(); users = d.setdefault("users", [])
    uid = "u" + secrets.token_hex(4)
    while any(u.get("id") == uid for u in users): uid = "u" + secrets.token_hex(4)
    pw = secrets.token_urlsafe(9)
    users.append({"id": uid, "pw": hashlib.sha256(pw.encode()).hexdigest(), "limit": 100000, "usage": {}})
    mem_save(d)
    rows = [[{"text": "back to panel", "callback_data": "adm:home"}]]
    _admin_edit(chat_id, message_id, "web user created\n\nid: " + uid + "\npassword: " + pw + "\ndaily limit: 100000 chars\n\ncopy the password now, it is shown only once", rows)

def _admin_callback(query):
    msg = query.get("message") or {}; chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id"); data = str(query.get("data") or "")
    sender = query.get("from") or {}
    if str(chat_id) != TELEGRAM_ADMIN_OWNER or str(sender.get("id", "")) != TELEGRAM_ADMIN_OWNER:
        tg_json("answerCallbackQuery", callback_query_id=query.get("id"))
        return
    tg_json("answerCallbackQuery", callback_query_id=query.get("id"))
    if not message_id: return
    if data == "adm:home": _admin_pending_set(chat_id, None); _admin_home(chat_id, message_id)
    elif data == "adm:create": _admin_pending_set(chat_id, None); _admin_create_user(chat_id, message_id)
    elif data == "adm:users": _admin_pending_set(chat_id, None); _admin_users(chat_id, message_id)
    elif data == "adm:limits": _admin_pending_set(chat_id, None); _admin_limit_choices(chat_id, message_id)
    elif data.startswith("adm:limit:"):
        uid = data[len("adm:limit:"):]
        exists = any(u.get("id") == uid for u in mem_load().get("users", []))
        if not exists: _admin_home(chat_id, message_id, "user not found")
        else:
            _admin_pending_set(chat_id, {"action": "setlimit", "uid": uid, "message_id": message_id})
            _admin_edit(chat_id, message_id, "send the daily character limit for " + uid + " (use -1 for unlimited)",
                        [[{"text": "cancel", "callback_data": "adm:home"}]])
    elif data == "adm:addkey":
        _admin_pending_set(chat_id, {"action": "addkey", "message_id": message_id})
        _admin_edit(chat_id, message_id, "send the Groq API key in your next message. it will be stored in the key pool",
                    [[{"text": "cancel", "callback_data": "adm:home"}]])
    elif data == "adm:keys": _admin_pending_set(chat_id, None); _admin_keys(chat_id, message_id)
    elif data == "adm:stats": _admin_pending_set(chat_id, None); _admin_stats(chat_id, message_id)

def _admin_prompt_reply(message, text):
    if not _admin_owner(message): return False
    chat_id = (message.get("chat") or {}).get("id")
    pending = _admin_pending_get(chat_id)
    if not pending or pending.get("action") == "panel": return False
    if text.strip().lower() in ("/cancel", "cancel"):
        _admin_pending_set(chat_id, None)
        _admin_edit(chat_id, pending.get("message_id"), "V-CORE admin panel")
        return True
    d = mem_load(); action = pending.get("action"); notice = ""
    if action == "setlimit":
        try: limit = int(text.strip())
        except ValueError:
            _admin_edit(chat_id, pending.get("message_id"), "limit ta whole number hote hobe. abar pathao, -1 = unlimited",
                        [[{"text": "cancel", "callback_data": "adm:home"}]])
            return True
        user = next((u for u in d.get("users", []) if u.get("id") == pending.get("uid")), None)
        if user:
            user["limit"] = limit; mem_save(d); notice = "daily limit updated for " + str(user.get("id")) + ": " + str(limit)
        else: notice = "user not found"
    elif action == "addkey":
        key = text.strip()
        if not key or len(key) > 500:
            _admin_edit(chat_id, pending.get("message_id"), "key ta valid text hisebe pelam na. abar pathao",
                        [[{"text": "cancel", "callback_data": "adm:home"}]])
            return True
        keys = d.setdefault("keys", [])
        if key not in keys: keys.append(key); mem_save(d); notice = "Groq key pool e add hoyeche"
        else: notice = "ei key agei pool e ache"
    _admin_pending_set(chat_id, None)
    _admin_home(chat_id, pending.get("message_id"), notice)
    return True

def transcribe_voice(file_id):
    """Voice note -> text via Groq whisper (bn/en both)."""
    try:
        r = requests.get("https://api.telegram.org/bot" + TG_TOKEN + "/getFile", params={"file_id": file_id}, timeout=10)
        fp = r.json()["result"]["file_path"]
        f = requests.get("https://api.telegram.org/file/bot" + TG_TOKEN + "/" + fp, timeout=20)
        import io as _io
        rr = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": "Bearer " + (os.environ.get("GROQ_KEY") or "")},
            files={"file": ("voice.ogg", _io.BytesIO(f.content), "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "bn", "response_format": "text",
                  "prompt": "বাংলা কথ্য ভাষা, ঢাকার চ্যাট টোন। English words may be mixed in."}, timeout=25)
        if rr.ok: return rr.text.strip()
    except Exception:
        pass
    return ""

@app.route("/api/webhook", methods=["POST"])
def webhook():
    try:
        sweep_due_rems()
    except Exception:
        pass
    try: u = request.get_json(force=True)
    except Exception: return jsonify(ok=True)
    cq = (u or {}).get("callback_query") or {}
    if cq:
        _admin_callback(cq)
        return jsonify(ok=True)
    m = (u or {}).get("message") or {}
    t = (m.get("text") or "").strip()
    v = m.get("voice") or m.get("audio")
    cid = m.get("chat", {}).get("id")
    if not t and v and cid is not None:
        t = transcribe_voice(v.get("file_id", ""))
        if not t:
            _typing_on.discard(cid)
            tg_json("sendMessage", chat_id=cid, text="voice ta thik moto dhora ponorsy na, ektu abar bolo?")
            return jsonify(ok=True)
        t = "[voice] " + t
    if t and cid is not None:
        if _admin_owner(m):
            cmd = t.split()[0].split("@")[0].lower()
            if cmd == "/admin":
                # Reuse the existing panel message when possible so navigation edits in place.
                pending = _admin_pending_get(cid)
                if pending and pending.get("message_id"):
                    _admin_pending_set(cid, None)
                    _admin_home(cid, pending.get("message_id"))
                else:
                    r = tg_json("sendMessage", chat_id=cid, text="V-CORE admin panel", reply_markup=_admin_keyboard())
                    if r is not None and r.ok:
                        try:
                            mid = r.json().get("result", {}).get("message_id")
                            if mid: _admin_pending_set(cid, {"action": "panel", "message_id": mid})
                        except Exception: pass
                _typing_on.discard(cid)
                return jsonify(ok=True)
            if _admin_prompt_reply(m, t):
                _typing_on.discard(cid)
                return jsonify(ok=True)
        elif t.split()[0].split("@")[0].lower() == "/admin":
            # Silently ignore all admin access from anyone other than the owner.
            return jsonify(ok=True)
        _typing_on.add(cid)
        threading.Thread(target=_typing_loop, args=(cid,), daemon=True).start()
        out = ask(cid, t)
        _typing_on.discard(cid)
        if out: tg_json("sendMessage", chat_id=cid, text=out)
    return jsonify(ok=True)


# ---------- cron: fires due reminders + occasional own-initiative messages ----------
CRON_KEY = os.environ.get("CRON_KEY", "")
OWNER_ID = os.environ.get("OWNER_ID", "")
AUTO_EVERY_H = 4  # one own-initiative message per 4h window


# ---------- web UI: login + telegram-style chat (same brain, same memory) ----------
import secrets, hashlib
from flask import render_template_string, make_response, Response

WEB_USERS_FILE = "web_users.json"

def web_users_load():
    d = mem_load()
    if d.get("users"): return {"users": d["users"]}
    try:
        r = requests.get("https://raw.githubusercontent.com/" + GH_REPO + "/main/web_users.json?t=" + str(int(time.time())), timeout=10)
        if r.ok and r.text.strip():
            j = json.loads(r.text)
            # migrate plaintext users to hashed in memory.json
            d.setdefault("users", [])
            for u in j.get("users", []):
                if not any(x["id"] == u["id"] for x in d["users"]):
                    d["users"].append({"id": u["id"], "pw": hashlib.sha256(u["pw"].encode()).hexdigest(), "limit": 100000, "usage": {}})
            mem_save(d)
            return {"users": d["users"]}
    except Exception: pass
    return {"users": []}

# stateless signed sessions (serverless-safe: memory across instances)
SIGN_KEY = os.environ.get("CRON_KEY", "vcore-sess")
def _sign(s): return hashlib.sha256((SIGN_KEY + s).encode()).hexdigest()[:32]
def make_session(uid):
    t = secrets.token_hex(24); return uid + "|" + t + "." + _sign(uid + "|" + t)
def check_session(tok):
    if not tok or "." not in tok or "|" not in tok: return False
    body, sig = tok.rsplit(".", 1)
    return _sign(body) == sig
def session_uid(tok):
    return tok.split("|", 1)[0] if "|" in tok else ""

LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V-CORE Login</title><style>
*{box-sizing:border-box;margin:0;padding:0}body{font-family:system-ui;background:#0b0e11;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;padding:36px 28px;border-radius:16px;width:90%%;max-width:360px;border:1px solid #21262d}
h1{color:#e6edf3;font-size:22px;text-align:center;margin-bottom:6px}p{color:#8b949e;font-size:13px;text-align:center;margin-bottom:24px}
input{width:100%%;padding:12px 14px;margin-bottom:12px;background:#0d1117;border:1px solid #30363d;border-radius:10px;color:#e6edf3;font-size:15px}
input:focus{outline:none;border-color:#58a6ff}
button{width:100%%;padding:12px;background:#238636;border:none;border-radius:10px;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:active{transform:scale(.98)}.err{color:#f85149;font-size:13px;text-align:center;margin-bottom:12px;display:none}
</style></head><body><div class="card"><h1>V-CORE</h1><p>login to continue</p><div class="err" id="e">wrong id or password</div>
<form method="POST" action="/login"><input name="id" placeholder="User ID" required><input name="pw" type="password" placeholder="Password" required><button>Log in</button></form></div></body></html>"""

CHAT_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>V-CORE</title><style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{font-family:-apple-system,system-ui,'Segoe UI',Roboto,sans-serif;background:#0a0a0c;color:#ececf1;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;letter-spacing:.3px}
.logo .mark{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#6a5cff,#a45cff);display:flex;align-items:center;justify-content:center;font-size:15px}
#newchat{background:#1a1a1f;border:1px solid #26262c;color:#b9b9c2;border:none;border-radius:20px;padding:8px 16px;font-size:13px;cursor:pointer}
#wrap{flex:1;overflow-y:auto;display:flex;flex-direction:column;scroll-behavior:smooth}
#hero{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;padding:20px;transition:opacity .4s}
#hero .orb{width:84px;height:84px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#8b7bff,#4a3aff 60%,#1c1440);box-shadow:0 0 60px rgba(106,92,255,.35);animation:pulse 3s ease-in-out infinite}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}
#hero h1{font-size:26px;font-weight:700;background:linear-gradient(90deg,#fff,#a79fff);-webkit-background-clip:text;background-clip:text;color:transparent}
#hero p{color:#6e6e78;font-size:14px}
#msgs{display:none;flex-direction:column;gap:14px;padding:18px 16px 8px;max-width:760px;width:100%;margin:0 auto}
.m{max-width:85%;padding:12px 16px;border-radius:20px;font-size:15px;line-height:1.55;white-space:pre-wrap;word-wrap:break-word;animation:fadeUp .25s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.u{align-self:flex-end;background:linear-gradient(135deg,#4a3aff,#6a5cff);border-bottom-right-radius:6px;color:#fff}
.b{align-self:flex-start;background:#1a1a1f;border:1px solid #26262c;border-bottom-left-radius:6px}
#chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;padding:0 16px 14px;max-width:760px;margin:0 auto;width:100%}
.chip{background:#1a1a1f;border:1px solid #2c2c33;color:#c8c8d2;border-radius:22px;padding:10px 16px;font-size:13.5px;cursor:pointer;transition:background .15s}
.chip:active{background:#26262d}
#dock{padding:10px 14px calc(14px + env(safe-area-inset-bottom));background:linear-gradient(to top,#0a0a0c 70%,transparent)}
#composer{max-width:760px;margin:0 auto;background:#141418;border:1px solid #2a2a31;border-radius:26px;padding:6px 6px 6px 18px;display:flex;align-items:center;gap:10px;transition:border-color .2s}
#composer:focus-within{border-color:#4a3aff}
#in{flex:1;background:transparent;border:none;outline:none;color:#ececf1;font-size:15.5px;min-height:24px;max-height:120px;resize:none;font-family:inherit;line-height:1.4;padding:10px 0}
#in::placeholder{color:#5c5c66}
#send{width:42px;height:42px;border-radius:50%;border:none;background:linear-gradient(135deg,#4a3aff,#6a5cff);color:#fff;font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .2s,transform .1s}
#send:disabled{opacity:.4}#send:active{transform:scale(.92)}
.ty{color:#7a7a85;font-size:13px;height:20px;padding:2px 22px 6px;max-width:760px;margin:0 auto}
.typing-dots span{display:inline-block;width:5px;height:5px;background:#7a7a85;border-radius:50%;margin-right:3px;animation:blink 1.2s infinite}
.typing-dots span:nth-child(2){animation-delay:.2s}.typing-dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}

#menu{background:none;border:none;color:#ececf1;font-size:22px;cursor:pointer;padding:4px 8px}
#scrim{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40}
#sidebar{position:fixed;top:0;left:-300px;width:290px;height:100%;background:#101014;border-right:1px solid #232329;z-index:50;transition:left .25s ease;display:flex;flex-direction:column}
#sidebar.open{left:0}
#sidebar .head{padding:20px 18px 12px;display:flex;align-items:center;justify-content:space-between}
#sidebar .head b{font-size:16px}
#sidebar .head button{background:none;border:none;color:#8b8b95;font-size:18px;cursor:pointer}
#convlist{flex:1;overflow-y:auto;padding:6px 10px}
.citem{padding:12px 12px;border-radius:12px;cursor:pointer;margin-bottom:2px}
.citem:active{background:#1c1c22}
.citem .t{font-size:14px;color:#e6e6ee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.citem .s{font-size:11.5px;color:#6e6e78;margin-top:3px}
#convlist .empty{color:#5c5c66;font-size:13px;padding:16px 12px}
</style></head><body>
<div id="scrim" onclick="closeSide()"></div>
<div id="sidebar"><div class="head"><b>Conversations</b><button onclick="closeSide()">✕</button></div><div id="convlist"><div class="empty">kotha suru korle ekhane joma hobe</div></div></div>
<header><button id="menu" onclick="openSide()">☰</button><div class="logo"><div class="mark">V</div>V-CORE</div><button id="newchat" onclick="location.reload()">+ New</button></header>
<div id="wrap">
  <div id="hero"><div class="orb"></div><h1>Assalamualaikum, Fuad</h1><p>ki jigges korba ajke?</p></div>
  <div id="msgs"></div>
</div>
<div id="chips">
  <div class="chip" onclick="quick('Ajke btc er price koto?')">BTC price</div>
  <div class="chip" onclick="quick('Ekta hadith share koro ajker din er jono')">Islamic</div>
  <div class="chip" onclick="quick('Study motivation niya ekta pdf banao')">PDF banao</div>
  <div class="chip" onclick="quick('Solo leveling er best arc niya take dao')">Solo Leveling</div>
</div>
<div id="dock"><div id="composer"><textarea id="in" rows="1" placeholder="Ask anything"></textarea><button id="send">&#8593;</button></div><div class="ty" id="ty"></div></div>
<script>
const msgs=document.getElementById('msgs'),hero=document.getElementById('hero'),chips=document.getElementById('chips'),
in_=document.getElementById('in'),ty=document.getElementById('ty'),send=document.getElementById('send'),wrap=document.getElementById('wrap');
let busy=false;
in_.addEventListener('input',()=>{in_.style.height='auto';in_.style.height=Math.min(in_.scrollHeight,120)+'px'});
function add(t,c){if(hero.style.display!=='none'){hero.style.display='none';chips.style.display='none';msgs.style.display='flex'}
const d=document.createElement('div');d.className='m '+c;d.textContent=t;msgs.appendChild(d);wrap.scrollTop=wrap.scrollHeight;return d}
async function go(){if(busy)return;const v=in_.value.trim();if(!v)return;add(v,'u');in_.value='';in_.style.height='auto';busy=true;send.disabled=true;
ty.innerHTML='<div class="typing-dots"><span></span><span></span><span></span></div>';
try{const r=await fetch('/api/webchat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:v})});
if(r.status===401){location='/login';return}
const j=await r.json();ty.textContent='';add(j.reply,'b')}catch(e){ty.textContent='';add('net error, abar pathao','b')}
busy=false;send.disabled=false;in_.focus()}
function quick(t){in_.value=t;go()}
send.onclick=go;
function openSide(){document.getElementById('sidebar').classList.add('open');document.getElementById('scrim').style.display='block';loadConvs()}
function closeSide(){document.getElementById('sidebar').classList.remove('open');document.getElementById('scrim').style.display='none'}
async function loadConvs(){try{const r=await fetch('/api/convs');if(r.status===401)return;const j=await r.json();
const el=document.getElementById('convlist');el.innerHTML='';
if(!j.convs.length){el.innerHTML='<div class="empty">kotha suru korle ekhane joma hobe</div>';return}
j.convs.forEach(c=>{const d=document.createElement('div');d.className='citem';
const dt=new Date(c.at*1000);const when=dt.toLocaleDateString('en-GB',{day:'2-digit',month:'short'})+' '+dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
d.innerHTML='<div class="t"></div><div class="s"></div>';
d.querySelector('.t').textContent=c.title;d.querySelector('.s').textContent=when+' · '+c.n+' msgs';
d.onclick=()=>openConv(c.id);el.appendChild(d)})}catch(e){}}
async function openConv(id){try{const r=await fetch('/api/conv?id='+encodeURIComponent(id));if(r.status===401)return;
const j=await r.json();closeSide();
if(hero.style.display!=='none'){hero.style.display='none';chips.style.display='none';msgs.style.display='flex'}
msgs.innerHTML='';
j.msgs.forEach(m=>add(m.t, m.r==='u'?'u':'b'));
// continue that conversation: point composer at same cid
curCid=id;
if(!j.msgs.length){hero.style.display='flex';chips.style.display='flex';msgs.style.display='none'}
}catch(e){}}
let curCid=null;
const _go=go;
async function go(){if(busy)return;const v=in_.value.trim();if(!v)return;add(v,'u');in_.value='';in_.style.height='auto';busy=true;send.disabled=true;
ty.innerHTML='<div class="typing-dots"><span></span><span></span><span></span></div>';
try{const url=curCid?('/api/webchat?cid='+encodeURIComponent(curCid)):'/api/webchat';
const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:v})});
if(r.status===401){location='/login';return}
const j=await r.json();ty.textContent='';add(j.reply,'b');
if(!curCid&&j.cid){curCid=j.cid}}catch(e){ty.textContent='';add('net error, abar pathao','b')}
busy=false;send.disabled=false;in_.focus()}
in_.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();go()}});
</script></body></html>"""

@app.route("/", methods=["GET"])
@app.route("/login", methods=["GET"])
def login_page():
    tok = request.cookies.get("s", "")
    if check_session(tok):
        return CHAT_PAGE
    return LOGIN_PAGE

@app.route("/login", methods=["POST"])
def login_post():
    uid = request.form.get("id", "").strip()
    pw = request.form.get("pw", "")
    u = web_users_load()
    def _pw_ok(stored):
        return stored == hashlib.sha256(pw.encode()).hexdigest() or stored == pw
    ok = any(x["id"] == uid and _pw_ok(x.get("pw", "")) for x in u.get("users", []))
    if not ok:
        return LOGIN_PAGE
    tok = make_session(uid)
    resp = make_response(CHAT_PAGE)
    resp.set_cookie("s", tok, max_age=86400 * 30, httponly=True)
    return resp

@app.route("/api/webchat", methods=["POST"])
def webchat():
    tok = request.cookies.get("s", "")
    if not check_session(tok):
        return "unauthorized", 401
    body = request.get_json(force=True, silent=True) or {}
    msg = (body.get("msg") or "").strip()
    if not msg: return jsonify(reply="")
    uid = session_uid(tok)
    ok, used, limit = usage_check_and_add(uid, len(msg))
    if not ok:
        return jsonify(reply="tomar ajker usage limit sesh hoye geche (" + str(limit) + " chars). kal abar ashbe ba admin limit barate parbe.")
    cid = request.args.get("cid") or ("web-" + uid)
    out = ask(cid, msg)
    conv_save(cid, msg, out)
    return jsonify(reply=out, cid=cid)


@app.route("/api/convs", methods=["GET"])
def api_convs():
    tok = request.cookies.get("s", "")
    if not check_session(tok): return "unauthorized", 401
    d = mem_load()
    out = [{"id": c["id"], "title": c["title"], "at": c["at"], "n": len(c["msgs"])} for c in d.get("convs", [])]
    return jsonify(convs=out)

@app.route("/api/conv", methods=["GET"])
def api_conv():
    tok = request.cookies.get("s", "")
    if not check_session(tok): return "unauthorized", 401
    cid = request.args.get("id", "")
    return jsonify(conv_get(cid))


# ---------- admin panel: users, limits, api keys ----------
ADMIN_PW = os.environ.get("ADMIN_PW", "")
ADMINS = set()

ADMIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V-CORE Admin</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:#0a0a0c;color:#ececf1;padding:20px}
h1{font-size:20px;margin-bottom:4px}.sub{color:#6e6e78;font-size:13px;margin-bottom:20px}
.card{background:#141418;border:1px solid #26262c;border-radius:14px;padding:18px;margin-bottom:16px;max-width:820px}
.card h2{font-size:15px;margin-bottom:12px;color:#a79fff}
input,select{background:#0d0d11;border:1px solid #2c2c33;color:#ececf1;border-radius:9px;padding:9px 12px;font-size:14px;outline:none}
input:focus{border-color:#4a3aff}
button{background:linear-gradient(135deg,#4a3aff,#6a5cff);border:none;color:#fff;border-radius:9px;padding:9px 16px;font-size:14px;cursor:pointer;font-weight:600}
button.gray{background:#26262c}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{color:#6e6e78;text-align:left;padding:8px 6px;font-weight:600;border-bottom:1px solid #26262c}
td{padding:9px 6px;border-bottom:1px solid #1c1c22}
.mono{font-family:monospace;font-size:12.5px}
.tag{background:#1c1430;color:#a79fff;border-radius:6px;padding:2px 8px;font-size:11.5px;margin-left:6px}
.ok{color:#3fb950}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.small{font-size:12px;color:#6e6e78;margin-top:8px}
.del{color:#f85149;cursor:pointer;font-size:12.5px;background:none;border:none;padding:0}
</style></head><body>
<h1>V-CORE Admin</h1><div class="sub">users · limits · API keys</div>
<div class="card"><h2>+ Notun user</h2>
<div class="row"><input id="nid" placeholder="User ID (e.g. rahim)"><input id="npw" placeholder="Password"><input id="nlim" type="number" placeholder="Daily limit (chars)" style="width:180px"><button onclick="addUser()">Create</button></div>
<div class="small">limit -1 = unlimited. password ta copy kore rakho, ei page e ar dekha jabe na (hash save hoy)</div></div>
<div class="card"><h2>Users</h2><table id="utab"><tr><th>ID</th><th>Ajker usage</th><th>Daily limit</th><th></th></tr></table></div>
<div class="card"><h2>API Keys (Groq pool)</h2>
<div class="row"><input id="nkey" placeholder="gsk_..." style="flex:1;min-width:220px"><button onclick="addKey()">Add key</button></div>
<table id="ktab"><tr><th>Key</th><th></th></tr></table>
<div class="small">ekhane joto Groq key addbe, user beshi holei auto round-robin korbe — ekta key r limit sesh hole porerta nibe</div></div>
<script>
async function load(){let r;try{r=await fetch('/admin/api/data?t='+Date.now(),{cache:'no-store'})}catch(e){alert('net error: '+e);return}
if(r.status===401){alert('session sesh - abar login koro');location='/panel';return}
const j=await r.json();const ut=document.getElementById('utab');
ut.querySelectorAll('.urow').forEach(e=>e.remove());
j.users.forEach(u=>{const tr=document.createElement('tr');tr.className='urow';
const today=new Date().toLocaleDateString('en-CA');
tr.innerHTML='<td class="mono"></td><td></td><td><input style="width:110px" type="number"><button class="gray" style="margin-left:6px">save</button></td><td><button class="del">delete</button></td>';
tr.children[0].textContent=u.id;
tr.children[1].innerHTML=(u.used||0).toLocaleString()+(u.limit<0?' <span class="tag">unlimited</span>':'');
tr.children[2].querySelector('input').value=u.limit;
tr.children[2].querySelector('button').onclick=()=>setLimit(u.id,tr.children[2].querySelector('input').value);
tr.querySelector('.del').onclick=()=>delUser(u.id);ut.appendChild(tr)});
const kt=document.getElementById('ktab');kt.querySelectorAll('.krow').forEach(e=>e.remove());
(j.keys||[]).forEach((k,i)=>{const tr=document.createElement('tr');tr.className='krow';
tr.innerHTML='<td class="mono"></td><td><button class="del">delete</button></td>';
tr.children[0].textContent=k.slice(0,10)+'...'+k.slice(-4);tr.querySelector('.del').onclick=()=>delKey(i);kt.appendChild(tr)});
if(!(j.keys||[]).length){const tr=document.createElement('tr');tr.className='krow';tr.innerHTML='<td colspan="2" style="color:#6e6e78">shudhu main key choltese</td>';kt.appendChild(tr)}}
function adminPw(){return document.cookie.split('admin=').pop().split(';')[0]}
async function post(body){let r;try{r=await fetch('/admin/api/action?t='+Date.now(),{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify(body)})}catch(e){alert('net error: '+e);return null}
if(r.status===401){alert('session sesh - abar login koro');location='/panel';return null}
const j=await r.json().catch(()=>({ok:false,err:'bad response'}));
if(!j.ok&&j.err)alert('fail: '+j.err);return j}
async function addUser(){const id=document.getElementById('nid').value.trim(),pw=document.getElementById('npw').value,lim=parseInt(document.getElementById('nlim').value||'100000');
if(!id||!pw)return alert('id ar password dao');
const j=await post({action:'adduser',id,pw,limit:lim});if(j&&j.ok){document.getElementById('nid').value='';document.getElementById('npw').value='';
alert('user created:\nID: '+id+'\nPassword: '+pw);load()}}
async function delUser(id){if(!confirm(id+' ke delete?'))return;await post({action:'deluser',id});load()}
async function setLimit(id,limit){await post({action:'setlimit',id,limit:parseInt(limit)});load()}
async function addKey(){const k=document.getElementById('nkey').value.trim();if(!k)return;await post({action:'addkey',key:k});document.getElementById('nkey').value='';load()}
async function delKey(i){await post({action:'delkey',i});load()}
load();
</script></body></html>"""

ADMIN_LOGIN = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin Login</title><style>
body{font-family:system-ui;background:#0a0a0c;color:#ececf1;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.c{background:#141418;border:1px solid #26262c;border-radius:14px;padding:30px;width:90%;max-width:340px}
h1{font-size:18px;margin-bottom:16px}input{width:100%;padding:11px;background:#0d0d11;border:1px solid #2c2c33;color:#ececf1;border-radius:9px;font-size:14px;outline:none}
button{width:100%;margin-top:12px;background:linear-gradient(135deg,#4a3aff,#6a5cff);border:none;color:#fff;border-radius:9px;padding:11px;font-size:14px;font-weight:600;cursor:pointer}
</style></head><body><div class="c"><h1>Admin login</h1><form method="POST" action="/panel"><input name="pw" type="password" placeholder="Admin password" required><button>Enter</button></form></div></body></html>"""

def _admin_ok():
    if not ADMIN_PW: return False
    if request.headers.get("x-admin") == ADMIN_PW: return True
    if request.form.get("pw") == ADMIN_PW: return True
    return request.cookies.get("admin", "") == _sign("admin:" + ADMIN_PW)

def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/panel", methods=["GET"])
@app.route("/admin", methods=["GET"])
def admin_page():
    if not ADMIN_PW: return "ADMIN_PW set kora nai server e", 500
    if request.cookies.get("admin", "") == _sign("admin:" + ADMIN_PW): return _no_store(make_response(ADMIN_PAGE))
    return _no_store(make_response(ADMIN_LOGIN))

@app.route("/panel", methods=["POST"])
@app.route("/admin", methods=["POST"])
def admin_login():
    if request.form.get("pw") != ADMIN_PW: return ADMIN_LOGIN
    resp = make_response(ADMIN_PAGE)
    resp.set_cookie("admin", _sign("admin:" + ADMIN_PW), max_age=86400, httponly=True)
    return resp

@app.route("/admin/api/data", methods=["GET"])
def admin_data():
    if not _admin_ok(): return "unauthorized", 401
    d = mem_load(); today = _today()
    users = [{"id": u["id"], "limit": u.get("limit", 100000), "used": (u.get("usage") or {}).get(today, 0)} for u in d.get("users", [])]
    return jsonify(users=users, keys=d.get("keys", []))

@app.route("/admin/api/action", methods=["POST"])
def admin_action():
    if not _admin_ok(): return "unauthorized", 401
    b = request.get_json(force=True); a = b.get("action")
    with _MEM_LOCK:
        _MEM_CACHE["data"] = None; _MEM_CACHE["at"] = 0
    d = mem_load()
    if a == "adduser":
        if any(u["id"] == b["id"] for u in d.get("users", [])): return jsonify(ok=False, err="already exists")
        d.setdefault("users", []).append({"id": b["id"][:40], "pw": hashlib.sha256(b["pw"].encode()).hexdigest(), "limit": int(b.get("limit", 100000)), "usage": {}})
    elif a == "deluser":
        d["users"] = [u for u in d.get("users", []) if u["id"] != b["id"]]
    elif a == "setlimit":
        for u in d.get("users", []):
            if u["id"] == b["id"]: u["limit"] = int(b["limit"])
    elif a == "addkey":
        k = b["key"].strip()
        if k and k not in (d.get("keys") or []): d.setdefault("keys", []).append(k)
    elif a == "delkey":
        d["keys"] = [k for i, k in enumerate(d.get("keys", [])) if i != int(b["i"])]
    else:
        return jsonify(ok=False)
    mem_save(d)
    return jsonify(ok=True)

@app.route("/api/cron", methods=["GET"])
def sweep_due_rems():
    """Fires all due reminders immediately to TG/web. Runs on cron AND on every inbound webhook.
    Claims each reminder atomically on GitHub BEFORE sending so parallel instances never double-fire."""
    d = mem_load()
    now = time.time()
    sent = []
    for r in list(d.get("rems", [])):
        if r.get("at", 0) <= now:
            if not mem_rem_done(r):
                continue  # someone else claimed/removed it -> don't send
            c = r.get("chat")
            if str(c).lstrip("-").isdigit():
                try: tg_json("sendMessage", chat_id=c, text="\u23f0 " + r["text"])
                except Exception: pass
            sent.append(r["text"])
    return sent

def cron_tick():
    if CRON_KEY and request.args.get("key") != CRON_KEY and request.args.get("k") != "cron":
        return "forbidden", 403
    d = mem_load()
    now = time.time()
    sent = []
    sent = sweep_due_rems()
    # own-initiative message: every AUTO_EVERY_H hours, one natural check-in
    _auto_err = ""
    if now - (d.get("last_auto") or 0) > AUTO_EVERY_H * 3600:
        try:
            facts = "\n".join("- " + f["text"] for f in (d.get("facts") or [])[-25:])
            Dhaka = _dtmod.datetime.now(TZV)
            hour = Dhaka.hour
            if 8 <= hour < 23:  # quiet hours respect: no auto msg at night
                msg = llm([{"role": "user", "content": "Write ONE short natural check-in message (1-2 lines, casual Banglish, like a friend texting) to send Fuad right now. Time: " + Dhaka.strftime("%I:%M %p %A") + ".\nThings you remember about him:\n" + (facts or "(nothing yet)") + "\nNo questions about reminders. No emoji spam. Just a warm human check-in."}], timeout=40)
                if msg and len(msg) > 5 and OWNER_ID:
                    tg_json("sendMessage", chat_id=int(OWNER_ID), text=msg.strip())
                    d["last_auto"] = now
                    mem_save(d)
                    sent.append("auto-msg")
        except Exception as e:
            _auto_err = "auto-err: " + str(e)[:150]
    return "ok: " + (", ".join(sent) or "nothing due") + ((" | " + _auto_err) if _auto_err else ""), 200

@app.route("/api", methods=["GET"])
def home():
    return "v-core raw online", 200
