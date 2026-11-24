# -*- coding: utf-8 -*-
"""
دفتر صوتي — سيرفر محلي.
- يقرأ ملفات PDF والصور ويجهّز نصها لقراءة محاسبية مسموعة (عن طريق Claude إن كان مسجّلاً، وإلا قراءة محلية).
- يقرأ النص بصوت «حامد» العربي (أصوات Microsoft Edge المجانية، تحتاج إنترنت): سريع جداً ونطق عربي أصلي.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import glob
import subprocess
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "hamed"
JOBS = DATA / "jobs"
EXPORTS = DATA / "exports"
for d in (CACHE, JOBS, EXPORTS):
    d.mkdir(parents=True, exist_ok=True)

MAX_PAGES = 40
VOICE = "ar-SA-HamedNeural"
PARALLEL = 6  # sentences requested from the voice service at the same time

# On the online server these come from its secret settings; on the Mac they are empty and nothing changes.
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()  # fast free speech-to-text (optional)
GEMINI_MODELS = [m for m in (os.environ.get("GEMINI_MODEL", ""), "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash") if m]
_MODEL_CACHE = {"at": 0, "list": []}
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
LAST_AI_ERROR = [""]

app = FastAPI()


# ---------------------------------------------------------------- jobs
JOBS_STATE = {}


def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    JOBS_STATE[jid] = {"id": jid, "kind": kind, "status": "running", "progress": 0.0,
                       "stage": "", "live": "", "result": None, "error": None, "cancel": False}
    return jid, JOBS_STATE[jid]


# ---------------------------------------------------------------- Claude CLI
def find_claude():
    p = shutil.which("claude")
    if p:
        return p
    pats = [os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude")]
    for c in pats:
        if os.path.exists(c):
            return c
    found = sorted(glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")),
        key=os.path.getmtime)
    return found[-1] if found else None


class ClaudeUnavailable(Exception):
    pass


PROMPT_RULES = """أنت قارئ محاسبي محترف يجهّز نص مستند محاسبي ليُقرأ بصوت بشري لمستمع لا يرى الصفحة.
المطلوب: نص عربي مفهوم بالسماع وحده، بنفس محتوى المستند، بلا زيادة معلومات ولا حذف.

القواعد:
1. انقل الكلام كما هو في المستند دون تغيير المعنى، ولا تضف معلومة ولا رأياً ولا تحذف بنداً أو رقماً.
2. ترتيب القراءة (مهم جداً): المستند العربي يُقرأ صفاً صفاً من الأعلى إلى الأسفل، وفي كل صف ابدأ بالجزء الأيمن ثم الأيسر: أعلى اليمين، ثم أعلى اليسار، ثم الوسط يمين، ثم الوسط يسار، ثم الأسفل يمين، ثم الأسفل يسار. لا تقرأ الجهة اليسرى قبل اليمنى في المستند العربي، ولا تقرأ عموداً كاملاً ثم تعود للأعلى. أما المستند الإنجليزي فبنفس الطريقة لكن يبدأ كل صف من اليسار ثم اليمين.
3. القيود اليومية: ابدأ بالتاريخ إن وُجد، ثم اقرأ القيد بصيغة: «من حساب كذا مبلغ كذا، إلى حساب كذا مبلغ كذا». وفي القيد المركّب: «من مذكورين: حساب كذا مبلغ كذا، وحساب كذا مبلغ كذا، إلى حساب كذا مبلغ كذا». واذكر الشرح (البيان) بعده إن وُجد: «وذلك عن كذا».
4. «حـ/» و«ح/» تُكتب «حساب». «مدين» و«دائن» تبقى كما هي. الرقم بين قوسين في القوائم المالية يعني قيمة سالبة: اكتب كلمة «سالب» قبل الرقم بدل القوسين.
5. الجداول (أهم قاعدة): لا تنسخ الخلايا. حوّل كل صف إلى جملة عربية كاملة مفهومة وحدها: اسم البند، ثم اسم العمود، ثم القيمة. مثال: «رصيد النقدية في نهاية السنة الحالية خمسون ألف ريال، مقابل أربعين ألفاً في السنة السابقة». إذا كان للجدول عنوان أو شرط عام فاذكره قبل جمله. وإذا كان للجدول إجمالي فاختم به: «وبذلك يكون الإجمالي كذا».
6. حسابات T وميزان المراجعة: اذكر اسم الحساب ثم الجانب المدين ثم الجانب الدائن ثم الرصيد وطبيعته.
7. اكتب الرموز كلمات تُنطق: ر.س = ريال سعودي، % = بالمئة، + = يُضاف، − = يُطرح، = = يساوي، ↑ = يرتفع، ↓ = ينخفض، → = يؤدي إلى. لا تترك رمزاً بلا نطق.
8. الأرقام: اتركها أرقاماً كما في المستند (مثل 500,000) ولا تكتبها حروفاً أبداً، والتواريخ كذلك، فالنظام يحوّلها لاحقاً.
9. المصطلحات الإنجليزية اتركها بالإنجليزية وصحّح إملاءها، ولا تترجمها إلا إذا كان المستند يذكر ترجمتها.
10. شكّل فقط الكلمات التي قد تُقرأ خطأ بدون تشكيل (مثل: دَين ودِين، مُدين، دائِن، قِيد)، ولا تشكّل كل النص.
11. كل سطر جملة واحدة تامة تُقرأ في نَفَس واحد، لا تزيد على نحو 150 حرفاً، وتنتهي بنقطة. لا تجمع بندين أو جملتين في سطر واحد.
11ب. الإبراز يُؤخذ من شكل المستند نفسه فقط: ضع العلامة # ثم مسافة في أول السطر فقط إذا كان هذا الكلام مميّزاً بصرياً في الصورة عمّا حوله (خط أكبر، أو لون مختلف، أو خط عريض، أو تحته خط، أو عليه تظليل أو إطار). أما الكلام الذي شكله مثل ما فوقه وما تحته وما بجانبه فاكتبه سطراً عادياً بلا # حتى لو بدا كعنوان، ليبقى ما له نفس الأهمية بنفس الشكل. وفي النص المكتوب بلا صورة لا تضع # إلا لعنوان واضح.
12. الروابط بين الجمل: أضف رابطاً قصيراً متنوعاً فقط حيث يخدم الانتقال (ثم، وبعد ذلك، وفي قيد آخر، وفي المقابل، وعليه، وأخيراً)، ولا تكرر الرابط نفسه في سطرين متتاليين، وكثير من الجمل لا تحتاج رابطاً.
13. تجاهل أرقام الصفحات والترويسات والتذييلات المتكررة والأختام والعلامات المائية.
14. الدقة الإملائية: انقل كل كلمة بحروفها الصحيحة كما في الصورة، وراجع كل كلمة قبل كتابتها، فلا تسقط حرفاً ولا تبدّله (مثلاً: «مشتريات» لا «مشريات»، و«اشتريت» لا «شريت»). إذا كان خط اليد غير واضح فاختر الكلمة المحاسبية الصحيحة التي يدل عليها السياق.
15. إذا كان جزء غير مقروء فاكتب: «هنا جزء غير مقروء». ولا تخمّن الأرقام أبداً.

صيغة الإخراج: لكل صفحة سطر واحد فقط بالشكل === الصفحة N === (N بالأرقام اللاتينية 1 و2 و3، لا الرومانية) ثم نص تلك الصفحة. لا مقدمة ولا شرح ولا ملاحظات، ولا أي تنسيق آخر غير علامة # في أول سطر العنوان."""


