from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os, uuid, asyncio, subprocess, json, re, math
from supabase import create_client, Client
import anthropic
import stripe
import httpx
import jwt as pyjwt
try:
    from google import genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

app = FastAPI(title="Growth Partner Edit Tool API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CLIENTS ──
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if (GEMINI_AVAILABLE and GEMINI_API_KEY) else None

STRIPE_PRICES = {
    "basic": os.environ.get("STRIPE_PRICE_BASIC", ""),
    "plus":  os.environ.get("STRIPE_PRICE_PLUS", ""),
    "gold":  os.environ.get("STRIPE_PRICE_GOLD", ""),
}

# ────────────────────────────────────────────
# AUTH HELPERS
# ────────────────────────────────────────────

class UserObj:
    def __init__(self, data: dict):
        self.id = data.get("sub")
        self.email = data.get("email")
        self.user_metadata = data.get("user_metadata", {})

def get_user_from_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = auth.split(" ")[1]
    try:
        user = supabase.auth.get_user(token)
        if user and user.user:
            return user.user
    except Exception as e:
        print(f"Supabase auth error: {e}")
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if jwt_secret:
        try:
            payload = pyjwt.decode(token, jwt_secret, algorithms=["HS256"], options={"verify_aud": False})
            return UserObj(payload)
        except Exception as e:
            print(f"JWT decode failed: {e}")
    raise HTTPException(status_code=401, detail="Invalid token")

def check_user_plan(user_id: str):
    result = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="User profile not found")
    profile = result.data
    plan = profile.get("plan", "trial")
    if plan == "trial":
        hours_used = profile.get("hours_used", 0)
        hours_limit = profile.get("hours_limit", 5)
        if hours_used >= hours_limit:
            raise HTTPException(status_code=402, detail="Trial hours exhausted. Please upgrade.")
        trial_end = profile.get("trial_end")
        if trial_end:
            from datetime import datetime, timezone
            if datetime.now(timezone.utc).isoformat() > trial_end:
                raise HTTPException(status_code=402, detail="Trial expired. Please upgrade.")
    return profile

def update_job(job_id: str, status: str):
    supabase.table("jobs").update({"status": status}).eq("id", job_id).execute()

# ────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────

@app.get("/test-gql")
async def test_gql():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://gql.twitch.tv/gql",
                json=[{"query": "query { currentUser { id } }"}],
                headers={"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"},
            )
            return {"status": r.status_code, "body": r.text[:200]}
    except Exception as e:
        return {"error": str(e)}


@app.post("/process-free")
async def process_free(request: Request, body: dict):
    """
    Free mode — Claude generates FFmpeg command directly from prompt.
    No predefined effects, Claude figures out the command itself.
    """
    user = get_user_from_token(request)
    upload_id = body.get("upload_id")
    prompt = body.get("prompt", "")
    if not upload_id or not prompt:
        raise HTTPException(status_code=400, detail="Missing upload_id or prompt")

    upload_dir = f"/tmp/uploads/{upload_id}"
    video_files = [f for f in os.listdir(upload_dir) if not f.startswith("chunk_") and not f.startswith("music")]
    if not video_files:
        raise HTTPException(status_code=404, detail="Video not found")
    video_path = f"{upload_dir}/{video_files[0]}"

    job_id = str(uuid.uuid4())
    supabase.table("jobs").insert({
        "id": job_id, "user_id": user.id, "status": "processing",
        "prompt": prompt, "upload_id": upload_id,
    }).execute()

    async def run_free_job():
        output_dir = f"/tmp/outputs/{job_id}"
        os.makedirs(output_dir, exist_ok=True)
        try:
            update_job(job_id, "analyzing")

            # Get video duration
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path
            ], capture_output=True, text=True, timeout=30)
            video_duration = float(probe.stdout.strip()) if probe.returncode == 0 else 60.0

            # Get transcript for context
            segments = []
            try:
                audio_path = f"{output_dir}/audio.wav"
                subprocess.run([
                    "ffmpeg", "-ss", "0", "-i", video_path, "-to", "300",
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", audio_path, "-y"
                ], capture_output=True, timeout=60)
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 10000:
                    segments = await transcribe_with_deepgram(audio_path, job_id)
            except:
                pass

            # Ask Claude to generate FFmpeg command
            from ai_logic import build_ai_prompt
            ai_prompt = build_ai_prompt(
                content_type="free",
                user_prompt=prompt,
                stream_context="",
                video_duration=video_duration,
                segments=segments,
            )
            ai_response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": ai_prompt}]
            )
            ai_text = ai_response.content[0].text.strip()
            if "```" in ai_text:
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"):
                    ai_text = ai_text[4:]
            result = json.loads(ai_text.strip())
            ffmpeg_args = result.get("ffmpeg_args", [])
            description = result.get("description", "")
            print(f"[JOB {job_id}] Free mode FULL command: {ffmpeg_args}")

            # Safety check — block shell injection, allow FFmpeg filter syntax
            if not ffmpeg_args or ffmpeg_args[0] != "ffmpeg":
                raise Exception("Invalid command — must start with ffmpeg")
            always_dangerous = ["&&", "||", "`", "$(", "rm ", "mv ", "cp ", "/etc/", "/bin/sh", "/bin/bash"]
            filter_flags = {"-filter_complex", "-vf", "-af", "-filter:v", "-filter:a"}
            prev_arg = ""
            for arg in ffmpeg_args:
                in_filter = prev_arg in filter_flags
                for d in always_dangerous:
                    if d in arg:
                        raise Exception(f"Unsafe command pattern: {d}")
                if ";" in arg and not in_filter:
                    raise Exception(f"Unsafe command pattern: ;")
                prev_arg = arg

            # Replace input.mp4 and output.mp4 with actual paths
            output_path = f"{output_dir}/output.mp4"
            final_args = []
            for arg in ffmpeg_args:
                if arg == "input.mp4":
                    final_args.append(video_path)
                elif arg == "output.mp4":
                    final_args.append(output_path)
                else:
                    final_args.append(arg)
            final_args.extend(["-y"])

            update_job(job_id, "editing")
            print(f"[JOB {job_id}] Running FULL: {final_args}")
            proc = subprocess.run(final_args, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0:
                print(f"[JOB {job_id}] FFmpeg FULL stderr: {proc.stderr}")
                raise Exception(f"FFmpeg error: {proc.stderr[-500:]}")

            update_job(job_id, "uploading")
            base_url = "growth-partner-edit-tool-production.up.railway.app"
            fname = "output.mp4"
            size_mb = os.path.getsize(output_path) / (1024 * 1024) if os.path.exists(output_path) else 0
            download_urls = [{"filename": fname, "url": f"https://{base_url}/download/{job_id}/{fname}", "size_mb": round(size_mb, 1), "duration": 0}]
            supabase.table("jobs").update({
                "status": "done",
                "result": json.dumps(download_urls),
                "description": description,
            }).eq("id", job_id).execute()
            print(f"[JOB {job_id}] Free mode done: {description}")

        except Exception as e:
            import traceback
            print(f"[JOB {job_id}] Free mode ERROR: {e}\n{traceback.format_exc()}")
            supabase.table("jobs").update({"status": "error", "error": str(e)[:300]}).eq("id", job_id).execute()

    asyncio.create_task(run_free_job())
    return {"job_id": job_id}


@app.get("/auth/twitch")
async def twitch_oauth_start(request: Request, token: str = None, force: str = None):
    """Redirect user to Twitch OAuth page."""
    if token:
        try:
            payload = pyjwt.decode(token, options={"verify_signature": False})
            user_id = payload.get("sub")
        except:
            raise HTTPException(status_code=401, detail="Invalid token")
    else:
        user = get_user_from_token(request)
        user_id = user.id
    backend_url = os.environ.get("BACKEND_URL", "https://growth-partner-edit-tool-production.up.railway.app")
    redirect_uri = f"{backend_url}/auth/twitch/callback"
    scopes = "user:read:email"
    url = (
        f"https://id.twitch.tv/oauth2/authorize"
        f"?client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scopes}"
        f"&state={user_id}"
    )
    # Force re-login so user can switch accounts
    if force:
        url += "&force_verify=true"
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@app.get("/auth/twitch/callback")
async def twitch_oauth_callback(code: str = None, state: str = None, error: str = None):
    """Handle Twitch OAuth callback, save tokens to Supabase."""
    from fastapi.responses import HTMLResponse
    if error or not code:
        return HTMLResponse("<script>window.close();</script><p>Twitch connection failed.</p>")

    backend_url = os.environ.get("BACKEND_URL", "https://growth-partner-edit-tool-production.up.railway.app")
    redirect_uri = f"{backend_url}/auth/twitch/callback"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Exchange code for tokens
            r = await client.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            })
            if r.status_code != 200:
                return HTMLResponse(f"<script>window.close();</script><p>Token error: {r.text}</p>")
            token_data = r.json()
            access_token = token_data["access_token"]
            refresh_token = token_data["refresh_token"]

            # Get Twitch user info
            user_r = await client.get("https://api.twitch.tv/helix/users", headers={
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {access_token}",
            })
            twitch_user = user_r.json().get("data", [{}])[0]
            twitch_username = twitch_user.get("display_name", "")

        # Save to Supabase profiles
        user_id = state
        supabase.table("profiles").update({
            "twitch_access_token": access_token,
            "twitch_refresh_token": refresh_token,
            "twitch_username": twitch_username,
        }).eq("id", user_id).execute()

        return HTMLResponse(f"""
            <html><body>
            <p>✓ Twitch connected as <b>{twitch_username}</b>. You can close this window.</p>
            <script>
                if (window.opener) {{
                    window.opener.postMessage({{type: 'twitch_connected', username: '{twitch_username}'}}, '*');
                    setTimeout(() => window.close(), 1500);
                }}
            </script>
            </body></html>
        """)
    except Exception as e:
        return HTMLResponse(f"<script>window.close();</script><p>Error: {e}</p>")


