# ────────────────────────────────────────────────────────────────
# AI LOGIC — per content type
# ────────────────────────────────────────────────────────────────
import re

def build_ai_prompt(
    content_type: str,          # "short_clip" | "video" | "stream"
    user_prompt: str,
    stream_context: str,
    video_duration: float,
    segments: list,
    example_prompts: str = "",
    peak_candidates: str = "",
) -> str:
    """
    Build the Claude prompt based on content type.
    Each type has different guidelines, context size, and output format.
    """

    duration_str = (
        f"{int(video_duration // 3600)}h {int((video_duration % 3600) // 60)}m"
        if video_duration >= 3600
        else f"{int(video_duration // 60)}m {int(video_duration % 60)}s"
    )

    # ── Shared effects detection (used by all content types) ──
    p = user_prompt.lower()
    detected_effects = []
    if any(w in p for w in ["cinematic", "cinema", "film", "color grade", "filmový"]):
        detected_effects.append("cinematic")
    if any(w in p for w in ["zoom", "ken burns", "zoom in"]):
        detected_effects.append("zoom")
    if any(w in p for w in ["fade", "fade in", "fade out"]):
        detected_effects.append("fade")
    if any(w in p for w in ["vignette", "vignetta"]):
        detected_effects.append("vignette")
    if any(w in p for w in ["sharpen", "sharp", "ostřejší", "ostrý"]):
        detected_effects.append("sharpen")
    if any(w in p for w in ["effect", "effects", "efekt", "efekty", "special"]) and not detected_effects:
        detected_effects = ["cinematic", "fade"]  # default nice combo
    effects_instruction = f'- MUST set "effects": {detected_effects} in JSON output.' if detected_effects else '- Leave "effects": [] unless user asked for effects.'

    # ── JSON schema always the same ──
    json_schema = """{
  "clips": [
    {"start": 0.0, "end": 0.0, "label": "descriptive_clip_name"}
  ],
  "add_captions": false,
  "vertical_format": false,
  "add_music": false,
  "bg_audio_volume": null,
  "effects": [],
  "output_type": "clips",
  "description": "Brief description of selections and reasoning"
}

Available effects (use exact strings in the "effects" array):
- "cinematic" — warm color grade, slight vignette, cinematic look
- "zoom" — slow Ken Burns zoom in effect
- "fade" — fade in at start, fade out at end
- "vignette" — dark edges, focused center
- "sharpen" — sharper image"""

    # ── Detect if user wants best moments (used by all content types) ──
    wants_best_moments = any(w in p for w in [
        "best moment", "best moments", "highlight", "highlights", "funny", "funniest",
        "hype", "exciting", "nejlepší moment", "nejlepší momenty", "vtipný", "vtipné"
    ])
    peak_section = ""
    if peak_candidates and wants_best_moments:
        peak_section = f"\n{peak_candidates}\n"

    # ────────────────────────────────
    # FREE MODE — Claude generates FFmpeg command directly
    # ────────────────────────────────
    if content_type == "free":
        transcript_text = " ".join(s["text"] for s in segments[:300])[:4000]
        return f"""You are an expert FFmpeg video editor. Generate a single FFmpeg command to fulfill the user's request.

USER INSTRUCTION: {user_prompt}

VIDEO: input.mp4
DURATION: {duration_str} ({video_duration:.1f} seconds)
TRANSCRIPT: {transcript_text if transcript_text else "(no speech)"}

RULES:
1. Return ONLY a JSON object, no other text.
2. The command must use "input.mp4" as input and "output.mp4" as output.
3. Do NOT use -y flag (handled externally).
4. Do NOT reference any files other than input.mp4 and generated temp files.
5. Keep the command safe — no file deletion, no system commands, no pipes to shell.
6. If image generation is needed (e.g. background), generate it with ffmpeg lavfi source.

Return this JSON format:
{{
  "ffmpeg_args": ["ffmpeg", "-i", "input.mp4", ...more args..., "output.mp4"],
  "description": "What this command does in plain English"
}}"""

    # ────────────────────────────────
    # SHORT CLIP (< ~10 min)
    # ────────────────────────────────
    if content_type == "short_clip":
        transcript_text = " ".join(s["text"] for s in segments[:200])[:3000]

        vertical_format = any(w in p for w in ["vertical", "vertikální", "tiktok", "reels", "shorts", "portrait", "9:16"])
        add_captions = any(w in p for w in ["caption", "captions", "subtitle", "subtitles", "titulky"])
        add_music = any(w in p for w in ["music", "hudba", "beat", "song", "audio background"])

        return f"""You are an expert video editor. You are working with a SHORT CLIP (duration: {duration_str}).

USER INSTRUCTION: {user_prompt}
{example_prompts}
{peak_section}
VIDEO TRANSCRIPT:
{transcript_text if transcript_text else "(no speech detected)"}

VIDEO DURATION: {video_duration:.1f} seconds total

YOUR TASK:
This is a short clip. The user wants to apply an edit, effect, or format change to it.
- Do NOT cut out large sections — keep the content largely intact unless explicitly asked to trim
- If trimming/cutting: select EXACTLY the portion the user wants
- If CLIP CANDIDATES are shown above: use ONLY those start/end times, pick the best {1}
- Timestamps must be between 0 and {video_duration:.1f} seconds

REQUIRED JSON VALUES (set these exactly):
- "vertical_format": {"true" if vertical_format else "false"}
- "add_captions": {"true" if add_captions else "false"}
- "add_music": {"true" if add_music else "false"}
- {effects_instruction}

Return ONLY valid JSON, no other text:
{json_schema}"""

    # ────────────────────────────────
    # VIDEO (medium length)
    # ────────────────────────────────
    elif content_type == "video":
        transcript_text = " ".join(s["text"] for s in segments[:500])[:8000]

        vertical_format_v = any(w in p for w in ["vertical", "vertikální", "tiktok", "reels", "shorts", "portrait", "9:16"])

        return f"""You are an expert video editor specializing in YouTube content.

USER INSTRUCTION: {user_prompt}
{example_prompts}
{peak_section}
VIDEO DURATION: {duration_str} ({video_duration:.0f} seconds total)

VIDEO TRANSCRIPT (with timestamps):
{chr(10).join(f"[{s['start']:.0f}s] {s['text']}" for s in segments[:300]) if segments else "(no speech detected)"}

YOUR TASK:
This is a medium-length video. Analyze the content and apply the user's instruction.

GUIDELINES:
- If CLIP CANDIDATES are shown above: use ONLY those start/end times, pick by rank# (rank#1 = strongest spike), copy timestamps exactly
- "Find best moments" → pick top candidates by rank# if available, else select from transcript
- "Split into parts" → divide into equal parts at natural break points (pauses, topic changes)
- "Add captions" → keep full video (start=0, end={video_duration:.0f}), set add_captions=true
- NEVER select random sections — every clip must have a clear reason
- Timestamps must be between 0 and {video_duration:.0f} seconds
- Minimum clip: 20 seconds, Maximum clip: 50 seconds

REQUIRED JSON VALUES (set these exactly):
- "vertical_format": {"true" if vertical_format_v else "false"}
- "add_captions": {"true" if any(w in p for w in ["caption", "captions", "subtitle", "subtitles", "titulky"]) else "false"}
- "add_music": {"true" if any(w in p for w in ["music", "hudba", "beat", "song", "audio background"]) else "false"}
- {effects_instruction}

Return ONLY valid JSON, no other text:
{json_schema}"""

    # ────────────────────────────────
    # STREAM (long form)
    # ────────────────────────────────
    else:  # stream
        p = user_prompt.lower()

        # Determine what user wants
        wants_funniest = any(w in p for w in ["funny", "funniest", "vtipný", "vtipné", "nejlepší", "comedy", "laugh", "lol", "humor"])
        wants_hype = any(w in p for w in ["hype", "hype moments", "exciting", "crazy", "insane", "clutch", "epic", "best plays"])
        wants_split = any(w in p for w in ["split", "parts", "rozděl", "části", "divide"])
        wants_vertical = any(w in p for w in ["vertical", "vertikální", "tiktok", "reels", "shorts", "portrait", "9:16"])

        # How many clips to find
        num_match = re.search(r'(\d+)\s*(clip|moment|video|highlight|funny|part)', p)
        num_clips = int(num_match.group(1)) if num_match else 3
        num_clips = max(1, min(num_clips, 10))  # cap 1-10

        if wants_funniest:
            selection_criteria = f"""
SELECTION CRITERIA — FUNNIEST MOMENTS:
Find {num_clips} moments where the chat reacted most with laughs, chaos, or surprise.
- Look for blocks marked ⚡PEAK — high reaction% means diváci laughed or were shocked
- Look for LOL, LMAO, KEKW, 😂, 💀 in chat samples
- Clip start = block start time - 10s (for buildup), end = start + 45s
- Label should describe what happened: e.g. "chat_explodes_fail", "viewers_panic"
- Pick {num_clips} moments with highest reaction% or message count"""

        elif wants_hype:
            selection_criteria = f"""
SELECTION CRITERIA — HYPE MOMENTS:
Find {num_clips} moments where chat went most hype.
- Look for blocks marked ⚡PEAK with many messages
- Look for PogChamp, LET'S GO, POGGERS, clutch, insane in chat samples
- Clip start = block start time - 10s, end = start + 45s
- Label should describe the hype: e.g. "clutch_moment", "hype_peak"
- Pick {num_clips} moments with highest activity"""

        elif wants_split:
            part_duration = video_duration / num_clips
            selection_criteria = f"""
SELECTION CRITERIA — SPLIT INTO {num_clips} PARTS:
Divide the stream into {num_clips} roughly equal parts of ~{part_duration/60:.0f} minutes each.
- Cut at natural break points: pauses, topic changes, game switches
- Each part should start and end cleanly
- Parts should be roughly equal in length"""

        else:
            selection_criteria = f"""
SELECTION CRITERIA — BEST MOMENTS:
Find {num_clips} moments with highest chat activity and reactions.
- Prioritize blocks marked ⚡PEAK — these had the most viewer reactions
- Look for high reaction% and message counts
- Clip start = block start time - 10s, end = start + 45s
- Pick {num_clips} most active blocks and explain why each is a good moment"""

        vertical_note = "\nNOTE: Set vertical_format=true on ALL selected clips." if wants_vertical else ""

        return f"""You are a clip selector for a stream VOD. Pick the best {num_clips} clips based on chat activity.

USER INSTRUCTION: {user_prompt}

STREAM DURATION: {duration_str} ({video_duration:.0f} seconds total)

{peak_candidates if peak_candidates else stream_context}

{selection_criteria}

RULES:
1. Pick EXACTLY {num_clips} clips.
2. Each clip must be 20-50 seconds (end - start).
3. Timestamps must be between 0 and {video_duration:.0f}.
4. "vertical_format": {"true" if wants_vertical else "false"} — set exactly.
5. "add_captions": {"true" if any(w in p for w in ["caption","captions","subtitle","subtitles","titulky"]) else "false"} — set exactly.
6. {effects_instruction}

Return ONLY valid JSON, no other text:
{json_schema}"""