def run_claude(prompt, cwd, job=None):
    job = job if job is not None else {"live": "", "cancel": False}
    exe = find_claude()
    if not exe:
        raise ClaudeUnavailable("not_found")
    env = dict(os.environ)
    try:
        p = subprocess.Popen([exe, "-p", prompt, "--allowedTools", "Read", "--output-format", "text"],
                             cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError:
        raise ClaudeUnavailable("not_found")
    out = []
    start = time.time()
    for line in p.stdout:
        out.append(line)
        job["live"] = "".join(out)[-240:]
        if job["cancel"]:
            p.kill()
            raise RuntimeError("cancelled")
        if time.time() - start > 900:
            p.kill()
            raise RuntimeError("timeout")
    p.wait()
    text = "".join(out)
    if re.search(r"Not logged in|Please run /login|Invalid API key|authentication", text, re.I):
        raise ClaudeUnavailable("login")
    if p.returncode != 0:
        raise ClaudeUnavailable("failed: " + text[-300:])
    return text


# ---------------------------------------------------------------- PDF
def norm_ar(s):
    s = unicodedata.normalize("NFKC", s)  # Arabic presentation forms → base letters
    s = s.replace("ـ", "") if "حـ/" not in s else s
    return s


def page_text(page):
    return norm_ar(page.get_text("text", sort=True)).strip()


def has_ocr():
    try:
        import Vision  # noqa: F401  (only on the Mac)
        return True
    except Exception:
        return False


def tess_ok():
    return bool(shutil.which("tesseract"))


def tess_ocr(path):
    """Free text recognizer that runs on the server itself (no outside service): fine for printed pages, weak on handwriting."""
    try:
        r = subprocess.run(["tesseract", str(path), "-", "-l", "ara+eng", "--psm", "3"], capture_output=True, text=True, timeout=180)
        return r.stdout
    except Exception:
        return ""


DOC_OCR_RULES = """أنت قارئ محترف يجهّز نص صفحات ليُقرأ بصوت بشري لمستمع لا يرى الصفحة.
النص التالي مستخرج آلياً من صور الصفحات بقارئ نصوص، ففيه أخطاء في الحروف والأرقام ورموز زائدة، وقد تتداخل أعمدة الجداول أو يختلف ترتيبها.
المطلوب لكل صفحة:
1. صحّح الكلمات الخاطئة بحسب السياق، واحذف الرموز والحروف الزائدة التي لا معنى لها، ولا تخترع معلومة غير موجودة، ولا تخمّن رقماً غير واضح.
2. اقرأ المستند العربي من اليمين لليسار صفاً صفاً، ورتّب الكلام ترتيباً منطقياً مفهوماً بالسماع. حوّل الجداول إلى جمل كاملة: اسم البند ثم قيمته.
3. كل سطر جملة واحدة تامة تنتهي بنقطة. العنوان البارز فقط يبدأ بالعلامة # ثم مسافة.
4. اكتب الرموز كلمات (% = بالمئة، - بين رقمين = إلى). الأرقام تبقى أرقاماً.
5. تجاهل أرقام الصفحات المنفردة والترويسات المتكررة.
صيغة الإخراج: لكل صفحة سطر === الصفحة N === ثم نصها، بلا أي مقدمة أو شرح."""


def ocr_then_groq(job, pages, n):
    """Plan B for files when Google's assistant is busy: read each page on the server, then let Groq fix and arrange it."""
    from concurrent.futures import ThreadPoolExecutor
    texts, done = {}, [0]

    def read(p):
        t = local_clean(norm_ar(tess_ocr(p["path"]))).strip()
        done[0] += 1
        job["stage"] = f"نقرأ الصفحات بطريقة بديلة ({done[0]} من {n})"
        job["progress"] = 0.15 + 0.55 * done[0] / n
        return p["n"], t

    with ThreadPoolExecutor(max_workers=2) as ex:
        for k, t in ex.map(read, pages):
            if t:
                texts[k] = t
    if not texts:
        raise RuntimeError("empty")
    groups, cur, size = [], [], 0
    for k in sorted(texts):
        if cur and size + len(texts[k]) > 1800:
            groups.append(cur)
            cur, size = [], 0
        cur.append(k)
        size += len(texts[k])
    if cur:
        groups.append(cur)
    out = {}
    job["stage"] = "نرتّب الصفحات…"

    def arrange(g):
        body = "\n\n".join(f"=== الصفحة {k} ===\n{texts[k]}" for k in g)
        prompt = (DOC_OCR_RULES + "\n\n" + body)
        try:
            return {x["n"]: x for x in parse_pages(groq_chat(prompt, budget=240, max_tokens=min(5000, 600 + len(body))), g)
                    if x["n"] in g and x["text"].strip()}
        except ClaudeUnavailable as e:
            alog(f"[groq-chat] pages {g[0]}-{g[-1]} not arranged: {e!r}"[:160])
            return {}

    with ThreadPoolExecutor(max_workers=2) as ex:
        for got in ex.map(arrange, groups):
            out.update(got)
    arranged = len(out)
    for k in sorted(texts):
        if k not in out:
            out[k] = {"n": k, "text": texts[k]}
    return [out[k] for k in sorted(out)], arranged


def ocr_image(path):
    """macOS built-in text recognition (Arabic + English), lines in reading order."""
    try:
        import Vision
        from Foundation import NSURL
    except Exception:
        return ""
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["ar-SA", "en-US"])
    req.setUsesLanguageCorrection_(True)
    h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    if not h.performRequests_error_([req], None)[0]:
        return ""
    items = []
    for o in req.results() or []:
        bb = o.boundingBox()
        c = o.topCandidates_(1)
        if c and len(c):
            items.append((bb.origin.y + bb.size.height / 2, bb.origin.x, bb.size.height, str(c[0].string())))
    items.sort(key=lambda t: -t[0])
    lines, cur = [], []
    for it in items:
        if cur and abs(cur[-1][0] - it[0]) > max(0.006, 0.5 * min(cur[-1][2], it[2])):
            lines.append(cur)
            cur = []
        cur.append(it)
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        rtl = any("؀" <= ch <= "ۿ" for t in ln for ch in t[3])
        ln.sort(key=lambda t: -t[1] if rtl else t[1])
        out.append(" ".join(t[3] for t in ln))
    return "\n".join(out)


def local_clean(text):
    lines = []
    for ln in text.splitlines():
        t = re.sub(r"\s+", " ", ln).strip()
        if not t:
            continue
        if re.fullmatch(r"[-–—\s]*(صفحة|page)?\s*[0-9٠-٩]+\s*(من|of)?\s*[0-9٠-٩]*[-–—\s]*", t, re.I):
            continue
        lines.append(t)
    return "\n".join(lines)


def parse_pages(text, nums):
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*=+\s*الصفحة\s*([0-9٠-٩]+)\s*=+\s*$", line)
        if not m and re.match(r"^\s*=+\s*الصفحة\s*[^=]*=+\s*$", line):
            continue  # a stray page-title line (e.g. roman numerals): never part of the text
        if m:
            n = int(m.group(1).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
            cur = {"n": n, "lines": []}
            out.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
        elif line.strip():
            cur = {"n": nums[0], "lines": [line]}
            out.append(cur)
    return [{"n": p["n"], "text": "\n".join(p["lines"]).strip()} for p in out if "\n".join(p["lines"]).strip()]


def gemini_models():
    """Ask Google which models this key can use right now (names change when old versions are retired),
    and order them: the regular «flash» ones first (more accurate reading), then «flash-lite» as a fallback when busy."""
    import requests

    if _MODEL_CACHE["list"] and time.time() - _MODEL_CACHE["at"] < 3600:
        return _MODEL_CACHE["list"]
    found = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": GEMINI_KEY}, timeout=30)
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []) or "flash" not in name:
                continue
            if any(w in name for w in ("image", "tts", "audio", "live", "embedding", "thinking", "exp")):
                continue
            found.append(name)
    except Exception as e:
        print("[assistant] could not list models:", repr(e)[:200], flush=True)

    def rank(n):
        ver = [int(x) for x in re.findall(r"\d+", n)[:2]] + [0, 0]
        return ("lite" in n, "preview" in n, -ver[0], -ver[1], "latest" not in n, n)

    found.sort(key=rank)
    lst = list(dict.fromkeys([m for m in (os.environ.get("GEMINI_MODEL", ""),) if m] + found + GEMINI_MODELS))
    _MODEL_CACHE.update(at=time.time(), list=lst)
    print("[assistant] models:", ", ".join(lst[:6]), flush=True)
    return lst


ASSIST_LOG = []
EXHAUSTED = {}  # version name -> time until which its free limit is considered used up