async def get_twitch_user_token(user_id: str, job_id: str) -> str:
    """Get valid Twitch user token, refresh if needed."""
    try:
        profile = supabase.table("profiles").select(
            "twitch_access_token, twitch_refresh_token"
        ).eq("id", user_id).single().execute()
        data = profile.data
        if not data or not data.get("twitch_access_token"):
            return ""

        access_token = data["twitch_access_token"]
        refresh_token = data["twitch_refresh_token"]

        # Validate token
        async with httpx.AsyncClient(timeout=10) as client:
            val = await client.get("https://id.twitch.tv/oauth2/validate",
                                   headers={"Authorization": f"OAuth {access_token}"})
            if val.status_code == 200:
                return access_token

            # Token expired — refresh it
            print(f"[JOB {job_id}] Twitch token expired, refreshing...")
            r = await client.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
            if r.status_code != 200:
                print(f"[JOB {job_id}] Twitch refresh failed: {r.status_code}")
                return ""
            new_data = r.json()
            new_access = new_data["access_token"]
            new_refresh = new_data.get("refresh_token", refresh_token)
            supabase.table("profiles").update({
                "twitch_access_token": new_access,
                "twitch_refresh_token": new_refresh,
            }).eq("id", user_id).execute()
            print(f"[JOB {job_id}] Twitch token refreshed")
            return new_access
    except Exception as e:
        print(f"[JOB {job_id}] get_twitch_user_token error: {e}")
        return ""


@app.get("/")
def health():
    return {"status": "ok", "service": "Growth Partner Edit Tool API"}

@app.get("/download/{job_id}/{filename}")
async def download_file(job_id: str, filename: str):
    from fastapi.responses import FileResponse
    file_path = f"/tmp/outputs/{job_id}/{filename}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found. Please process the video again.")
    return FileResponse(path=file_path, filename=filename, media_type="video/mp4", headers={"Cache-Control": "no-cache"})

@app.on_event("startup")
async def startup_keepalive():
    async def keepalive():
        await asyncio.sleep(10)
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    await client.get("https://growth-partner-edit-tool-production.up.railway.app/", timeout=10)
                    print("Keepalive ping sent")
            except Exception as e:
                print(f"Keepalive error: {e}")
            await asyncio.sleep(240)
    asyncio.create_task(keepalive())

@app.post("/auth/profile")
async def create_profile(request: Request):
    try:
        user = get_user_from_token(request)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid token")
    from datetime import datetime, timezone, timedelta
    trial_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    try:
        existing = supabase.table("profiles").select("id").eq("id", user.id).execute()
        if existing.data:
            return {"message": "Profile already exists"}
    except: pass
    email = ""
    name = "there"
    try:
        email = user.email or ""
        if not email and hasattr(user, 'user_metadata') and user.user_metadata:
            email = user.user_metadata.get("email", "")
        if hasattr(user, 'user_metadata') and user.user_metadata:
            name = user.user_metadata.get("name", "there") or "there"
    except: pass
    try:
        supabase.table("profiles").insert({
            "id": user.id, "email": email, "plan": "trial",
            "trial_end": trial_end, "hours_used": 0, "hours_limit": 5,
        }).execute()
    except:
        return {"message": "Profile already exists or created"}
    asyncio.create_task(asyncio.to_thread(send_welcome_email, email, name))
    return {"message": "Profile created", "trial_end": trial_end}

@app.get("/auth/me")
async def get_profile(request: Request):
    user = get_user_from_token(request)
    result = supabase.table("profiles").select("*").eq("id", user.id).execute()
    if not result.data:
        from datetime import datetime, timezone, timedelta
        trial_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        email = user.email or (user.user_metadata.get("email") if user.user_metadata else "")
        name = user.user_metadata.get("name", "there") if user.user_metadata else "there"
        supabase.table("profiles").insert({
            "id": user.id, "email": email, "plan": "trial",
            "trial_end": trial_end, "hours_used": 0, "hours_limit": 5,
        }).execute()
        asyncio.create_task(asyncio.to_thread(send_welcome_email, email, name))
        result = supabase.table("profiles").select("*").eq("id", user.id).execute()
    return result.data[0] if result.data else {}

@app.post("/upload/music")
async def upload_music(request: Request, file: UploadFile = File(...), upload_id: str = Form(...)):
    user = get_user_from_token(request)
    music_dir = f"/tmp/uploads/{upload_id}"
    os.makedirs(music_dir, exist_ok=True)
    content = await file.read()
    ext = os.path.splitext(file.filename)[1].lower() or ".mp3"
    music_path = f"{music_dir}/music{ext}"
    with open(music_path, "wb") as f:
        f.write(content)
    print(f"Music uploaded: {music_path}, size: {len(content)/1024/1024:.1f}MB")
    return {"status": "uploaded", "music_path": music_path}

@app.post("/upload/chunk")
async def upload_chunk(
    request: Request, file: UploadFile = File(...), upload_id: str = Form(...),
    chunk_index: int = Form(...), total_chunks: int = Form(...),
):
    user = get_user_from_token(request)
    # Only check plan on first chunk — not every chunk
    if chunk_index == 0:
        check_user_plan(user.id)

    upload_dir = f"/tmp/uploads/{upload_id}"
    os.makedirs(upload_dir, exist_ok=True)
    chunk_path = f"{upload_dir}/chunk_{chunk_index:04d}"
    content = await file.read()

    # Write chunk in thread to not block event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: open(chunk_path, "wb").write(content))

    # Check if ALL chunks are now present — safe with parallel uploads
    def all_chunks_present():
        for i in range(total_chunks):
            if not os.path.exists(f"{upload_dir}/chunk_{i:04d}"):
                return False
        return True

    if await loop.run_in_executor(None, all_chunks_present):
        assembled_path = f"{upload_dir}/video{os.path.splitext(file.filename)[1]}"
        if not os.path.exists(assembled_path):
            def assemble():
                with open(assembled_path + ".tmp", "wb") as out:
                    for i in range(total_chunks):
                        chunk_file = f"{upload_dir}/chunk_{i:04d}"
                        with open(chunk_file, "rb") as cf:
                            out.write(cf.read())
                os.replace(assembled_path + ".tmp", assembled_path)
                for i in range(total_chunks):
                    try: os.remove(f"{upload_dir}/chunk_{i:04d}")
                    except: pass
            await loop.run_in_executor(None, assemble)
        return {"status": "assembled", "upload_id": upload_id, "path": assembled_path, "filename": file.filename}

    return {"status": "chunk_saved", "chunk_index": chunk_index}

@app.post("/process")
async def process_video(request: Request, body: dict):
    user = get_user_from_token(request)
    profile = check_user_plan(user.id)
    upload_id = body.get("upload_id")
    prompt = body.get("prompt", "")
    output_format = body.get("output_format", "mp4")
    quality = body.get("quality", "1080p")
    music_upload_id = body.get("music_upload_id")
    music_trim_start = body.get("music_trim_start")
    music_trim_end = body.get("music_trim_end")
    content_type = body.get("content_type", "video")  # "short_clip" | "video" | "stream"
    vod_url = body.get("vod_url", "")

    # Stream with VOD URL — no upload needed
    if content_type == "stream" and vod_url:
        if not prompt:
            raise HTTPException(status_code=400, detail="Missing prompt")
        job_id = str(uuid.uuid4())
        supabase.table("jobs").insert({
            "id": job_id, "user_id": user.id, "status": "processing",
            "prompt": prompt, "upload_id": job_id,
        }).execute()
        if profile.get("plan") == "trial":
            if profile.get("hours_used", 0) >= profile.get("hours_limit", 5):
                raise HTTPException(status_code=402, detail="trial_hours_exhausted")

        async def safe_run_vod():
            print(f"[SAFE_RUN {job_id}] VOD task started")
            try:
                await run_stream_vod(job_id, user.id, vod_url, prompt, output_format, quality)
                print(f"[SAFE_RUN {job_id}] VOD task completed")
            except Exception as e:
                import traceback
                print(f"[SAFE_RUN {job_id}] FATAL ERROR: {e}\n{traceback.format_exc()}")
                try:
                    supabase.table("jobs").update({"status": "error", "error": f"{str(e)[:200]}"}).eq("id", job_id).execute()
                except:
                    pass

        asyncio.create_task(safe_run_vod())
        return {"job_id": job_id}

    if not upload_id or not prompt:
        raise HTTPException(status_code=400, detail="Missing upload_id or prompt")
    upload_dir = f"/tmp/uploads/{upload_id}"
    video_files = [f for f in os.listdir(upload_dir) if not f.startswith("chunk_") and not f.startswith("music")]
    if not video_files:
        raise HTTPException(status_code=404, detail="Video not found. Upload first.")
    video_path = f"{upload_dir}/{video_files[0]}"
    job_id = str(uuid.uuid4())
    supabase.table("jobs").insert({
        "id": job_id, "user_id": user.id, "status": "processing",
        "prompt": prompt, "upload_id": upload_id,
    }).execute()
    if profile.get("plan") == "trial":
        if profile.get("hours_used", 0) >= profile.get("hours_limit", 5):
            raise HTTPException(status_code=402, detail="trial_hours_exhausted")
    music_path = None
    if music_upload_id:
        music_dir = f"/tmp/uploads/{music_upload_id}"
        if os.path.exists(music_dir):
            music_files = [f for f in os.listdir(music_dir) if f.startswith("music")]
            if music_files:
                music_path = f"{music_dir}/{music_files[0]}"
                print(f"Music file found: {music_path}")

    async def safe_run():
        print(f"[SAFE_RUN {job_id}] Task started")
        try:
            await run_processing(job_id, user.id, video_path, prompt, output_format, quality, music_path, music_trim_start, music_trim_end, content_type)
            print(f"[SAFE_RUN {job_id}] Task completed successfully")
        except Exception as e:
            import traceback
            print(f"[SAFE_RUN {job_id}] FATAL ERROR: {e}\n{traceback.format_exc()}")
            try:
                supabase.table("jobs").update({"status": "error", "error": f"{str(e)[:200]}"}).eq("id", job_id).execute()
            except: pass

    print(f"[PROCESS {job_id}] Creating background task...")
    asyncio.create_task(safe_run())
    return {"job_id": job_id, "status": "processing"}

@app.get("/jobs/pending")
async def get_pending_job(request: Request):
    user = get_user_from_token(request)
    result = supabase.table("jobs").select("*").eq("user_id", user.id)\
        .in_("status", ["processing", "transcribing", "analyzing", "editing", "uploading", "done"])\
        .order("created_at", desc=True).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="No pending jobs")
    return result.data[0]