def alog(msg):
    print(msg, flush=True)
    ASSIST_LOG.append(time.strftime("%H:%M:%S ") + msg)
    del ASSIST_LOG[:-30]


def ask_gemini(prompt, image_path=None, image_paths=None, audio_paths=None, timeout=None, budget=150, rounds=4, retry_busy=True):
    """One request to Google's free Gemini assistant; tries the next model name if one is unknown,
    and waits a little when the free limit is hit for a moment."""
    import base64
    import requests

    parts = [{"text": prompt}]
    for ip in ([image_path] if image_path else []) + list(image_paths or []):
        mime = "image/png" if str(ip).lower().endswith(".png") else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(Path(ip).read_bytes()).decode()}})
    for ap in audio_paths or []:
        parts.append({"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(Path(ap).read_bytes()).decode()}})
    def body_for(model, level):
        cfg = {"temperature": 0.2}
        if level:  # reading and arranging text needs almost no «thinking»; skipping it makes the answer much faster
            cfg["thinkingConfig"] = {"thinkingBudget": 0} if model.startswith("gemini-2") else {"thinkingLevel": level}
        return {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}

    start = time.time()

    def left():
        return budget - (time.time() - start)

    def ask_model(model):
        """One version: the answer text, or (reason, busy) when it can't answer now."""
        levels = ["minimal", "low", None]  # least thinking first; step down only if this version refuses the setting
        last = "?"
        for attempt in range(6):
            if left() < 5:
                return ("out of time", False)
            t0 = time.time()
            try:  # a version that is slow today is skipped instead of making the reader wait minutes
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body_for(model, levels[0]),
                                  timeout=(10, max(5, min(timeout or (120 if "lite" in model else 75), left()))))
            except requests.exceptions.RequestException as e:
                alog(f"[assistant] {model} no answer ({time.time() - t0:.0f}s): {type(e).__name__}")
                return ("timeout", True)
            alog(f"[assistant] {model} {r.status_code} in {time.time() - t0:.0f}s ({levels[0]})")
            if r.status_code == 400 and levels[0] and "think" in r.text.lower():
                levels.pop(0)  # this version doesn't accept that setting: ask again with the next one
                continue
            if r.status_code == 404:
                EXHAUSTED[model] = time.time() + 86400  # this version no longer exists: stop asking it
                return (f"model {model} not found", False)
            if r.status_code == 429:
                body = r.text
                if "PerDay" in body or "per day" in body.lower():
                    EXHAUSTED[model] = time.time() + 3 * 3600  # today's free limit is used up: skip it for hours
                    alog(f"[assistant] {model}: daily limit used up")
                    return ("daily limit", False)
                m = re.search(r'"retryDelay":\s*"(\d+)', body)
                wait = min(int(m.group(1)) if m else 15, 40)
                EXHAUSTED[model] = time.time() + wait  # too many requests this minute: rest it a few seconds
                alog(f"[assistant] {model}: per-minute limit, rest {wait}s | {body[:160]!r}")
                return ("minute limit", True)
            if r.status_code in (500, 503):
                last = f"busy {r.status_code}"
                alog(f"[assistant] {model} says: {r.text[:140]!r}")
                if attempt >= 1 or not retry_busy or time.time() - t0 > 15:  # slow «busy» answers: don't wait for a second one
                    return (last, True)  # still busy after one retry: try the next version
                time.sleep(2)
                continue
            if r.status_code != 200:
                raise ClaudeUnavailable(f"gemini {r.status_code}: {r.text[:200]}")
            try:
                return "".join(x.get("text", "") for x in r.json()["candidates"][0]["content"]["parts"] if not x.get("thought"))
            except Exception:
                return ("empty", True)
        return (last, True)

    last = "no version available"
    for rnd in range(rounds):  # Google sometimes says «busy» for every version at once: wait a little and go round again
        busy, soonest = False, None
        for model in gemini_models():
            until = EXHAUSTED.get(model, 0)
            if until > time.time():  # resting (limit hit) or gone: don't waste time asking it now
                if until - time.time() < 60:
                    busy, soonest = True, min(soonest or until, until)
                continue
            got = ask_model(model)
            if isinstance(got, str):
                return got
            last, b = got
            busy = busy or b
        if not busy or rnd == rounds - 1 or left() < 20:
            break
        wait = max(5, min(40, (soonest - time.time()) if soonest else 10 * (rnd + 1)))
        wait = min(wait, max(0, left() - 15))
        alog(f"[assistant] all versions busy: waiting {wait:.0f}s, round {rnd + 2}")
        time.sleep(wait)
    raise ClaudeUnavailable(f"gemini failed: {last}")


def prepare_worker(job, src: Path, name: str, use_claude: bool):
    import fitz

    try:
        work = src.parent
        is_pdf = src.suffix.lower() == ".pdf"
        job["stage"] = "نفتح الملف…"
        if is_pdf:
            doc = fitz.open(str(src))
            if doc.needs_pass:
                raise RuntimeError("bad_pdf")
            total = doc.page_count
            n = min(total, MAX_PAGES)
            pages = []
            for i in range(n):
                job["stage"] = f"نجهّز الصفحة {i + 1} من {n}"
                job["progress"] = 0.02 + 0.13 * i / max(1, n)
                pg = doc[i]
                pix = pg.get_pixmap(dpi=200)
                img = work / f"page-{i + 1}.jpg"
                pix.save(str(img), jpg_quality=88)  # much smaller than PNG: uploads faster, same clarity for reading
                pages.append({"n": i + 1, "img": img.name, "raw": page_text(pg)})
        else:
            total = n = 1
            pages = [{"n": 1, "img": src.name, "raw": ""}]

        result, note, engine = [], "", "local"
        if use_claude:
            try:
                # every page goes to Claude at the same time (one request per page), so two pages
                # take about as long as one instead of twice as long
                from concurrent.futures import ThreadPoolExecutor

                def one_page(p):
                    if GEMINI_KEY:  # online server: Google's free assistant reads the page image directly
                        parts = [PROMPT_RULES, "", f"الصورة المرفقة هي الصفحة {p['n']}. اقرأها بعناية."]
                    else:          # the Mac: the Claude assistant linked to this computer
                        parts = [PROMPT_RULES, "", "استخدم أداة Read لفتح صورة الصفحة التالية، واقرأها بعناية:",
                                 f"- الصفحة {p['n']}: {p['img']}"]
                    if p["raw"]:
                        parts.append("\nللمساعدة فقط: هذا النص المستخرج آلياً من نفس الصفحة (قد يكون ترتيبه أو اتجاهه خاطئاً، والمرجع هو الصورة؛ استفد منه في دقة الأرقام والإملاء):")
                        parts.append(f"=== نص آلي للصفحة {p['n']} ===\n{p['raw'][:6000]}")
                    prompt = "\n".join(parts)
                    out = ask_gemini(prompt, work / p["img"], **QUICK) if GEMINI_KEY else run_claude(prompt, work, job)
                    got = parse_pages(out, [p["n"]])
                    if not got:
                        raise ClaudeUnavailable("empty")
                    for g in got:
                        g["n"] = p["n"]
                    return got

                # small files: split into up to 3 requests that run at once (faster); big files: 8 pages per request
                # (a 27-page file = 4 requests, so Google's per-minute and daily free limits are not hit)
                GEMINI_BATCH = max(1, min(8, -(-len(pages) // 3)))
                # with a plan B ready, Google gets one quick try instead of waiting through its busy spells
                QUICK = dict(budget=120, rounds=1, retry_busy=False) if (GROQ_KEY and tess_ok() and not has_ocr()) else {}

                def page_block(batch):
                    nums = [p["n"] for p in batch]
                    parts = [PROMPT_RULES, "",
                             f"الصور المرفقة هي الصفحات التالية بالترتيب: {'، '.join(map(str, nums))}. "
                             "اقرأ كل صفحة بعناية على حدة، وأخرج لكل صفحة عنوانها === الصفحة N === ثم نصها، "
                             "ولا تدمج صفحتين ولا تتخطَّ أي صفحة."]
                    for p in batch:
                        if p["raw"]:
                            parts.append(f"\nللمساعدة فقط، نص آلي للصفحة {p['n']} (قد يكون ترتيبه خاطئاً، والمرجع هو الصورة):\n{p['raw'][:4000]}")
                    out = ask_gemini("\n".join(parts), image_paths=[work / p["img"] for p in batch], **QUICK)
                    got = {g["n"]: g for g in parse_pages(out, nums) if g["n"] in nums and g["text"].strip()}
                    for p in batch:  # a page that came back missing or empty is read again, alone
                        if p["n"] not in got:
                            for g in one_page(p):
                                got[p["n"]] = g
                    return [got[p["n"]] for p in batch if p["n"] in got]

                if GEMINI_KEY:
                    batches = [pages[b:b + GEMINI_BATCH] for b in range(0, len(pages), GEMINI_BATCH)]
                    job["stage"] = f"المساعد الذكي يقرأ الصفحات ({n})"
                    with ThreadPoolExecutor(max_workers=min(3, len(batches))) as ex:  # up to 3 requests at once
                        futs = [ex.submit(page_block, b) for b in batches]
                        for k, f in enumerate(futs):
                            result.extend(f.result())
                            job["progress"] = 0.15 + 0.8 * (k + 1) / len(batches)
                            job["stage"] = f"جهزت {min((k + 1) * GEMINI_BATCH, n)} من {n} صفحات"
                    if not result:
                        raise ClaudeUnavailable("empty")
                    pages_to_do = []
                else:
                    pages_to_do = pages

                done = [0]
                job["stage"] = f"المساعد الذكي يقرأ الصفحات كلها مع بعض ({n})"
                with ThreadPoolExecutor(max_workers=max(1, min(6, len(pages_to_do)))) as ex:
                    futs = [ex.submit(one_page, p) for p in pages_to_do]
                    for f in futs:
                        result.extend(f.result())
                        done[0] += 1
                        job["progress"] = 0.15 + 0.8 * done[0] / len(pages)
                        job["stage"] = f"جهزت {done[0]} من {n} صفحات"
                engine = "claude"
            except ClaudeUnavailable as e:
                result = []
                print("[assistant]", repr(e)[:300], flush=True)
                LAST_AI_ERROR[0] = re.sub(r"(key=|AIza|AQ\.)[\w.-]+", "***", repr(e))[:300]
                if GEMINI_KEY and not has_ocr():
                    if not (GROQ_KEY and tess_ok()):
                        # no reliable way to read the page images here, and a PDF's own text layer often comes out
                        # reversed with missing letters: say so clearly instead of reading nonsense
                        raise RuntimeError("ai_busy")
                    for p in pages:
                        p["path"] = work / p["img"]
                    result, arranged = ocr_then_groq(job, pages, n)
                    note = ("المساعد الأساسي كان زحمة، فقرأنا الملف بطريقة بديلة. الكلام المطبوع يكون جيد، "
                            "بس الجداول وخط اليد ممكن تكون أقل دقة. تقدر تعيد التجهيز بعد شوي للحصول على أفضل قراءة.")
                    if not arranged:
                        note = "المساعد الذكي كان زحمة، فقرأنا الصفحات بدون ترتيب. أعد التجهيز بعد شوي للحصول على أفضل قراءة."
                    engine = "ocr"
                    pages_to_do = []
                    if not result:
                        raise RuntimeError("empty")
                else:
                    note = "تعذّر التجهيز الذكي هذه المرة، فقُرئت الصفحات كما هي." if GEMINI_KEY else ("Claude غير مربوط في هذا الجهاز، فقُرئت الصفحات كما هي بدون تجهيز محاسبي للجداول والقيود. "
                            "شغّل ملف «ربط Claude» مرة واحدة ثم أعد التحويل للحصول على أفضل قراءة.") if str(e) in ("login", "not_found") else \
                           "تعذّر التجهيز عن طريق Claude هذه المرة، فقُرئت الصفحات كما هي."
        if not result:
            # Local reading: the PDF text layer often comes out reversed or with broken Arabic letters,
            # so each page is read from its image with the Mac's own text recognition instead.
            for k, p in enumerate(pages):
                if job["cancel"]:
                    raise RuntimeError("cancelled")
                job["stage"] = f"نقرأ الصفحة {p['n']} من {n}"
                job["progress"] = 0.15 + 0.8 * k / len(pages)
                txt = ocr_image(work / p["img"]) or p["raw"]
                txt = local_clean(norm_ar(txt))
                if txt.strip():
                    result.append({"n": p["n"], "text": txt})
            if not result:
                raise RuntimeError("empty")
        if total > MAX_PAGES:
            note = (note + " " if note else "") + f"قُرئت أول {MAX_PAGES} صفحة من {total}."
        transcript = "\n\n".join(f"=== الصفحة {p['n']} ===\n{p['text']}" for p in result)
        job["result"] = {"name": name, "text": transcript, "note": note, "engine": engine}
        job["progress"] = 1.0
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e) if str(e) in ("cancelled", "bad_pdf", "empty", "timeout", "ai_busy") else "failed"
        job["detail"] = repr(e)[:400]


# ---------------------------------------------------------------- password (only on the online server)
PUBLIC = {"/login", "/og.png", "/icon.png", "/apple-touch-icon.png", "/favicon.png", "/manifest.json", "/health"}


@app.get("/health")
def health():
    """The free alarm knocks here every few minutes so the online server never falls asleep."""
    return {"ok": True}


def _abs_images(html, request):
    """Link previews (WhatsApp and others) need the full address of the preview image."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return html.replace('content="/og.png"', f'content="{proto}://{host}/og.png"') if host else html


def _token():
    return hashlib.sha256(("hams:" + APP_PASSWORD).encode()).hexdigest()


LOGIN_PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>همس</title>
<meta property="og:title" content="همس"><meta property="og:description" content="يقرأ لك… ويكتب عنك">
<meta property="og:image" content="/og.png"><meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.png"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Aref+Ruqaa:wght@700&family=IBM+Plex+Sans+Arabic:wght@400;500&display=swap">
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07060d;color:#f4efe6;font-family:'IBM Plex Sans Arabic',sans-serif;
background-image:radial-gradient(45% 45% at 50% 35%,rgba(233,180,76,.18),transparent 70%)}
form{width:min(360px,90vw);text-align:center}.w{font-family:'Aref Ruqaa';font-weight:700;font-size:96px;line-height:1.5;display:inline-block;padding:.5em .15em .1em;margin-top:-.6em;transform:rotate(-6deg);
background:linear-gradient(90deg,#f3c766,#e2557a);-webkit-background-clip:text;background-clip:text;color:transparent}
p{color:#8d8798;margin:0 0 22px}input{width:100%;height:50px;border-radius:14px;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.04);color:#f4efe6;font:inherit;font-size:17px;padding:0 16px;text-align:center}
button{margin-top:12px;width:100%;height:50px;border:0;border-radius:999px;background:#e9b44c;color:#07060d;font:inherit;font-weight:600;font-size:17px;cursor:pointer}
.err{color:#ff8a7a;margin-top:12px;min-height:1.5em}</style></head><body>
<form method="post" action="/login"><div class="w">همس</div><p>يقرأ لك… ويكتب عنك</p>
<input type="password" name="password" placeholder="كلمة السر" autofocus aria-label="كلمة السر"><button type="submit">ادخل</button><div class="err">%ERR%</div></form></body></html>"""


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if not APP_PASSWORD or request.url.path in PUBLIC or request.cookies.get("hams") == _token():
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "login"}, status_code=401)
    return HTMLResponse(_abs_images(LOGIN_PAGE.replace("%ERR%", ""), request))


@app.post("/login")
def login(password: str = Form("")):
    if not APP_PASSWORD or password.strip() != APP_PASSWORD:
        return HTMLResponse(LOGIN_PAGE.replace("%ERR%", "كلمة السر غير صحيحة"), status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("hams", _token(), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax", secure=True)
    return r


# the word «همس» as preview image and icons
for _name in ("og.png", "icon.png", "apple-touch-icon.png", "favicon.png"):
    def _serve(_n=_name):
        return FileResponse(ROOT / "static" / _n, headers={"Cache-Control": "public, max-age=86400"})
    app.add_api_route("/" + _name, _serve, methods=["GET"])


@app.get("/manifest.json")
def manifest():
    return JSONResponse({"name": "همس", "short_name": "همس", "description": "يقرأ لك… ويكتب عنك", "lang": "ar", "dir": "rtl",
                         "start_url": "/", "display": "standalone", "background_color": "#07060d", "theme_color": "#07060d",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
                                   {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}]})


# ---------------------------------------------------------------- voice → text
AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".oga", ".opus", ".webm", ".mp4", ".3gp", ".amr",
             ".flac", ".caf", ".mpeg", ".mpga", ".wma", ".mov")
MAX_AUDIO_MIN = 90

VOICE_STYLES = {
    "tidy": "مرتّب: اكتب كل ما قيل بنفس كلمات المتكلم وبنفس ترتيبه، واحذف فقط الحشو والتلعثم والتكرار (مثل: يعني، امم، إيه، اللي هو، "
            "والكلمة أو الجملة التي يعيدها المتكلم مرتين)، وضع علامات الترقيم (، . ؟ :) في مكانها. لا تلخّص ولا تعِد الصياغة ولا تحذف أي معلومة "
            "أو اسم أو حكم أو مثال أو رقم: النص المرتّب يكون قريباً جداً من طول الكلام الأصلي بعد حذف الحشو فقط.",
    "short": "مختصر: مثل المرتّب، لكن اختصر كل فقرة إلى أهم ما فيها، مع الإبقاء على كل المعلومات المهمة والتعريفات والأرقام والأمثلة الأساسية.",
    "summary": "مرتّب مع ملخص: ابدأ بعنوان «الملخص» وتحته من 5 إلى 10 نقاط قصيرة لأهم ما قيل، كل نقطة تبدأ بـ «- » ووقتها هو وقت أول ذكر لها، "
               "ثم اكتب الكلام كاملاً مرتّباً: احذف الحشو والتلعثم والتكرار دون حذف أي معلومة.",
    "verbatim": "حرفي: اكتب كل كلمة كما قيلت بالضبط، بما فيها التكرار والحشو، وأضف علامات الترقيم فقط.",
}
VOICE_LANGS = {
    "spoken": "اكتب الكلام بلهجته كما قيل بنفس كلمات المتكلم (مثل: ما يصير، يجيب، رجعته، كلمته، شوي)، ولا تحوّل أي كلمة إلى الفصحى، لكن بإملاء عربي سليم.",
    "fusha": "حوّل الكلام إلى فصحى مبسّطة وواضحة، مع الحفاظ على المعنى والمصطلحات وأسلوب الشرح.",
}


def voice_prompt(style, lang):
    return f"""أنت كاتب محترف يفرّغ تسجيلاً صوتياً بالعربية (محاضرة أو شرح أو ملاحظات أو حديث) إلى نص مكتوب سليم ومرتّب ترتيباً ممتازاً.

الأسلوب المطلوب: {VOICE_STYLES.get(style, VOICE_STYLES['tidy'])}
اللغة: {VOICE_LANGS.get(lang, VOICE_LANGS['spoken'])}

القواعد:
1. لا تضف معلومة ولا رأياً ليس في التسجيل.
2. الفقرات: اجمع كل الجمل المتتالية التي تتكلم عن نفس الفكرة أو نفس الشخص في فقرة واحدة متصلة (عادةً من 2 إلى 6 جمل)، ولا تجعل كل جملة قصيرة في سطر وحدها. ضع عنواناً قصيراً فقط عند انتقال واضح إلى موضوع جديد (مثل: الانتقال للكلام عن شخص آخر أو محور آخر)، والعنوان مأخوذ من الكلام نفسه (مثل اسم الشخص).
3. المصطلحات الإنجليزية اكتبها بالإنجليزية بإملاء صحيح، والأرقام أرقاماً.
4. إذا تكلم شخص آخر غير المتكلم الأساسي (مثل سؤال من طالب) فابدأ فقرته بكلمة «سؤال:» أو «مداخلة:».
5. الجزء غير المسموع اكتبه: [غير واضح]. لا تخمّن.
6. انتبه للإملاء ولا تسقط حرفاً من أي كلمة، وانقل أسماء الأشخاص كما وردت.

صيغة الإخراج (مهمة جداً): كل سطر يبدأ بوقت بدايته في التسجيل بين قوسين مربعين بالأرقام اللاتينية بالشكل [دقائق:ثواني] مثل [03:12] (أو [ساعات:دقائق:ثواني] بعد الساعة الأولى)،
ثم إما عنوان بالشكل: # العنوان، أو نص الفقرة كاملة في نفس السطر.
مثال:
[00:00] # المقدمة
[00:04] نص الفقرة الأولى.
لا مقدمة ولا خاتمة ولا أي تنسيق آخر."""


def audio_seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def parse_voice(out, dur):
    segs = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        m = re.match(r"^\[?\s*(\d{1,2})\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?\s*\]?\s*:?\s*(.*)$", line)
        if m:
            a, b, c, rest = m.groups()
            t = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
        else:
            t, rest = (segs[-1]["t"] if segs else 0), line
        rest = rest.strip()
        if not rest:
            continue
        head = rest.startswith("#")
        if head:
            rest = rest.lstrip("#").strip()
        if not rest:
            continue
        rest = re.sub(r"^[-–•*]\s+", "• ", rest)  # summary points
        if dur:
            t = min(t, int(dur))
        if not m and segs and not head and not segs[-1]["h"]:
            segs[-1]["x"] += " " + rest  # a paragraph that went on to a second line
            continue
        segs.append({"t": t, "h": head, "x": rest})
    return segs


WHISPER_MODELS = ["whisper-large-v3"]  # the faster «turbo» skips whole stretches of Saudi speech: not used
WHISPER_REST = {}
# phrases the recognizer sometimes invents on silence or at the end of a piece
JUNK = re.compile(r"ترجمة\s+نانسي|نانسي\s+قنقر|اشترك(?:وا)?\s+في\s+القناة|شكرا\s+(?:لكم\s+)?(?:على\s+)?(?:ل?المشاهدة|للمتابعة)|"
                  r"لا\s+تنس(?:وا)?\s+الاشتراك|^\s*موسيقى\s*$|تفريغ\s+.{0,20}قنقر|subscribe|subtitles", re.I)


def cut_points(path, dur, target=60.0, lo=40.0, hi=80.0):
    """Split points every ~40 seconds, each placed in a natural pause of the speech when there is one."""
    pauses = []
    try:
        r = subprocess.run(["ffmpeg", "-i", str(path), "-af", "silencedetect=n=-32dB:d=0.35", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300)
        st = None
        for line in r.stderr.splitlines():
            m = re.search(r"silence_start: ([\d.]+)", line)
            if m:
                st = float(m.group(1))
            m = re.search(r"silence_end: ([\d.]+)", line)
            if m and st is not None:
                pauses.append((st + float(m.group(1))) / 2)
                st = None
    except Exception:
        pass
    cuts, last = [0.0], 0.0
    while dur - last > hi:
        cands = [x for x in pauses if last + lo <= x <= last + hi]
        nxt = min(cands, key=lambda x: abs(x - last - target)) if cands else last + target
        cuts.append(nxt)
        last = nxt
    return cuts + [dur]


def _whisper_piece(path):
    import requests
    last = ""
    for attempt in range(8):
        now = time.time()
        free = [m for m in WHISPER_MODELS if WHISPER_REST.get(m, 0) <= now]
        if not free:
            time.sleep(max(0.5, min(WHISPER_REST.values()) - now))
            continue
        model = free[attempt % len(free)]
        try:
            with open(path, "rb") as fh:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {GROQ_KEY}"},
                                  files={"file": (Path(path).name, fh, "audio/flac")},
                                  data={"model": model, "language": "ar", "response_format": "verbose_json", "temperature": "0"},
                                  timeout=(10, 90))
        except requests.exceptions.RequestException as e:
            last = type(e).__name__
            continue
        if r.status_code == 200:
            return [(float(x.get("start", 0)), (x.get("text") or "").strip()) for x in r.json().get("segments") or []]
        last = f"{r.status_code}: {r.text[:120]}"
        if r.status_code == 429:
            wait = _reset_secs(r.headers.get("retry-after", "")) or _reset_secs(r.headers.get("x-ratelimit-reset-requests", "")) or 10
            WHISPER_REST[model] = time.time() + min(wait, 60) + 0.3
        elif r.status_code >= 500:
            time.sleep(1.5)
        else:
            break
    raise ClaudeUnavailable(f"groq failed: {last}")


def groq_transcribe(path, dur=0.0, job=None):
    """Groq's free speech recognizer. The recording is cut into ~40-second pieces at pauses (on long audio the
    recognizer sometimes skips whole stretches), and all pieces are heard at once. Returns [(start_seconds, text), ...]."""
    from concurrent.futures import ThreadPoolExecutor
    dur = dur or audio_seconds(path)
    cuts = cut_points(path, dur)
    folder = Path(path).parent
    pieces = []
    for k in range(len(cuts) - 1):
        a, b = cuts[k], cuts[k + 1]
        if b - a < 0.4:
            continue
        out = folder / f"piece-{Path(path).stem}-{k}.flac"  # lossless: compressed audio made the recognizer miss names and words
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(out)], capture_output=True, timeout=120)
        if out.exists() and out.stat().st_size > 500:
            pieces.append((a, out))
    if not pieces:
        raise RuntimeError("bad_audio")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(8, len(pieces))) as ex:
        results = list(ex.map(lambda pc: _whisper_piece(pc[1]), pieces))
    alog(f"[groq] heard {len(pieces)} pieces in {time.time() - t0:.1f}s")
    raw = []
    for (a, _), segs in zip(pieces, results):
        for t, x in segs:
            x = JUNK.sub("", x).strip(" .،")
            if x and not (raw and raw[-1][1] == x):  # the recognizer also repeats a line at a piece's end
                raw.append((a + t, x))
    if not raw:
        raise ClaudeUnavailable("empty")
    return raw