@app.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    user = get_user_from_token(request)
    result = supabase.table("jobs").select("*").eq("id", job_id).eq("user_id", user.id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    return result.data

@app.post("/jobs/{job_id}/feedback")
async def submit_feedback(job_id: str, request: Request, body: dict):
    user = get_user_from_token(request)
    rating = body.get("rating")
    if rating not in ["good", "bad"]:
        raise HTTPException(status_code=400, detail="Rating must be 'good' or 'bad'")
    supabase.table("jobs").update({"feedback": rating}).eq("id", job_id).eq("user_id", user.id).execute()
    if rating == "good":
        job = supabase.table("jobs").select("prompt").eq("id", job_id).single().execute()
        if job.data:
            save_successful_prompt(job.data["prompt"], "unknown", user.id)
    return {"received": True}

@app.post("/billing/checkout")
async def create_checkout(request: Request, body: dict):
    user = get_user_from_token(request)
    plan = body.get("plan")
    if plan not in STRIPE_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan")
    price_id = STRIPE_PRICES[plan]
    if not price_id:
        raise HTTPException(status_code=400, detail="Plan not configured yet")

    # Get or create Stripe customer so portal works immediately after checkout
    profile = supabase.table("profiles").select("stripe_customer_id, email").eq("id", user.id).single().execute()
    customer_id = (profile.data or {}).get("stripe_customer_id")
    if not customer_id:
        email = (profile.data or {}).get("email", "")
        customer = stripe.Customer.create(email=email, metadata={"user_id": user.id})
        customer_id = customer.id
        supabase.table("profiles").update({"stripe_customer_id": customer_id}).eq("id", user.id).execute()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"], mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{os.environ.get('FRONTEND_URL', 'http://localhost:3000')}?payment=success",
        cancel_url=f"{os.environ.get('FRONTEND_URL', 'http://localhost:3000')}?payment=cancelled",
        metadata={"user_id": user.id, "plan": plan},
    )
    return {"checkout_url": session.url}

@app.post("/billing/portal")
async def billing_portal(request: Request):
    """Open Stripe customer portal for plan management."""
    user = get_user_from_token(request)
    profile = supabase.table("profiles").select("stripe_customer_id").eq("id", user.id).single().execute()
    customer_id = (profile.data or {}).get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=400, detail="No billing account found")
    frontend_url = os.environ.get("FRONTEND_URL", "https://growth-partner.agency")
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=frontend_url,
    )
    return {"url": session.url}


@app.post("/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, os.environ["STRIPE_WEBHOOK_SECRET"])
    except:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
        hours_map = {"basic": 20, "plus": 60, "gold": 999999}
        supabase.table("profiles").update({
            "plan": plan, "hours_limit": hours_map.get(plan, 20),
            "stripe_customer_id": session.get("customer"),
            "stripe_subscription_id": session.get("subscription"),
        }).eq("id", user_id).execute()
        profile = supabase.table("profiles").select("email").eq("id", user_id).single().execute()
        if profile.data:
            send_purchase_confirmation(profile.data["email"], plan)
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        supabase.table("profiles").update({"plan": "expired"}).eq("stripe_subscription_id", sub["id"]).execute()
    return {"received": True}

# ────────────────────────────────────────────
# CORE PROCESSING PIPELINE
# ────────────────────────────────────────────

async def transcribe_with_deepgram(audio_path: str, job_id: str) -> list:
    """Transcribe audio using Deepgram Nova-2. Returns list of {start, end, text, confidence} segments."""
    print(f"[JOB {job_id}] Starting Deepgram transcription...")
    if not DEEPGRAM_API_KEY:
        raise Exception("DEEPGRAM_API_KEY not set")

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=900.0)) as client:
        response = await client.post(
            "https://api.deepgram.com/v1/listen",
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "audio/wav",
            },
            params={
                "model": "nova-2",
                "detect_language": "true",
                "punctuate": "true",
                "utterances": "true",
                "utt_split": "0.8",
                "paragraphs": "true",
            },
            content=audio_data,
        )

    print(f"[JOB {job_id}] Deepgram response status: {response.status_code}")

    if response.status_code != 200:
        raise Exception(f"Deepgram error {response.status_code}: {response.text[:300]}")

    data = response.json()
    segments = []

    # Parse utterances — these give natural speech segments with timestamps
    utterances = data.get("results", {}).get("utterances", [])
    if utterances:
        for utt in utterances:
            segments.append({
                "start": utt["start"],
                "end": utt["end"],
                "text": utt["transcript"],
                "confidence": utt.get("confidence", 0.9),
            })
    else:
        # Fallback: parse words into ~10s chunks
        words = data.get("results", {}).get("channels", [{}])[0]\
                    .get("alternatives", [{}])[0].get("words", [])
        chunk_text = []
        chunk_start = 0
        for word in words:
            if not chunk_text:
                chunk_start = word["start"]
            chunk_text.append(word["word"])
            if word["end"] - chunk_start >= 10 or word == words[-1]:
                segments.append({
                    "start": chunk_start,
                    "end": word["end"],
                    "text": " ".join(chunk_text),
                    "confidence": word.get("confidence", 0.9),
                })
                chunk_text = []

    print(f"[JOB {job_id}] Deepgram done: {len(segments)} segments")
    return segments


def analyze_audio_energy(video_path: str, job_id: str, interval: float = 1.0) -> list:
    """
    Analyze per-second loudness via ebur128.
    Peak detection uses DELTA (sudden loudness spike vs rolling average) —
    this catches reactions/laughs/panic but ignores background music and loud talking.
    """
    import re as _re
    print(f"[JOB {job_id}] Analyzing audio energy (ebur128 delta)...")

    result = subprocess.run([
        "ffmpeg", "-i", video_path,
        "-af", "ebur128=peak=true",
        "-f", "null", "-"
    ], capture_output=True, text=True, timeout=1800)

    # Parse: [Parsed_ebur128_0 @ 0x...] t: 1.23  TARGET:-23 LUFS    M: -18.4 S: ...
    rms_by_second = {}
    for line in result.stderr.split("\n"):
        if "Parsed_ebur128" not in line:
            continue
        mt = _re.search(r"t:\s*([\d.]+)", line)
        mm = _re.search(r"\bM:\s*([\-\d.inf]+)", line)
        if mt and mm:
            try:
                t = float(mt.group(1))
                lufs_str = mm.group(1)
                lufs = -120.0 if "inf" in lufs_str else float(lufs_str)
                second = int(t)
                if second not in rms_by_second or lufs > rms_by_second[second]:
                    rms_by_second[second] = lufs
            except:
                pass

    if not rms_by_second:
        print(f"[JOB {job_id}] ebur128 parse failed, falling back to silencedetect")
        return _analyze_audio_energy_fallback(video_path, job_id)

    print(f"[JOB {job_id}] ebur128 parsed {len(rms_by_second)} seconds of loudness data")

    try:
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ], capture_output=True, text=True, timeout=30)
        total_duration = float(probe.stdout.strip())
    except:
        total_duration = float(max(rms_by_second.keys()) + 1)

    all_rms = []
    for s in range(int(total_duration) + 1):
        all_rms.append(rms_by_second.get(s, -120.0))

    # Delta-based spike detection:
    # For each second, compute how much LOUDER it is vs rolling 5s average before it.
    # Large delta = sudden reaction/laugh/panic. Constant loudness (music, talking) = low delta.
    WINDOW = 5
    deltas = []
    for s in range(len(all_rms)):
        cur = all_rms[s]
        if cur < -70:
            deltas.append(0.0)
            continue
        window_vals = [all_rms[i] for i in range(max(0, s - WINDOW), s) if all_rms[i] > -70]
        if len(window_vals) < 2:
            deltas.append(0.0)
            continue
        avg_before = sum(window_vals) / len(window_vals)
        deltas.append(max(0.0, cur - avg_before))

    # Top 2% of delta seconds = peaks, BUT only if absolute loudness is also high enough.
    # This filters out whisper→normal transitions (big delta, low absolute RMS).
    # Real reactions/laughs/panic are BOTH a sudden spike AND loud (> -20 LUFS).
    MIN_PEAK_RMS = -20.0  # must be at least this loud to count as a peak
    nonzero = [(s, d) for s, d in enumerate(deltas) if d > 0 and all_rms[s] > MIN_PEAK_RMS]
    if not nonzero:
        # Relax threshold if nothing qualifies
        nonzero = [(s, d) for s, d in enumerate(deltas) if d > 0 and all_rms[s] > -30.0]
    if nonzero:
        sorted_by_delta = sorted(nonzero, key=lambda x: x[1], reverse=True)
        top_n = max(10, int(len(nonzero) * 0.02))
        peak_seconds = set(s for s, _ in sorted_by_delta[:top_n])
        mean_delta = sum(d for _, d in nonzero) / len(nonzero)
        max_delta = sorted_by_delta[0][1]
        print(f"[JOB {job_id}] Delta peaks: {len(peak_seconds)} spikes | mean_delta={mean_delta:.1f}dB max_delta={max_delta:.1f}dB")
    else:
        peak_seconds = set()
        print(f"[JOB {job_id}] No delta peaks found, falling back to absolute loudness")
        # Fallback to absolute top 2%
        active = [(s, r) for s, r in enumerate(all_rms) if r > -70]
        if active:
            top_n = max(10, int(len(active) * 0.02))
            peak_seconds = set(s for s, _ in sorted(active, key=lambda x: x[1], reverse=True)[:top_n])

    energy_map = []
    for s, rms in enumerate(all_rms):
        is_silent = rms < -70
        energy_map.append({
            "time": float(s),
            "rms": rms,
            "is_silent": is_silent,
            "is_active": not is_silent,
            "is_peak": s in peak_seconds,
            "delta": deltas[s] if s < len(deltas) else 0.0,
        })

    return energy_map


def _analyze_audio_energy_fallback(video_path: str, job_id: str) -> list:
    """Fallback: binary silence/active detection via silencedetect."""
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ], capture_output=True, text=True, timeout=30)
        total_duration = float(probe.stdout.strip())
    except:
        return []

    result = subprocess.run([
        "ffmpeg", "-i", video_path,
        "-af", "silencedetect=noise=-40dB:d=2",
        "-f", "null", "-"
    ], capture_output=True, text=True, timeout=1800)

    silence_periods = []
    for line in result.stderr.split("\n"):
        if "silence_start" in line:
            try:
                t = float(line.split("silence_start:")[1].strip().split()[0])
                silence_periods.append([t, None])
            except: pass
        elif "silence_end" in line and silence_periods and silence_periods[-1][1] is None:
            try:
                t = float(line.split("silence_end:")[1].strip().split("|")[0].strip())
                silence_periods[-1][1] = t
            except: pass

    energy_map = []
    for i in range(int(total_duration) + 1):
        t = float(i)
        is_silent = any((s[0] <= t + 1 and (s[1] or total_duration) >= t) for s in silence_periods)
        energy_map.append({
            "time": t, "rms": -50.0 if is_silent else -20.0,
            "is_silent": is_silent, "is_active": not is_silent, "is_peak": False,
        })
    print(f"[JOB {job_id}] Fallback energy map done: {len(energy_map)} seconds")
    return energy_map


def _cluster_peaks(energy_map: list, max_clusters: int = 20) -> list:
    """Cluster is_peak seconds (within 20s = one event), sorted by delta desc."""
    peak_entries = sorted(
        [(e["time"], e.get("delta", 0.0), e["rms"]) for e in energy_map if e.get("is_peak", False)],
        key=lambda x: x[0]
    )
    clusters = []
    if peak_entries:
        c_times = [peak_entries[0][0]]
        c_delta = peak_entries[0][1]
        c_rms = peak_entries[0][2]
        for t, delta, rms in peak_entries[1:]:
            if t - c_times[-1] <= 20:
                c_times.append(t)
                c_delta = max(c_delta, delta)
                c_rms = max(c_rms, rms)
            else:
                clusters.append((sum(c_times) / len(c_times), c_delta, c_rms))
                c_times = [t]
                c_delta = delta
                c_rms = rms
        clusters.append((sum(c_times) / len(c_times), c_delta, c_rms))
    clusters.sort(key=lambda x: x[1], reverse=True)
    return clusters[:max_clusters]


async def analyze_with_gemini(video_path: str, user_prompt: str, num_clips: int, video_duration: float, job_id: str) -> str:
    """
    Use Gemini to find best moments in video.
    Returns candidates string in same format as build_peak_candidates.
    Falls back to empty string if Gemini unavailable or fails.
    """
    if not GEMINI_AVAILABLE or not gemini_client:
        print(f"[JOB {job_id}] Gemini not available, using audio analysis")
        return ""

    print(f"[JOB {job_id}] Transcoding video to 360p for Gemini...")

    # Transcode to 360p to reduce token count
    low_res_path = video_path.replace(".mp4", "_360p.mp4").replace(".mov", "_360p.mp4")
    transcode_cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", "scale=-2:360",
        "-c:v", "libx264", "-crf", "28", "-preset", "fast",
        "-c:a", "aac", "-b:a", "64k",
        "-y", low_res_path
    ]
    try:
        subprocess.run(transcode_cmd, capture_output=True, timeout=600, check=True)
        file_size_mb = os.path.getsize(low_res_path) / (1024 * 1024)
        print(f"[JOB {job_id}] 360p file size: {file_size_mb:.1f}MB")
    except Exception as e:
        print(f"[JOB {job_id}] Transcode failed: {e}, using original")
        low_res_path = video_path

    try:
        print(f"[JOB {job_id}] Uploading video to Gemini...")
        loop = asyncio.get_event_loop()

        def upload_and_analyze():
            import time

            # Delete any existing files to free up storage quota
            try:
                existing = gemini_client.files.list()
                for f in existing:
                    try:
                        gemini_client.files.delete(name=f.name)
                    except:
                        pass
                print(f"[JOB {job_id}] Cleaned up existing Gemini files")
            except:
                pass

            # Upload file using new SDK
            print(f"[JOB {job_id}] Uploading to Gemini...")
            with open(low_res_path, "rb") as f:
                video_file = gemini_client.files.upload(
                    file=f,
                    config={"mime_type": "video/mp4"}
                )

            try:
                # Wait for processing
                while video_file.state.name == "PROCESSING":
                    time.sleep(5)
                    video_file = gemini_client.files.get(name=video_file.name)

                if video_file.state.name != "ACTIVE":
                    raise Exception(f"Gemini file not active: {video_file.state.name}")

                print(f"[JOB {job_id}] Gemini video ready, analyzing...")

                gemini_prompt = f"""Analyze this video and find the {num_clips} best moments for: "{user_prompt}"

You are looking for: genuine reactions, funny fails, hype moments, emotional peaks, anything that would perform well on TikTok/YouTube Shorts.

For each moment return ONLY a JSON array, no other text:
[
  {{"peak_time": 45.0, "start": 30.0, "end": 75.0, "label": "funny_fail_moment", "reason": "brief reason"}},
  ...
]

Rules:
- peak_time is the exact second of the best moment
- start = peak_time - 15 seconds (minimum 0)
- end = peak_time + 35 seconds (maximum {video_duration:.0f})
- Each clip must be 20-50 seconds (end - start)
- label must be 2-4 words with underscores
- Return EXACTLY {num_clips} moments
- Timestamps must be between 0 and {video_duration:.0f}"""

                response = gemini_client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=[video_file, gemini_prompt]
                )
                return response.text
            finally:
                # Always delete file after analysis to free storage
                try:
                    gemini_client.files.delete(name=video_file.name)
                    print(f"[JOB {job_id}] Gemini file deleted")
                except:
                    pass

        gemini_text = await loop.run_in_executor(None, upload_and_analyze)
        print(f"[JOB {job_id}] Gemini response received")

        # Parse JSON from response
        gemini_text = gemini_text.strip()
        if "```" in gemini_text:
            gemini_text = gemini_text.split("```")[1]
            if gemini_text.startswith("json"):
                gemini_text = gemini_text[4:]
        gemini_text = gemini_text.strip()

        moments = json.loads(gemini_text)

        # Format as candidates string (same format as build_peak_candidates)
        lines = [
            f"CLIP CANDIDATES — {len(moments)} moments selected by Gemini video analysis.",
            "Copy start/end EXACTLY. Do NOT invent timestamps outside these candidates.\n"
        ]
        for i, m in enumerate(moments):
            start = max(0.0, float(m.get("start", m["peak_time"] - 15)))
            end = min(video_duration, float(m.get("end", m["peak_time"] + 35)))
            pt = float(m["peak_time"])
            mm, ss = int(pt // 60), int(pt % 60)
            lines.append(
                f"CANDIDATE {i+1}: peak@{mm}:{ss:02d} start={start:.0f} end={end:.0f}\n"
                f"  REASON: {m.get('reason', '')}\n"
                f"  LABEL: {m.get('label', 'moment')}"
            )

        print(f"[JOB {job_id}] Gemini found {len(moments)} candidates")
        return "\n".join(lines)

    except Exception as e:
        print(f"[JOB {job_id}] Gemini analysis failed: {e}, falling back to audio analysis")
        return ""
    finally:
        # Clean up 360p file
        if low_res_path != video_path and os.path.exists(low_res_path):
            try:
                os.remove(low_res_path)
            except:
                pass


def build_peak_candidates(segments: list, energy_map: list, video_duration: float, job_id: str) -> str:
    """
    Build pre-selected clip candidates from top audio spikes.
    Sorted by delta strength — Python decides order, Claude only labels.
    """
    top_clusters = _cluster_peaks(energy_map, max_clusters=15)
    if not top_clusters:
        print(f"[JOB {job_id}] No peaks, using transcript-only selection")
        return ""

    by_time = sorted(top_clusters, key=lambda x: x[0])
    ranked = sorted(top_clusters, key=lambda x: x[1], reverse=True)
    PRE, POST = 15, 35

    lines = [
        f"CLIP CANDIDATES — {len(by_time)} moments pre-selected by audio spike analysis.",
        "Copy start/end EXACTLY. Do NOT invent timestamps outside these candidates.\n"
    ]
    rank_labels = [f"{int(t//60)}:{int(t%60):02d}" for t, _, _ in ranked]
    lines.append(f"RANKED BY SPIKE STRENGTH (pick from top): {', '.join(rank_labels)}\n")

    for i, (peak_t, delta, rms) in enumerate(by_time):
        start = max(0.0, peak_t - PRE)
        end = min(video_duration, peak_t + POST)
        rank = next(j+1 for j, (t, _, _) in enumerate(ranked) if abs(t - peak_t) < 1)
        window_segs = [s for s in segments if s["start"] < end and s["end"] > start]
        snippet = " ".join(s["text"] for s in window_segs)[:200]
        m, s = int(peak_t // 60), int(peak_t % 60)
        lines.append(
            f"CANDIDATE {i+1} [rank#{rank} spike={delta:.1f}dB]: "
            f"peak@{m}:{s:02d} start={start:.0f} end={end:.0f}\n"
            f"  TRANSCRIPT: {snippet if snippet else '(no speech)'}"
        )

    print(f"[JOB {job_id}] Peak candidates: {len(by_time)} built")
    return "\n".join(lines)


def build_stream_context(segments: list, energy_map: list, video_duration: float, job_id: str) -> str:
    """
    Build context for Claude: 2-minute blocks with transcript + peak timestamps.
    """
    BLOCK_SIZE = 120  # 2 minutes per block
    num_blocks = math.ceil(video_duration / BLOCK_SIZE)

    # Cluster peak seconds (within 20s = one event), keep top 15 by loudness
    peak_entries = sorted(
        [(e["time"], e["rms"]) for e in energy_map if e.get("is_peak", False)],
        key=lambda x: x[0]
    )
    clusters = []
    if peak_entries:
        c_times = [peak_entries[0][0]]
        c_max = peak_entries[0][1]
        for t, rms in peak_entries[1:]:
            if t - c_times[-1] <= 20:
                c_times.append(t)
                c_max = max(c_max, rms)
            else:
                clusters.append((sum(c_times) / len(c_times), c_max))
                c_times = [t]
                c_max = rms
        clusters.append((sum(c_times) / len(c_times), c_max))

    # Sort by loudness, keep top 15, then re-sort by time for display
    clusters.sort(key=lambda x: x[1], reverse=True)
    top_clusters = clusters[:15]
    top_cluster_times = sorted(t for t, _ in top_clusters)

    blocks = []
    for i in range(num_blocks):
        block_start = i * BLOCK_SIZE
        block_end = min((i + 1) * BLOCK_SIZE, video_duration)

        block_segments = [s for s in segments if s["start"] < block_end and s["end"] > block_start]
        block_text = " ".join(s["text"] for s in block_segments)

        block_energy = [e for e in energy_map if e["time"] >= block_start and e["time"] < block_end]
        if block_energy:
            active_pct = sum(1 for e in block_energy if e["is_active"]) / len(block_energy) * 100
            active_vals = [e["rms"] for e in block_energy if e["is_active"] and e["rms"] > -80]
            avg_rms = sum(active_vals) / len(active_vals) if active_vals else -40
        else:
            active_pct = 50
            avg_rms = -40

        block_peaks = [t for t in top_cluster_times if block_start <= t < block_end]
        peaks_str = ""
        if block_peaks:
            labels = [f"{int(t//60)}:{int(t%60):02d}" for t in block_peaks]
            peaks_str = f" | \u26a1PEAKS@{', '.join(labels)}"

        sm = int(block_start // 60); ss = int(block_start % 60)
        em = int(block_end // 60);   es = int(block_end % 60)

        blocks.append(
            f"[{sm}:{ss:02d}-{em}:{es:02d}] "
            f"ENERGY:{active_pct:.0f}% avg:{avg_rms:.0f}LUFS"
            f"{peaks_str} | "
            f"SPEECH: {block_text[:300] if block_text else '(silent)'}"
        )

    # Global peak summary at top
    if top_cluster_times:
        labels = [f"{int(t//60)}:{int(t%60):02d}" for t in top_cluster_times]
        header = (
            f"TOP AUDIO PEAKS ({len(top_cluster_times)} loudest moments):\n"
            f"Timestamps: {', '.join(labels)}\n"
            f"BUILD YOUR CLIPS AROUND THESE. They mark reactions, hype, laughs.\n\n"
        )
    else:
        header = "(no audio peaks detected — using transcript only)\n\n"

    print(f"[JOB {job_id}] Stream context: {len(clusters)} clusters, top {len(top_clusters)} to Claude")
    return header + "\n".join(blocks)


def get_music_volume(prompt: str) -> float:
    prompt_lower = prompt.lower()
    if any(w in prompt_lower for w in ["quietly", "quiet", "background", "soft", "subtle", "low", "potichu", "pozadi"]):
        return 0.15
    if any(w in prompt_lower for w in ["loud", "strong", "prominent", "full", "nahlas", "hlasitě"]):
        return 0.8
    match = re.search(r'(\d+)\s*%', prompt_lower)
    if match:
        return min(1.0, int(match.group(1)) / 100)
    return 0.2


def generate_srt(transcript: list, srt_path: str, start: float, end: float):
    with open(srt_path, "w") as f:
        idx = 1
        for seg in transcript:
            if seg["end"] < start or seg["start"] > end:
                continue
            s = max(0, seg["start"] - start)
            e = min(end - start, seg["end"] - start)
            f.write(f"{idx}\n{format_srt_time(s)} --> {format_srt_time(e)}\n{seg['text'].strip()}\n\n")
            idx += 1


def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_effects_filter(effects: list, base_filter: str = "", clip_duration: float = 0) -> str:
    """Build FFmpeg vf filter string for visual effects."""
    filters = [base_filter] if base_filter else []
    for effect in effects:
        if effect == "cinematic":
            # Warm color grade + slight contrast
            filters.append("eq=contrast=1.1:brightness=0.02:saturation=1.2,vignette=PI/5")
        elif effect == "zoom":
            # Ken Burns slow zoom in
            filters.append("scale=iw*1.3:ih*1.3,crop=iw/1.3:ih/1.3")
        elif effect == "fade":
            filters.append("fade=t=in:st=0:d=1.5")
            if clip_duration > 3:
                filters.append(f"fade=t=out:st={max(0, clip_duration - 1.5):.2f}:d=1.5")
        elif effect == "vignette":
            filters.append("vignette=PI/4")
        elif effect == "sharpen":
            filters.append("unsharp=5:5:2.0:5:5:0.0")
    return ",".join(f for f in filters if f)


async def download_chat(platform: str, vod_url: str, vod_id: str, video_duration: float, job_id: str, user_id: str = "") -> list:
    """
    Download chat replay for a VOD. Returns list of {t, text, author} dicts.
    Twitch: uses TwitchDownloaderCLI or API
    Kick: uses Kick API
    YouTube: uses yt-dlp --write-comments
    """
    messages = []

    if platform == "twitch":
        if not vod_id:
            print(f"[JOB {job_id}] Twitch: no VOD ID")
            return []
        try:
            # Get user OAuth token (supports sub-only VODs)
            access_token = await get_twitch_user_token(user_id, job_id)
            if not access_token:
                print(f"[JOB {job_id}] No Twitch user token, using app token...")
                async with httpx.AsyncClient(timeout=15) as c:
                    tr = await c.post("https://id.twitch.tv/oauth2/token", data={
                        "client_id": TWITCH_CLIENT_ID,
                        "client_secret": TWITCH_CLIENT_SECRET,
                        "grant_type": "client_credentials",
                    })
                    access_token = tr.json().get("access_token", "") if tr.status_code == 200 else ""
            if not access_token:
                print(f"[JOB {job_id}] No token available, skipping chat")
                return []
            print(f"[JOB {job_id}] Twitch token OK, fetching GQL chat...")
            async with httpx.AsyncClient(timeout=30) as client:

                gql_cursor = None
                for page in range(500):
                    if gql_cursor:
                        variables = {"videoID": vod_id, "cursor": gql_cursor}
                    else:
                        variables = {"videoID": vod_id, "contentOffsetSeconds": 0}

                    r = await client.post(
                        "https://gql.twitch.tv/gql",
                        json=[{
                            "operationName": "VideoCommentsByOffsetOrCursor",
                            "variables": variables,
                            "extensions": {"persistedQuery": {
                                "version": 1,
                                "sha256Hash": "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
                            }}
                        }],
                        headers={
                            "Client-ID": TWITCH_CLIENT_ID,
                            "Authorization": f"Bearer {access_token}",
                        },
                    )
                    if r.status_code != 200:
                        print(f"[JOB {job_id}] GQL status: {r.status_code}")
                        break
                    try:
                        data = r.json()
                        video_data = (data[0] or {}).get("data", {})
                        if not video_data or not video_data.get("video"):
                            print(f"[JOB {job_id}] GQL no video data: {str(data[0])[:200]}")
                            break
                        comments = video_data["video"].get("comments") or {}
                        edges = comments.get("edges") or []
                        for edge in edges:
                            node = edge.get("node") or {}
                            t = node.get("contentOffsetSeconds", 0)
                            fragments = (node.get("message") or {}).get("fragments") or []
                            text = "".join(f.get("text", "") for f in fragments)
                            author = (node.get("commenter") or {}).get("displayName", "")
                            if text:
                                messages.append({"t": float(t), "text": text, "author": author})
                        page_info = comments.get("pageInfo") or {}
                        if not page_info.get("hasNextPage") or not edges:
                            break
                        gql_cursor = edges[-1].get("cursor") if edges else None
                        if not gql_cursor:
                            break
                        if page % 50 == 0:
                            print(f"[JOB {job_id}] GQL page {page}, {len(messages)} messages...")
                    except Exception as parse_err:
                        print(f"[JOB {job_id}] GQL parse error: {parse_err}")
                        break
            print(f"[JOB {job_id}] Twitch chat: {len(messages)} messages")
        except Exception as e:
            print(f"[JOB {job_id}] Twitch chat error: {e}")
    elif platform == "kick":
        # Kick VOD chat via API
        # Extract video ID from URL: kick.com/username/videos/VIDEO_ID
        import re as _re
        kick_match = _re.search(r'/videos/(\w+)', vod_url)
        if not kick_match:
            # Try clip format
            kick_match = _re.search(r'fa(\w+)', vod_url)
        kick_vod_id = kick_match.group(1) if kick_match else vod_id

        try:
            cursor = 0
            max_iters = 200
            async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as client:
                for _ in range(max_iters):
                    r = await client.get(
                        f"https://kick.com/api/v2/videos/{kick_vod_id}/messages",
                        params={"cursor": cursor}
                    )
                    if r.status_code != 200:
                        print(f"[JOB {job_id}] Kick chat API: {r.status_code} — trying legacy endpoint")
                        # Try legacy endpoint
                        r = await client.get(
                            f"https://kick.com/api/v1/video/{kick_vod_id}/messages",
                            params={"start_time": cursor}
                        )
                        if r.status_code != 200:
                            break
                    data = r.json()
                    msgs = data.get("messages") or data.get("data") or []
                    if not msgs:
                        break
                    for msg in msgs:
                        t = msg.get("created_at") or msg.get("offset") or 0
                        # created_at may be ISO or seconds offset
                        if isinstance(t, str) and "T" in t:
                            # ISO — skip for now, needs VOD start time
                            t = 0
                        text = msg.get("content") or msg.get("message") or ""
                        author = (msg.get("sender") or {}).get("username") or msg.get("author") or ""
                        if text and t:
                            messages.append({"t": float(t), "text": text, "author": author})
                    next_cursor = data.get("next_cursor") or data.get("cursor")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
            print(f"[JOB {job_id}] Kick chat: {len(messages)} messages")
        except Exception as e:
            print(f"[JOB {job_id}] Kick chat error: {e}")

    else:  # YouTube
        # YouTube live chat via yt-dlp
        try:
            loop = asyncio.get_event_loop()
            def fetch_yt_chat():
                import tempfile, os as _os
                with tempfile.TemporaryDirectory() as tmp:
                    cmd = [
                        "yt-dlp", "--skip-download",
                        "--write-subs", "--write-auto-subs",
                        "--sub-format", "json3",
                        "--sub-langs", "live_chat",
                        "-o", f"{tmp}/chat",
                        vod_url
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=300)
                    msgs = []
                    for f in _os.listdir(tmp):
                        if f.endswith(".json3") or f.endswith(".json"):
                            try:
                                with open(f"{tmp}/{f}") as fh:
                                    raw = json.load(fh)
                                events = raw.get("events", [])
                                for ev in events:
                                    t_ms = ev.get("tStartMs", 0)
                                    segs = ev.get("segs", [])
                                    text = "".join(s.get("utf8", "") for s in segs).strip()
                                    if text:
                                        msgs.append({"t": float(t_ms) / 1000, "text": text, "author": ""})
                            except:
                                pass
                    return msgs
            messages = await loop.run_in_executor(None, fetch_yt_chat)
            print(f"[JOB {job_id}] YouTube chat: {len(messages)} messages")
        except Exception as e:
            print(f"[JOB {job_id}] YouTube chat error: {e}")

    return messages


async def run_stream_vod(job_id: str, user_id: str, vod_url: str, prompt: str, output_format: str, quality: str):
    """Process a stream VOD from URL — downloads chat, finds best moments via Claude, renders clips."""
    import re as _re

    output_dir = f"/tmp/outputs/{job_id}"
    os.makedirs(output_dir, exist_ok=True)

    try:
        update_job(job_id, "transcribing")

        # Detect platform
        if "twitch.tv" in vod_url:
            platform = "twitch"
        elif "kick.com" in vod_url:
            platform = "kick"
        else:
            platform = "youtube"

        print(f"[JOB {job_id}] Platform: {platform} | URL: {vod_url}")

        # Get video duration via yt-dlp
        info_cmd = ["yt-dlp", "--dump-json", "--no-download", vod_url]
        info_result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)
        video_duration = 3600.0  # default 1h
        vod_id = None
        try:
            info = json.loads(info_result.stdout)
            video_duration = float(info.get("duration", 3600))
            vod_id = str(info.get("id", ""))
            print(f"[JOB {job_id}] VOD duration: {video_duration:.0f}s | ID: {vod_id}")
        except:
            pass

        # Download chat — platform specific
        print(f"[JOB {job_id}] Downloading chat...")
        chat_data = await download_chat(platform, vod_url, vod_id, video_duration, job_id, user_id)

        update_job(job_id, "analyzing")

        # Build chat context for Claude
        chat_context = build_chat_context(chat_data, video_duration, job_id)

        # Build AI prompt with chat context
        from ai_logic import build_ai_prompt
        p_lower = prompt.lower()
        num_match = _re.search(r'(\d+)\s*(clip|moment|video|highlight|funny|part)', p_lower)
        num_clips = int(num_match.group(1)) if num_match else 3

        ai_prompt = build_ai_prompt(
            content_type="stream",
            user_prompt=prompt,
            stream_context=chat_context,
            peak_candidates="",
            video_duration=video_duration,
            segments=[],
            example_prompts="",
        )

        ai_response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": ai_prompt}]
        )
        ai_text = ai_response.content[0].text.strip()
        if "```" in ai_text:
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        instructions = json.loads(ai_text.strip())

        clips = instructions.get("clips", [])
        print(f"[JOB {job_id}] Claude selected {len(clips)} clips from chat analysis")

        update_job(job_id, "editing")

        # Download only selected segments via yt-dlp
        output_paths = []
        for i, clip in enumerate(clips):
            start = float(clip.get("start", 0))
            end = float(clip.get("end", start + 60))
            label = clip.get("label", f"clip_{i+1}")
            clip_path = f"{output_dir}/{label}_{i+1}.{output_format}"

            dl_cmd = [
                "yt-dlp",
                "--download-sections", f"*{int(start)}-{int(end)}",
                "--force-keyframes-at-cuts",
                "-o", clip_path,
                "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                "--merge-output-format", output_format,
                vod_url
            ]
            print(f"[JOB {job_id}] Downloading clip {i+1}: {start:.0f}s-{end:.0f}s")
            dl_result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
            if dl_result.returncode == 0 and os.path.exists(clip_path):
                output_paths.append(clip_path)
                print(f"[JOB {job_id}] Clip {i+1} downloaded: {clip_path}")
            else:
                print(f"[JOB {job_id}] Clip {i+1} download failed: {dl_result.stderr[:200]}")

        update_job(job_id, "uploading")

        # Upload to Supabase storage
        base_url = "growth-partner-edit-tool-production.up.railway.app"
        download_urls = []
        for path in output_paths:
            if os.path.exists(path):
                fname = os.path.basename(path)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                download_urls.append({
                    "filename": fname,
                    "url": f"https://{base_url}/download/{job_id}/{fname}",
                    "size_mb": round(size_mb, 1),
                    "duration": 0,
                })

        supabase.table("jobs").update({
            "status": "done",
            "result": json.dumps(download_urls),
            "description": instructions.get("description", ""),
        }).eq("id", job_id).execute()
        print(f"[JOB {job_id}] VOD job done: {len(download_urls)} clips")

    except Exception as e:
        import traceback
        print(f"[JOB {job_id}] VOD ERROR: {e}\n{traceback.format_exc()}")
        supabase.table("jobs").update({"status": "error", "error": str(e)[:200]}).eq("id", job_id).execute()



def build_chat_context(chat_data: list, video_duration: float, job_id: str) -> str:
    """
    Build context from chat messages for Claude.
    Groups messages by 2-minute blocks, highlights reaction peaks.
    """
    if not chat_data:
        return "(no chat data available — select moments based on timing)"

    # Normalize chat messages — different platforms have different formats
    messages = []
    for msg in chat_data:
        # yt-dlp comment format
        t = msg.get("timestamp") or msg.get("time_in_seconds") or msg.get("offset")
        text = msg.get("text") or msg.get("message") or msg.get("body") or ""
        author = msg.get("author") or msg.get("author_name") or ""
        if t is not None and text:
            try:
                messages.append({"t": float(t), "text": str(text), "author": str(author)})
            except:
                pass

    if not messages:
        return "(chat data found but could not parse — check format)"

    messages.sort(key=lambda x: x["t"])
    print(f"[JOB {job_id}] Parsed {len(messages)} chat messages")

    # Reaction keywords that signal good moments
    REACTION_WORDS = {"lol", "lmao", "kekw", "omg", "wow", "wtf", "pog", "pogchamp",
                      "haha", "lul", "lulw", "😂", "💀", "🤣", "😭", "pls", "noo",
                      "nooo", "lets go", "lets gooo", "clutch", "insane", "what", "bro"}

    BLOCK = 120  # 2 minute blocks
    num_blocks = math.ceil(video_duration / BLOCK)
    blocks = []

    for i in range(num_blocks):
        t_start = i * BLOCK
        t_end = min((i + 1) * BLOCK, video_duration)
        block_msgs = [m for m in messages if t_start <= m["t"] < t_end]
        if not block_msgs:
            continue

        count = len(block_msgs)
        reactions = sum(1 for m in block_msgs if any(w in m["text"].lower() for w in REACTION_WORDS))
        reaction_pct = int(reactions / count * 100) if count else 0
        sample = " | ".join(m["text"][:40] for m in block_msgs[:4])

        sm, ss = int(t_start // 60), int(t_start % 60)
        em, es = int(t_end // 60), int(t_end % 60)

        peak_flag = " ⚡PEAK" if reaction_pct >= 30 or count >= 20 else ""
        blocks.append(
            f"[{sm}:{ss:02d}-{em}:{es:02d}] msgs:{count} reactions:{reaction_pct}%{peak_flag} | {sample}"
        )

    # Find top reaction blocks
    peak_blocks = [b for b in blocks if "⚡PEAK" in b]
    header = f"CHAT ANALYSIS — {len(messages)} messages total\n"
    if peak_blocks:
        header += f"HIGH ACTIVITY BLOCKS (⚡PEAK = many reactions/messages): {len(peak_blocks)} found\nBUILD CLIPS AROUND PEAK BLOCKS.\n\n"
    else:
        header += "No strong reaction peaks found — select based on message volume.\n\n"

    return header + "\n".join(blocks)


async def run_processing(
    job_id: str, user_id: str, video_path: str, prompt: str,
    output_format: str, quality: str,
    music_path: str = None, music_trim_start: float = None, music_trim_end: float = None,
    content_type: str = "video"
):
    try:
        print(f"[JOB {job_id}] Starting processing...")
        output_dir = f"/tmp/outputs/{job_id}"
        os.makedirs(output_dir, exist_ok=True)

        import shutil as _shutil
        total, used, free = _shutil.disk_usage("/tmp")
        print(f"[JOB {job_id}] Disk space - total: {total//(1024**3)}GB, used: {used//(1024**3)}GB, free: {free//(1024**3)}GB")

        # ── STEP 1: Get video duration ──
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ], capture_output=True, text=True)
        video_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 7200
        print(f"[JOB {job_id}] Video duration: {video_duration:.0f}s ({video_duration/3600:.2f}h)")

        # ── STEP 6: Claude Sonnet — intelligent editing decision ──
        update_job(job_id, "analyzing")
        print(f"[JOB {job_id}] Content type: {content_type} | Sending to Claude Sonnet...")
        example_prompts = get_example_prompts(prompt)

        # Skip Deepgram only for pure format/effect changes that don't need transcript
        needs_captions = any(w in prompt.lower() for w in ["caption", "captions", "subtitle", "subtitles", "titulky", "text"])
        skip_transcription = (
            content_type == "short_clip" and
            not needs_captions and
            any(w in prompt.lower() for w in ["vertical", "vertikální", "effect", "efekt", "music", "hudba"])
            and not any(w in prompt.lower() for w in ["cut", "trim", "remove", "find", "best", "moment"])
        )

        if skip_transcription:
            # For pure effect/format changes — no need to analyse content
            segments = []
            energy_map = []
            stream_context = ""
            peak_candidates = ""
            print(f"[JOB {job_id}] Skipping transcription — pure effect/format job")
        else:
            # Extract audio and transcribe
            update_job(job_id, "transcribing")
            audio_path = f"{output_dir}/audio.wav"
            print(f"[JOB {job_id}] Extracting audio...")
            extract_result = subprocess.run([
                "ffmpeg", "-i", video_path,
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                audio_path, "-y", "-loglevel", "error"
            ], capture_output=True, text=True, timeout=3600)
            if extract_result.returncode != 0:
                raise Exception(f"Audio extraction failed: {extract_result.stderr[:300]}")

            # Debug: check audio file size
            audio_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
            print(f"[JOB {job_id}] Audio WAV size: {audio_size/1024/1024:.1f}MB")

            # Expected size: 16kHz mono PCM = ~32KB/sec
            expected_min = video_duration * 32 * 1024 * 0.1  # 10% of expected = definitely wrong
            if audio_size < 10000:
                raise Exception(f"Audio extraction produced empty file ({audio_size} bytes)")
            if audio_size < expected_min:
                print(f"[JOB {job_id}] WARNING: Audio too small ({audio_size/1024/1024:.1f}MB, expected ~{expected_min/1024/1024:.0f}MB+) — retrying extraction...")
                # Retry with different flags
                extract_retry = subprocess.run([
                    "ffmpeg", "-i", video_path,
                    "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    audio_path, "-y"
                ], capture_output=True, text=True, timeout=3600)
                audio_size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0
                print(f"[JOB {job_id}] Retry audio size: {audio_size/1024/1024:.1f}MB")
                if audio_size < 10000:
                    raise Exception(f"Audio extraction failed after retry — video may have no audio stream")

            segments = await transcribe_with_deepgram(audio_path, job_id)

            # Energy analysis only for longer content
            if content_type in ("video", "stream") and video_duration > 300:
                energy_map = analyze_audio_energy(video_path, job_id, interval=10.0)
            elif content_type == "short_clip" and video_duration > 60:
                energy_map = analyze_audio_energy(video_path, job_id, interval=1.0)
            else:
                energy_map = []

            stream_context = build_stream_context(segments, energy_map, video_duration, job_id)

            # Use Gemini for best moments if available, else fall back to audio analysis
            p_lower_check = prompt.lower()
            wants_best = any(w in p_lower_check for w in [
                "best moment", "best moments", "highlight", "highlights", "funny", "funniest",
                "hype", "exciting", "nejlepší", "vtipný", "vtipné"
            ])
            num_match_g = re.search(r'(\d+)\s*(clip|moment|video|highlight|funny|part)', p_lower_check)
            num_clips_g = int(num_match_g.group(1)) if num_match_g else 3

            if wants_best and GEMINI_AVAILABLE and gemini_client:
                peak_candidates = await analyze_with_gemini(video_path, prompt, num_clips_g, video_duration, job_id)
            else:
                peak_candidates = ""

            # Fall back to audio analysis if Gemini unavailable or failed
            if not peak_candidates:
                peak_candidates = build_peak_candidates(segments, energy_map, video_duration, job_id)

            try:
                os.remove(audio_path)
            except:
                pass

        # Build smart prompt based on content type
        from ai_logic import build_ai_prompt
        import re as _re

        p_lower = prompt.lower()
        wants_split = any(w in p_lower for w in ["split", "parts", "rozděl", "části", "divide"])
        wants_moments = any(w in p_lower for w in ["best moment", "best moments", "highlight", "highlights", "funny", "funniest", "hype", "exciting", "nejlepší", "vtipný", "vtipné"])

        tasks = ["moments", "split"] if (wants_split and wants_moments) else ["combined"]

        all_clips = []
        all_instructions = None

        for task in tasks:
            if task == "moments":
                task_prompt = _re.sub(r"\b(split|parts|rozděl|části|divide)\b", "", prompt, flags=_re.IGNORECASE).strip()
            elif task == "split":
                task_prompt = _re.sub(r"\b(best moments?|highlights?|funny|funniest|hype|exciting|nejlepší|vtipný|vtipné)\b", "", prompt, flags=_re.IGNORECASE).strip()
            else:
                task_prompt = prompt

            ai_prompt = build_ai_prompt(
                content_type=content_type,
                user_prompt=task_prompt,
                stream_context=stream_context,
                peak_candidates=peak_candidates,
                video_duration=video_duration,
                segments=segments,
                example_prompts=example_prompts,
            )

            ai_response = anthropic_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4000,
                messages=[{"role": "user", "content": ai_prompt}]
            )

            ai_text = ai_response.content[0].text.strip()
            if ai_text.startswith("```"):
                ai_text = ai_text.split("```")[1]
                if ai_text.startswith("json"):
                    ai_text = ai_text[4:]
                ai_text = ai_text.strip()

            try:
                task_instructions = json.loads(ai_text)
            except json.JSONDecodeError:
                print(f"[JOB {job_id}] Claude JSON parse failed ({task}): {ai_text[:200]}")
                task_instructions = {
                    "clips": [{"start": 0, "end": min(300, video_duration), "label": "clip_1"}],
                    "add_captions": False, "add_music": False,
                    "output_type": "clips", "description": "Fallback: first 5 minutes"
                }

            all_clips.extend(task_instructions.get("clips", []))
            if all_instructions is None:
                all_instructions = task_instructions

        all_instructions["clips"] = all_clips
        instructions = all_instructions

        clips = instructions.get("clips", [])
        add_captions = instructions.get("add_captions", False)
        vertical_format = instructions.get("vertical_format", False)
        print(f"[JOB {job_id}] Claude selected {len(clips)} clips | vertical={vertical_format} captions={add_captions} | {instructions.get('description', '')}")

        # ── STEP 7: FFmpeg — render clips (parallel) ──
        update_job(job_id, "editing")
        quality_scale = "1920:1080" if quality == "1080p" else "3840:2160" if quality == "4K" else "1280:720"
        output_files = []

        async def render_clip(clip):
          try:
            start = float(clip["start"])
            end = float(clip["end"])
            start = max(0, min(start, video_duration - 1))
            end = max(start + 1, min(end, video_duration))
            if end - start < 5:
                print(f"[JOB {job_id}] Skipping clip {clip.get('label')} — too short ({end-start:.1f}s)")
                return None

            label = clip.get("label", f"clip_{hash(clip['start'])}")
            label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)[:50]
            out_path = f"{output_dir}/{label}.{output_format}"

            bg_vol = instructions.get("bg_audio_volume")
            add_captions_clip = instructions.get("add_captions", False) and bool(segments)
            vertical_fmt = instructions.get("vertical_format", False)
            effects = instructions.get("effects", [])
            clip_duration = end - start
            effects_filter = build_effects_filter(effects, clip_duration=clip_duration) if effects else ""

            # Prepare SRT
            srt_path = None
            if add_captions_clip:
                srt_path = f"{output_dir}/{label}.srt"
                generate_srt(segments, srt_path, start, end)
                if not os.path.exists(srt_path) or os.path.getsize(srt_path) == 0:
                    print(f"[JOB {job_id}] SRT empty for {label}, skipping captions")
                    srt_path = None

            # Audio args
            if bg_vol is not None and bg_vol == 0.0:
                audio_args = ["-an"]
            elif bg_vol is not None and 0 < bg_vol < 1.0:
                audio_args = ["-af", f"volume={bg_vol}", "-c:a", "aac", "-b:a", "320k"]
            else:
                audio_args = ["-c:a", "aac", "-b:a", "320k"]

            loop = asyncio.get_event_loop()

            # Determine if we need music in this clip
            add_music_here = (
                music_path and os.path.exists(music_path) and
                instructions.get("add_music", False)
            )
            music_volume = get_music_volume(prompt) if add_music_here else None

            # Pass 1: video + audio (+ music if needed) in ONE pass
            temp_path = f"{output_dir}/{label}_temp.{output_format}"

            # Effects suffix for filter_complex branches (appended to final video node)
            effects_fc = f",{effects_filter}" if effects_filter else ""

            if vertical_fmt and add_music_here:
                # Vertical + music + optional effects in one pass
                music_input_args = []
                if music_trim_start is not None and music_trim_end is not None:
                    music_input_args = ["-ss", str(music_trim_start), "-to", str(music_trim_end)]
                fc = (
                    "split=2[bg][fg];"
                    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,boxblur=20:20[blurred];"
                    "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[small];"
                    f"[blurred][small]overlay=(W-w)/2:(H-h)/2{effects_fc}[v];"
                    f"[1:a]volume={music_volume},atrim=duration={clip_duration}[music];"
                    f"[0:a][music]amix=inputs=2:duration=first:normalize=0[aout]"
                )
                cmd1 = (
                    ["ffmpeg", "-ss", str(start), "-i", video_path, "-to", str(clip_duration)]
                    + music_input_args + ["-i", music_path,
                     "-filter_complex", fc,
                     "-map", "[v]", "-map", "[aout]",
                     "-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-b:a", "320k",
                     temp_path, "-y"]
                )
            elif add_music_here:
                # Normal + music + optional effects in one pass
                music_input_args = []
                if music_trim_start is not None and music_trim_end is not None:
                    music_input_args = ["-ss", str(music_trim_start), "-to", str(music_trim_end)]
                video_chain = f"[0:v]scale={quality_scale}{effects_fc}[v]"
                fc = (
                    f"{video_chain};"
                    f"[1:a]volume={music_volume},atrim=duration={clip_duration}[music];"
                    f"[0:a][music]amix=inputs=2:duration=first:normalize=0[aout]"
                )
                cmd1 = (
                    ["ffmpeg", "-ss", str(start), "-i", video_path, "-to", str(clip_duration)]
                    + music_input_args + ["-i", music_path,
                     "-filter_complex", fc,
                     "-map", "[v]", "-map", "[aout]",
                     "-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-b:a", "320k",
                     temp_path, "-y"]
                )
            elif vertical_fmt:
                fc = (
                    "split=2[bg][fg];"
                    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,boxblur=20:20[blurred];"
                    "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[small];"
                    f"[blurred][small]overlay=(W-w)/2:(H-h)/2{effects_fc}"
                )
                cmd1 = (
                    ["ffmpeg", "-ss", str(start), "-i", video_path, "-to", str(clip_duration),
                     "-filter_complex", fc, "-map", "0:a",
                     "-c:v", "libx264", "-crf", "23"]
                    + audio_args + [temp_path, "-y"]
                )
            else:
                cmd1 = (
                    ["ffmpeg", "-ss", str(start), "-i", video_path, "-to", str(clip_duration),
                     "-vf", ",".join(f for f in [f"scale={quality_scale}", effects_filter] if f),
                     "-c:v", "libx264", "-crf", "23"]
                    + audio_args + [temp_path, "-y"]
                )

            cmd1_copy = list(cmd1)
            r1 = await loop.run_in_executor(
                None, lambda cmd=cmd1_copy: subprocess.run(cmd, capture_output=True, text=True)
            )
            if r1.returncode != 0 or not os.path.exists(temp_path):
                print(f"[JOB {job_id}] FFmpeg pass1 failed for {label} (rc={r1.returncode}): {r1.stderr[-500:]}")
                return None
            print(f"[JOB {job_id}] Pass1 done: {label}")

            # Pass 2: subtitles (separate pass — more reliable)
            if srt_path:
                font_size = 14 if vertical_fmt else 16
                sub_filter = f"subtitles={srt_path}:force_style='FontSize={font_size},FontName=Arial Rounded MT Bold,PrimaryColour=&H00FFFF,OutlineColour=&H000000,Outline=3,Bold=1,Shadow=1,MarginV=40'"
                cmd2 = [
                    "ffmpeg", "-i", temp_path,
                    "-vf", sub_filter,
                    "-c:v", "libx264", "-crf", "23", "-c:a", "copy",
                    out_path, "-y"
                ]
                cmd2_copy = list(cmd2)
                r2 = await loop.run_in_executor(
                    None, lambda cmd=cmd2_copy: subprocess.run(cmd, capture_output=True, text=True)
                )
                try: os.remove(temp_path)
                except: pass
                if r2.returncode != 0 or not os.path.exists(out_path):
                    print(f"[JOB {job_id}] Pass2 subtitles failed for {label}: {r2.stderr[-300:]}")
                    return None
                print(f"[JOB {job_id}] Pass2 done (subtitles): {label}")
            else:
                os.replace(temp_path, out_path)

            print(f"[JOB {job_id}] Clip {label} rendered ({end-start:.0f}s)")
            if add_music_here:
                print(f"[JOB {job_id}] Music included in {label}")

            size = os.path.getsize(out_path)
            return {
                "filename": f"{label}.{output_format}",
                "path": out_path,
                "size_mb": round(size / 1048576, 1),
                "duration": round(end - start, 1),
                "label": label,
            }
          except Exception as e:
            import traceback
            print(f"[JOB {job_id}] render_clip exception for {clip.get('label','?')}: {e}\n{traceback.format_exc()}")
            return None

        # Render all clips in parallel
        results = await asyncio.gather(*[render_clip(clip) for clip in clips], return_exceptions=True)
        output_files = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"[JOB {job_id}] render_clip[{i}] exception: {r}")
            elif r is None:
                print(f"[JOB {job_id}] render_clip[{i}] returned None")
            else:
                output_files.append(r)

        if not output_files:
            raise Exception("No clips were rendered. Check timestamps and video file.")

        # ── STEP 8: Upload results ──
        update_job(job_id, "uploading")
        base_url = "growth-partner-edit-tool-production.up.railway.app"
        download_urls = []
        for f in output_files:
            download_urls.append({
                "filename": f["filename"],
                "url": f"https://{base_url}/download/{job_id}/{f['filename']}",
                "size_mb": f["size_mb"],
                "duration": f["duration"],
            })

        supabase.table("jobs").update({
            "status": "done",
            "result": json.dumps(download_urls),
            "description": instructions.get("description", ""),
        }).eq("id", job_id).execute()

        # Track hours used
        try:
            input_duration_hours = video_duration / 3600
            supabase.rpc("increment_hours_used", {"user_id": user_id, "hours": input_duration_hours}).execute()
        except Exception as e:
            print(f"[JOB {job_id}] Hours tracking error: {e}")

        # ── CLEANUP ──
        import shutil as _shutil

        # Remove uploaded video (no longer needed)
        try:
            upload_dir = os.path.dirname(video_path)
            if upload_dir.startswith("/tmp/uploads/"):
                _shutil.rmtree(upload_dir, ignore_errors=True)
                print(f"[JOB {job_id}] Cleaned upload dir: {upload_dir}")
        except Exception as e:
            print(f"[JOB {job_id}] Upload cleanup error: {e}")

        # Remove old output dirs (keep only last 3 jobs to allow downloads)
        try:
            outputs_root = "/tmp/outputs"
            if os.path.exists(outputs_root):
                job_dirs = sorted(
                    [d for d in os.listdir(outputs_root) if os.path.isdir(f"{outputs_root}/{d}")],
                    key=lambda d: os.path.getmtime(f"{outputs_root}/{d}")
                )
                # Keep last 3, delete older ones
                for old_dir in job_dirs[:-3]:
                    _shutil.rmtree(f"{outputs_root}/{old_dir}", ignore_errors=True)
                    print(f"[JOB {job_id}] Cleaned old output: {old_dir}")
        except Exception as e:
            print(f"[JOB {job_id}] Output cleanup error: {e}")

        print(f"[JOB {job_id}] Done! {len(output_files)} clips ready.")

    except Exception as e:
        import traceback
        print(f"[JOB {job_id}] Processing error: {e}\n{traceback.format_exc()}")
        supabase.table("jobs").update({"status": "error", "error": str(e)[:200]}).eq("id", job_id).execute()