GROQ_CHAT_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]  # «qwen» allows only ~1000 written words a minute: too little
GROQ_REST = {}  # model -> time until its per-minute free limit has room again


def _reset_secs(v):
    """«20.98s», «1m26.4s», «495ms» → seconds."""
    v = (v or "").strip()
    if not v:
        return 0.0
    if v.endswith("ms") and "m" not in v[:-2]:
        return float(v[:-2]) / 1000
    m = re.match(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?$", v)
    if not m:
        try:
            return float(v)
        except ValueError:
            return 10.0
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)


def groq_chat(prompt, budget=90, max_tokens=8000, effort="low"):
    """Groq's free text assistants (very fast). Each has a small per-minute limit, so requests are spread over all of them."""
    import requests
    start, last = time.time(), "?"
    while time.time() - start < budget:
        for model in GROQ_CHAT_MODELS:
            if GROQ_REST.get(model, 0) > time.time():
                continue
            body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": max_tokens}
            if "gpt-oss" in model:
                body["reasoning_effort"] = effort
            t0 = time.time()
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {GROQ_KEY}"},
                                  timeout=(10, max(10, min(120, budget - (time.time() - start)))))
            except requests.exceptions.RequestException as e:
                last = type(e).__name__
                GROQ_REST[model] = time.time() + 5
                alog(f"[groq-chat] {model} no answer: {last}")
                continue
            alog(f"[groq-chat] {model} {r.status_code} in {time.time() - t0:.1f}s")
            if r.status_code == 200:
                try:
                    txt = r.json()["choices"][0]["message"]["content"] or ""
                except Exception:
                    txt = ""
                txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                if txt:
                    return txt
                last = "empty"
                GROQ_REST[model] = time.time() + 3
                continue
            last = f"{r.status_code}: {r.text[:120]}"
            if r.status_code == 429:
                wait = _reset_secs(r.headers.get("retry-after", "")) or _reset_secs(r.headers.get("x-ratelimit-reset-tokens", "")) or 15
                GROQ_REST[model] = time.time() + min(wait, 70) + 0.5
            elif r.status_code == 413:
                GROQ_REST[model] = time.time() + 20
            else:
                GROQ_REST[model] = time.time() + 5
        soonest = min([GROQ_REST.get(m, 0) for m in GROQ_CHAT_MODELS])
        pause = max(0.5, soonest - time.time())
        if time.time() - start + pause >= budget:
            break
        time.sleep(pause)
    raise ClaudeUnavailable(f"groq chat failed: {last}")