# ────────────────────────────────────────────
# PROMPT LEARNING
# ────────────────────────────────────────────

def save_successful_prompt(prompt: str, output_type: str, user_id: str):
    try:
        supabase.table("successful_prompts").insert({
            "prompt": prompt, "output_type": output_type, "user_id": user_id
        }).execute()
    except Exception as e:
        print(f"Prompt save error: {e}")

def get_example_prompts(prompt: str, limit: int = 3) -> str:
    try:
        result = supabase.table("successful_prompts").select("prompt, output_type").limit(limit).execute()
        if not result.data:
            return ""
        examples = "\n".join([f"- \"{r['prompt']}\" → {r['output_type']}" for r in result.data])
        return f"\nExamples of successful instructions from other users:\n{examples}\n"
    except:
        return ""


# ────────────────────────────────────────────
# EMAIL
# ────────────────────────────────────────────

def send_email(to: str, subject: str, html: str):
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return
    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": "Growth Partner Edit Tool <noreply@growth-partner.agency>", "to": [to], "subject": subject, "html": html},
            timeout=10,
        )
    except Exception as e:
        print(f"Email error: {e}")

def send_purchase_confirmation(email: str, plan: str):
    prices = {"basic": "$29", "plus": "$59", "gold": "$99"}
    limits = {"basic": "20 hours", "plus": "60 hours", "gold": "Unlimited"}
    price = prices.get(plan, "")
    limit = limits.get(plan, "")
    gold_row = '<tr><td style="padding:6px 0;color:#f0eeff;font-size:14px;">✓ &nbsp; Desktop app — process on your own GPU</td></tr>' if plan == 'gold' else ''
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#080810;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#080810;padding:40px 20px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#0e0e1a;border:1px solid rgba(155,48,255,0.2);border-radius:16px;overflow:hidden;">
<tr><td style="background:linear-gradient(135deg,#7b2fe0,#c040ff);padding:32px 40px;text-align:center;">
<h1 style="margin:0;color:#fff;font-size:24px;font-weight:800;">Growth Partner Edit Tool</h1>
<p style="margin:8px 0 0;color:rgba(255,255,255,0.8);font-size:14px;">Your subscription is confirmed</p>
</td></tr>
<tr><td style="padding:40px;">
<h2 style="color:#f0eeff;font-size:20px;margin:0 0 16px;">Thank you for your purchase! 🎉</h2>
<p style="color:#8b82a8;font-size:15px;line-height:1.7;margin:0 0 24px;">Your <strong style="color:#c984ff;">{plan.capitalize()} plan</strong> is now active.</p>
<table width="100%" cellpadding="0" cellspacing="0" style="background:rgba(155,48,255,0.08);border:1px solid rgba(155,48,255,0.2);border-radius:12px;margin-bottom:28px;">
<tr><td style="padding:20px 24px;">
<table width="100%"><tr>
<td><span style="color:#f0eeff;font-size:18px;font-weight:700;">{plan.capitalize()}</span><br><span style="color:#8b82a8;font-size:13px;">{limit} / month</span></td>
<td align="right"><span style="color:#c984ff;font-size:22px;font-weight:800;">{price}/mo</span></td>
</tr></table></td></tr></table>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
<tr><td style="padding:6px 0;color:#f0eeff;font-size:14px;">✓ &nbsp; AI-powered video editing</td></tr>
<tr><td style="padding:6px 0;color:#f0eeff;font-size:14px;">✓ &nbsp; Automatic captions in 99 languages</td></tr>
<tr><td style="padding:6px 0;color:#f0eeff;font-size:14px;">✓ &nbsp; Short clips for Instagram, TikTok & YouTube</td></tr>
{gold_row}
</table>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
<tr><td align="center"><a href="https://growth-partner.agency" style="display:inline-block;background:linear-gradient(135deg,#7b2fe0,#c040ff);color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:15px;font-weight:700;">Start editing now →</a></td></tr>
</table>
<p style="color:#4a4460;font-size:13px;">Questions? <a href="mailto:your@growth-partner.agency" style="color:#c984ff;">your@growth-partner.agency</a></p>
</td></tr>
<tr><td style="padding:20px 40px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;">
<p style="color:#4a4460;font-size:12px;margin:0;">© 2026 Growth Partner Edit Tool</p>
</td></tr></table></td></tr></table></body></html>"""
    send_email(email, f"Your {plan.capitalize()} plan is now active 🎉", html)

def send_welcome_email(email: str, name: str):
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#080810;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#080810;padding:40px 20px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#0e0e1a;border:1px solid rgba(155,48,255,0.2);border-radius:16px;overflow:hidden;">
<tr><td style="background:linear-gradient(135deg,#7b2fe0,#c040ff);padding:32px 40px;text-align:center;">
<h1 style="margin:0;color:#fff;font-size:24px;font-weight:800;">Growth Partner Edit Tool</h1>
<p style="margin:8px 0 0;color:rgba(255,255,255,0.8);font-size:14px;">Welcome aboard! 🎉</p>
</td></tr>
<tr><td style="padding:40px;">
<h2 style="color:#f0eeff;font-size:20px;margin:0 0 16px;">Hey {name}, welcome! 👋</h2>
<p style="color:#8b82a8;font-size:15px;line-height:1.7;margin:0 0 20px;">Your free trial is now active. You have <strong style="color:#c984ff;">7 days or 5 hours</strong> of video processing to try everything out.</p>
<p style="color:#8b82a8;font-size:15px;line-height:1.7;margin:0 0 28px;">Just upload a video, type what you want, and let AI do the work.</p>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
<tr><td align="center"><a href="https://growth-partner.agency" style="display:inline-block;background:linear-gradient(135deg,#7b2fe0,#c040ff);color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-size:15px;font-weight:700;">Start editing →</a></td></tr>
</table>
<p style="color:#4a4460;font-size:13px;">Questions? <a href="mailto:your@growth-partner.agency" style="color:#c984ff;">your@growth-partner.agency</a></p>
</td></tr>
<tr><td style="padding:20px 40px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;">
<p style="color:#4a4460;font-size:12px;margin:0;">© 2026 Growth Partner Edit Tool</p>
</td></tr></table></td></tr></table></body></html>"""
    send_email(email, "Welcome to Growth Partner Edit Tool 🎉", html)