def groq_tidy(style, lang, raw, dur, job=None):
    """Tidy a long recording in pieces (each fits the per-minute limit), all at once; then a short summary if asked."""
    from concurrent.futures import ThreadPoolExecutor
    chunks, cur, size = [], [], 0
    for seg in raw:
        if cur and size + len(seg[1]) > 2800:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += len(seg[1])
    if cur:
        chunks.append(cur)
    base_style = "tidy" if style == "summary" else style
    n = len(chunks)

    def one(k):
        prompt = tidy_prompt(base_style, lang, chunks[k])
        if n > 1:
            prompt += (f"\n\nهذا هو الجزء {k + 1} من {n} من نفس التسجيل: اكتب هذا الجزء فقط، "
                       + ("" if k == 0 else "ولا تبدأ بمقدمة، وضع عنواناً فقط إذا بدأ موضوع جديد فعلاً."))
        # more «thinking» understands the dialect better; on long recordings less, to stay inside the per-minute limit
        return parse_voice(groq_chat(prompt, budget=max(60, 25 * n), max_tokens=5000, effort="medium" if n <= 2 else "low"), dur)

    with ThreadPoolExecutor(max_workers=min(3, n)) as ex:
        parts = list(ex.map(one, range(n)))
    segs = [x for p in parts for x in p]
    if not segs:
        raise ClaudeUnavailable("empty")
    if style == "summary":
        body = "\n".join(f"[{fmt_ts(x['t'])}] " + ("# " if x["h"] else "") + x["x"][:220] for x in segs)[:9000]
        try:
            pts = parse_voice(groq_chat(
                "هذا نص مرتب لمحاضرة، وكل سطر يبدأ بوقته. اكتب ملخصاً من 5 إلى 10 نقاط قصيرة لأهم ما قيل، "
                "بنفس لغة النص ولهجته، كل نقطة في سطر بالشكل: [دقائق:ثواني] - النقطة، ووقتها هو وقت أول ذكر لها. "
                "لا مقدمة ولا خاتمة.\n\n" + body, budget=60, max_tokens=2000), dur)
            pts = [x for x in pts if not x["h"]]
            if pts:
                segs = [{"t": 0, "h": True, "x": "الملخص"}] + pts + segs
        except ClaudeUnavailable as e:
            alog(f"[groq-chat] summary skipped: {e!r}"[:200])
    return segs


def fmt_ts(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def paragraphs(raw):
    """Plain recognizer output grouped into readable paragraphs (used for «حرفي», and when the assistant is busy)."""
    out, cur = [], None
    for t, x in raw:
        if cur and (len(cur["x"]) > 320 or (len(cur["x"]) > 160 and re.search(r"[.!؟?]$", cur["x"]))):
            out.append(cur)
            cur = None
        if cur is None:
            cur = {"t": int(t), "h": False, "x": x}
        else:
            cur["x"] += " " + x
    if cur:
        out.append(cur)
    return out


def tidy_prompt(style, lang, raw):
    lines = "\n".join(f"[{fmt_ts(t)}] {x}" for t, x in raw)
    return voice_prompt(style, lang).replace(
        "يفرّغ تسجيلاً صوتياً بالعربية (محاضرة أو شرح أو ملاحظات أو حديث) إلى نص مكتوب",
        "يحوّل تفريغاً آلياً لتسجيل صوتي بالعربية (محاضرة أو شرح أو ملاحظات أو حديث) إلى نص مكتوب") + f"""

ملاحظات على التفريغ الآلي التالي:
- كل سطر يبدأ بوقته في التسجيل، والأسطر مقطوعة آلياً في أماكن عشوائية (قد تنقطع الجملة بين سطرين): اجمعها في فقرات. وقت الفقرة هو وقت أول سطر منها.
- افهم موضوع التسجيل من الكلام نفسه أولاً (قد يكون عن المحاسبة أو عن تحفيظ القرآن أو غيره)، ثم صحّح على أساسه.
- التفريغ الآلي يخطئ كثيراً في الحروف المتقاربة في النطق (ك وق، ن وم، ب وت، ذ وز، ض وظ، ج وي) وفي الأسماء والمصطلحات. راجع كل كلمة: إذا كانت بلا معنى في السياق فاكتب الكلمة الصحيحة المقصودة (أمثلة: «بنقمل» صوابها «بنكمل»، و«بالمجرم ممتاز» صوابها «بالمجمل ممتاز»، و«سبع الماضي» صوابها «الأسبوع الماضي»، و«اللهم لك حمد» صوابها «اللهم لك الحمد»).
- إذا تكرر اسم شخص بأكثر من شكل بسبب خطأ السماع (مثل «ابراهيم الخليفي» و«برهما خليفي»)، فاكتبه بالشكل الصحيح في كل مكان.
- المصطلحات الإنجليزية المكتوبة بحروف عربية (مثل «الأودت») اكتبها كما ينطقها المتكلم بالعربية بإملاء سليم أو بالإنجليزية إن كانت مصطلحاً معروفاً (مثل audit).
- لا تحذف أي جملة أو معلومة أو مثال أو سؤال من التفريغ أبداً، حتى في الأسلوب المرتّب: الحذف للحشو والتكرار فقط.

التفريغ الآلي:
{lines}"""


def transcribe_worker(job, files, style, lang):
    from concurrent.futures import ThreadPoolExecutor
    try:
        n = len(files)
        done = [0]
        notes = []
        job["stage"] = "نجهّز التسجيل…" if n == 1 else f"نجهّز التسجيلات ({n})"
        job["progress"] = 0.05

        def one(f):
            src, name = f
            dur = audio_seconds(src)
            if dur > MAX_AUDIO_MIN * 60:
                raise RuntimeError("too_long")
            small = src.with_suffix(".small.mp3")
            kbps = "48k" if dur <= 40 * 60 else "24k"  # spoken voice stays clear; the request stays small
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                                "-b:a", kbps, str(small)], capture_output=True, timeout=600)
            if r.returncode != 0 or not small.exists() or small.stat().st_size < 1000:
                raise RuntimeError("bad_audio")
            dur = dur or audio_seconds(small)
            note = ""
            segs = None
            if GEMINI_KEY and GROQ_KEY:
                # Google hears Saudi speech best: one quick try (no waiting when it is busy), Groq right after if not
                job["stage"] = "المساعد الذكي يسمع ويكتب…" if n == 1 else job["stage"]
                try:
                    segs = parse_voice(ask_gemini(voice_prompt(style, lang), audio_paths=[small], timeout=60 + dur / 4,
                                                  budget=60 + dur / 4, rounds=1, retry_busy=False), dur) or None
                    if segs:
                        alog(f"[assistant] voice by google: {len(segs)} lines")
                except ClaudeUnavailable as e:
                    alog(f"[assistant] google busy, using groq: {e!r}"[:160])
            if segs:
                pass
            elif GROQ_KEY:
                # 1) hear it fast with Groq, 2) tidy the words with the assistant (text only: lighter and quicker)
                job["stage"] = "نسمع التسجيل ونكتبه…" if n == 1 else job["stage"]
                raw = groq_transcribe(src, dur, job)  # from the original recording, not the compressed copy
                segs = None
                job["stage"] = "نرتّب النص…" if n == 1 else job["stage"]
                try:
                    segs = groq_tidy(style, lang, raw, dur, job)
                except ClaudeUnavailable as e:
                    alog(f"[groq-chat] tidy failed: {e!r}"[:200])
                if not segs and GEMINI_KEY:  # last try: Google's assistant, briefly
                    try:
                        segs = parse_voice(ask_gemini(tidy_prompt(style, lang, raw), timeout=60, budget=45), dur)
                    except ClaudeUnavailable as e:
                        alog(f"[assistant] tidy skipped: {e!r}"[:200])
                if not segs:
                        note = "المساعد الذكي كان زحمة، فطلع لك النص مثل ما انقال بدون ترتيب. تقدر تعيد الكتابة بعد شوي عشان يترتب."
                segs = segs or paragraphs(raw)
            else:
                out = ask_gemini(voice_prompt(style, lang), audio_paths=[small], timeout=min(420, 90 + dur / 4), budget=max(150, 60 + dur / 3))
                segs = parse_voice(out, dur)
            if not segs:
                raise ClaudeUnavailable("empty")
            if note:
                notes.append(note)
            done[0] += 1
            job["progress"] = 0.1 + 0.85 * done[0] / n
            job["stage"] = "خلصنا الكتابة…" if done[0] == n else f"كتبنا {done[0]} من {n} تسجيلات"
            return {"name": name, "dur": round(dur, 1), "segs": segs}

        job["stage"] = "المساعد الذكي يسمع ويكتب…" if n == 1 else f"المساعد الذكي يسمع ويكتب التسجيلات ({n}) مع بعض"
        job["progress"] = 0.1
        with ThreadPoolExecutor(max_workers=min(3, n)) as ex:  # each recording is its own request, all at the same time
            parts = list(ex.map(one, files))
        lines = []
        for p in parts:
            if n > 1:
                lines.append("# " + p["name"])
            lines += [("# " if s["h"] else "") + s["x"] for s in p["segs"]]
        more = "" if n == 1 else (" ومعه تسجيل آخر" if n == 2 else " ومعه " + str(n - 1).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) + " تسجيلات")
        base = parts[0]["name"] + more
        job["result"] = {"name": base, "kind": "voice", "parts": parts, "text": "\n".join(lines), "note": notes[0] if notes else ""}
        job["progress"] = 1.0
        job["status"] = "done"
    except ClaudeUnavailable as e:
        LAST_AI_ERROR[0] = re.sub(r"(key=|AIza|AQ\.)[\w.-]+", "***", repr(e))[:300]
        job["status"], job["error"] = "error", "ai_busy"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e) if str(e) in ("too_long", "bad_audio") else "failed"
        job["detail"] = repr(e)[:400]
    finally:  # the recordings are not kept on the server
        for src, _ in files:
            shutil.rmtree(src.parent, ignore_errors=True)


# ---------------------------------------------------------------- API
@app.get("/")
def index(request: Request):
    html = (ROOT / "index.html").read_text("utf-8")
    return HTMLResponse(_abs_images(html, request), headers={"Cache-Control": "no-store"})


@app.post("/api/prepare")
async def prepare(file: UploadFile = File(...), use_claude: bool = True):
    jid, job = new_job("prepare")
    work = JOBS / jid
    work.mkdir()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic"):
        raise HTTPException(400, "bad_type")
    src = work / f"source{ext}"
    src.write_bytes(await file.read())
    if ext == ".heic":
        if not shutil.which("sips"):  # the online server has no Mac converter for iPhone photos
            raise HTTPException(400, "bad_type")
        subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(work / "source.png")], capture_output=True)
        src = work / "source.png"
    threading.Thread(target=prepare_worker, args=(job, src, file.filename or "ملف", use_claude), daemon=True).start()
    return {"job": jid}


@app.post("/api/transcribe")
async def transcribe(files: list[UploadFile] = File(...), style: str = Form("tidy"), lang: str = Form("spoken")):
    if not (GEMINI_KEY or GROQ_KEY):
        raise HTTPException(400, "voice_needs_key")
    if not files or len(files) > 8:
        raise HTTPException(400, "too_many")
    jid, job = new_job("transcribe")
    work = JOBS / jid
    work.mkdir()
    saved = []
    for k, f in enumerate(files):
        ext = Path(f.filename or "").suffix.lower()
        if ext not in AUDIO_EXT and not (f.content_type or "").startswith(("audio/", "video/")):
            shutil.rmtree(work, ignore_errors=True)
            raise HTTPException(400, "bad_type")
        src = work / f"audio-{k + 1}{ext or '.bin'}"
        src.write_bytes(await f.read())
        name = re.sub(r"\.[^.]+$", "", f.filename or "") or f"تسجيل {k + 1}"
        saved.append((src, name))
    threading.Thread(target=transcribe_worker, args=(job, saved, style, lang), daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    j = JOBS_STATE.get(jid)
    if not j:
        raise HTTPException(404)
    return {k: v for k, v in j.items() if k != "cancel"}


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: str):
    j = JOBS_STATE.get(jid)
    if j:
        j["cancel"] = True
    return {"ok": True}


# ---------------------------------------------------------------- voice (Hamed)
def cache_path(text):
    return CACHE / (hashlib.sha1(json.dumps([text.strip()[:600], VOICE, 2], ensure_ascii=False).encode()).hexdigest() + ".mp3")


async def _speak(text, out):
    import edge_tts

    tmp = out.with_suffix(".part")
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, VOICE).save(str(tmp))
            if tmp.exists() and tmp.stat().st_size > 0:
                # the voice service leaves ~0.2 s of silence before each sentence and ~0.9 s after it;
                # cut both so sentences follow each other without a felt stop
                trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.02,areverse,"
                        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.06,areverse")
                trimmed = out.with_suffix(".trim.mp3")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error", "-f", "mp3", "-i", str(tmp), "-af", trim,
                    "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(trimmed))
                await proc.wait()
                if proc.returncode == 0 and trimmed.exists() and trimmed.stat().st_size > 0:
                    trimmed.replace(out)
                    tmp.unlink(missing_ok=True)
                else:
                    tmp.replace(out)
                return
        except Exception:
            await asyncio.sleep(1 + attempt)
    raise RuntimeError("voice_service")


INFLIGHT = {}  # sentence file -> Event, so the same sentence is never requested twice at once
INFLIGHT_LOCK = threading.Lock()


def speak_many(texts, on_done=None):
    """Record several sentences at the same time; on_done(text) is called as each one finishes."""
    async def run():
        sem = asyncio.Semaphore(PARALLEL)

        async def one(t):
            out = cache_path(t)
            if not out.exists():
                with INFLIGHT_LOCK:
                    ev = INFLIGHT.get(out)
                    mine = ev is None
                    if mine:
                        ev = INFLIGHT[out] = threading.Event()
                if mine:
                    try:
                        async with sem:
                            await _speak(t.strip()[:600], out)
                    finally:
                        with INFLIGHT_LOCK:
                            INFLIGHT.pop(out, None)
                        ev.set()
                else:
                    # the same sentence is already being made elsewhere (reading vs background): wait for it
                    await asyncio.to_thread(ev.wait, 40)
                    if not out.exists():
                        raise RuntimeError("voice_service")
            if on_done:
                on_done(t)

        await asyncio.gather(*(one(t) for t in dict.fromkeys(texts)))

    asyncio.run(run())


class TTSIn(BaseModel):
    text: str


@app.post("/api/tts")
def tts(body: TTSIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    p = cache_path(text)
    if not p.exists():
        try:
            speak_many([text])
        except Exception:
            raise HTTPException(503, "voice_service")
    return FileResponse(p, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    # says only whether a Google key is set (never the key itself), and the last assistant error if any
    return {"ok": True, "claude": bool(find_claude()), "gemini_key": bool(GEMINI_KEY), "assistant_error": LAST_AI_ERROR[0], "assistant_log": ASSIST_LOG[-15:], "models": _MODEL_CACHE["list"][:6]}


# ---------------------------------------------------------------- background recording
# Sentences are recorded ahead of the listener, several at a time, starting from where they are.
REC = {"doc": None, "items": [], "pos": 0, "ready": [], "error": None, "gen": 0}
REC_LOCK = threading.Lock()
REC_WAKE = threading.Event()


def recorder_loop():
    while True:
        REC_WAKE.wait(5)
        REC_WAKE.clear()
        while True:
            with REC_LOCK:
                items, ready, pos, gen = REC["items"], REC["ready"], REC["pos"], REC["gen"]
                order = list(range(pos, len(items))) + list(range(0, pos))
                todo = [i for i in order if not ready[i]]
                # the next 3 sentences first, alone, so reading starts and continues without waiting
                near = [i for i in todo if pos <= i < pos + 3]
                pick = near if near else todo[:PARALLEL * 2]
            if not pick:
                break

            def done(text, gen=gen, items=items):
                with REC_LOCK:
                    if REC["gen"] == gen:
                        for k, x in enumerate(items):
                            if x == text:
                                REC["ready"][k] = True

            try:
                speak_many([items[i] for i in pick], on_done=done)
                with REC_LOCK:
                    REC["error"] = None
            except Exception as e:
                with REC_LOCK:
                    REC["error"] = "voice_service"
                print("[recorder error]", repr(e)[:300], flush=True)
                time.sleep(3)


threading.Thread(target=recorder_loop, daemon=True).start()


class RecordIn(BaseModel):
    doc: str
    items: list[str]
    pos: int = 0


@app.post("/api/record")
def record(body: RecordIn):
    with REC_LOCK:
        REC["gen"] += 1
        REC["doc"] = body.doc
        REC["items"] = [t.strip()[:600] for t in body.items]
        REC["ready"] = [cache_path(t).exists() for t in REC["items"]]
        REC["pos"] = max(0, min(body.pos, len(REC["items"])))
        REC["error"] = None
    REC_WAKE.set()
    return rec_state(body.doc)


class PosIn(BaseModel):
    doc: str
    pos: int


@app.post("/api/record/pos")
def record_pos(body: PosIn):
    with REC_LOCK:
        if REC["doc"] == body.doc:
            REC["pos"] = max(0, min(body.pos, len(REC["items"])))
    REC_WAKE.set()
    return rec_state(body.doc)


@app.get("/api/record/{doc}")
def rec_state(doc: str):
    with REC_LOCK:
        if REC["doc"] != doc:
            return {"active": False}
        return {"active": True, "ready": REC["ready"], "error": REC["error"]}


# ---------------------------------------------------------------- download as one audio file
class ExportIn(BaseModel):
    name: str
    items: list[dict]  # [{say, pause}]


def export_worker(job, name, items):
    try:
        says = [it["say"].strip()[:600] for it in items]
        job["stage"] = "نسجّل الجمل…"
        n_done = [0]

        def done(_):
            n_done[0] += 1
            job["progress"] = 0.9 * n_done[0] / max(1, len(says))

        speak_many(says, on_done=done)
        work = JOBS / f"export-{uuid.uuid4().hex[:8]}"
        work.mkdir()
        lst = []
        for i, (say, it) in enumerate(zip(says, items)):
            lst.append(f"file '{cache_path(say)}'")
            pause = float(it.get("pause", 0.4))
            if pause > 0:
                sil = work / f"s{i}.mp3"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                                "-t", f"{pause:.2f}", "-c:a", "libmp3lame", "-b:a", "48k", str(sil)], check=True)
                lst.append(f"file '{sil}'")
        (work / "list.txt").write_text("\n".join(lst), "utf-8")
        safe = re.sub(r"[^\w\u0600-\u06FF-]+", "-", Path(name).stem).strip("-")[:60] or "قراءة"
        mp3 = EXPORTS / f"{safe}.mp3"
        job["stage"] = "نجمع الملف الصوتي…"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
                        "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "3", str(mp3)], check=True)
        shutil.rmtree(work, ignore_errors=True)
        job["result"] = {"file": mp3.name}
        job["progress"] = 1.0
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = "failed"
        job["detail"] = repr(e)[:400]


@app.post("/api/export")
def export(body: ExportIn):
    jid, job = new_job("export")
    threading.Thread(target=export_worker, args=(job, body.name, body.items), daemon=True).start()
    return {"job": jid}


@app.get("/api/exports/{fname}")
def get_export(fname: str):
    p = (EXPORTS / fname).resolve()
    if p.parent != EXPORTS.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/mpeg", filename=fname)



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8765")), log_level="warning")
