from flask import Flask, request, jsonify, send_file, redirect
import yt_dlp
import os
import tempfile
import uuid
import threading
import requests
import time
import json

app = Flask(__name__)

temp_files = {}

# ---------------- TikTok config (from environment variables) ----------------
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REFRESH_TOKEN = os.environ.get("TIKTOK_REFRESH_TOKEN", "")
TIKTOK_REDIRECT_URI = os.environ.get(
    "TIKTOK_REDIRECT_URI",
    "https://code-automation.onrender.com/tiktok/callback",
)


def cleanup_file(filepath, file_id, delay=300):
    import time
    time.sleep(delay)
    if os.path.exists(filepath):
        os.remove(filepath)
    temp_files.pop(file_id, None)


@app.route("/download", methods=["POST"])
def download():
    if request.is_json:
        data = request.json
        url = data.get("url") if data else None
    else:
        url = request.form.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    temp_dir = tempfile.gettempdir()
    file_id = str(uuid.uuid4())
    temp_path = os.path.join(temp_dir, f"{file_id}.mp4")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "outtmpl": temp_path,
        "merge_output_format": "mp4",
    }

    # use Instagram/TikTok login cookies if provided (Render Secret File)
    for cookie_path in ["/etc/secrets/cookies.txt", "cookies.txt"]:
        if os.path.exists(cookie_path):
            ydl_opts["cookiefile"] = cookie_path
            break

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "")

        actual_path = temp_path
        if not os.path.exists(actual_path):
            for ext in ["mp4", "webm", "mkv"]:
                candidate = os.path.join(temp_dir, f"{file_id}.{ext}")
                if os.path.exists(candidate):
                    actual_path = candidate
                    break

        temp_files[file_id] = (actual_path, title)

        t = threading.Thread(target=cleanup_file, args=(actual_path, file_id, 300))
        t.daemon = True
        t.start()

        base_url = request.host_url.rstrip("/")
        file_url = f"{base_url}/file/{file_id}"

        return jsonify({
            "url": file_url,
            "title": title,
            "medias": [{"url": file_url}],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/file/<file_id>", methods=["GET"])
def serve_file(file_id):
    if file_id not in temp_files:
        return jsonify({"error": "File not found or expired"}), 404

    filepath, _ = temp_files[file_id]

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    return send_file(filepath, mimetype="video/mp4")


# ---------------- TikTok OAuth (run once to get the refresh token) ----------------
@app.route("/tiktok/login", methods=["GET"])
def tiktok_login():
    if not TIKTOK_CLIENT_KEY:
        return "TIKTOK_CLIENT_KEY is not set", 500
    # which account we are authorizing (default = deen)
    account = request.args.get("account", "deen")
    auth_url = (
        "https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={TIKTOK_CLIENT_KEY}"
        "&scope=video.upload"
        "&response_type=code"
        f"&redirect_uri={TIKTOK_REDIRECT_URI}"
        f"&state={account}"
    )
    return redirect(auth_url)


@app.route("/tiktok/callback", methods=["GET"])
def tiktok_callback():
    code = request.args.get("code")
    account = request.args.get("state", "deen")
    if not code:
        return "No authorization code received", 400

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT_URI,
        },
    )
    data = resp.json()
    refresh = data.get("refresh_token", "NOT FOUND")
    if refresh and refresh != "NOT FOUND":
        _save_refresh_token(refresh, account)
        return (
            f"<h2>✅ Done! Account '{account}' is connected and saved.</h2>"
            "<p>You can close this page. The automation will post to this "
            "account automatically.</p>"
        )
    return (
        "<h2>❌ Could not get a refresh token. Response:</h2>"
        f"<pre>{data}</pre>"
    )


# access token cache per account: { account_name: {access_token, expires_at} }
_token_cache = {}


def _token_file(account):
    return f"/tmp/tiktok_token_{account}.json"


def _load_refresh_token(account):
    """Use the rotated refresh token saved on disk if available.
    For the default account, fall back to the env variable."""
    try:
        with open(_token_file(account)) as f:
            saved = json.load(f).get("refresh_token")
            if saved:
                return saved
    except Exception:
        pass
    if account == "deen":
        return TIKTOK_REFRESH_TOKEN
    return None


def _save_refresh_token(rt, account):
    try:
        with open(_token_file(account), "w") as f:
            json.dump({"refresh_token": rt}, f)
    except Exception:
        pass


def get_tiktok_access_token(account):
    # reuse a cached access token while it is still valid
    now = time.time()
    cached = _token_cache.get(account)
    if cached and now < cached["expires_at"]:
        return cached["access_token"], {"cached": True}

    refresh_token = _load_refresh_token(account)
    if not refresh_token:
        return None, {"error": f"no refresh token saved for account '{account}'. "
                               f"Open /tiktok/login?account={account} first."}

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    data = resp.json()
    access = data.get("access_token")
    if access:
        _token_cache[account] = {
            "access_token": access,
            "expires_at": now + data.get("expires_in", 86400) - 60,
        }
        # TikTok rotates the refresh token: save the new one
        new_rt = data.get("refresh_token")
        if new_rt:
            _save_refresh_token(new_rt, account)
    return access, data


@app.route("/upload-tiktok", methods=["POST"])
def upload_tiktok():
    if request.is_json:
        data = request.json or {}
        url = data.get("url")
        account = data.get("account") or "deen"
    else:
        url = request.form.get("url")
        account = request.form.get("account") or "deen"

    if not url:
        return jsonify({"error": "video url is required"}), 400

    try:
        # 1) download the video bytes (from Cloudinary url)
        video_resp = requests.get(url)
        video_bytes = video_resp.content
        video_size = len(video_bytes)

        # 2) get a fresh access token for the chosen account
        access_token, token_data = get_tiktok_access_token(account)
        if not access_token:
            return jsonify({
                "error": "could not get TikTok access token",
                "tiktok_response": token_data,
            }), 500

        # 3) init an inbox (draft) upload
        init_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": video_size,
                    "total_chunk_count": 1,
                }
            },
        )
        init_data = init_resp.json()

        upload_url = init_data.get("data", {}).get("upload_url")
        if not upload_url:
            return jsonify({"error": "init failed", "details": init_data}), 500

        # 4) upload the bytes to TikTok
        put_resp = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
            },
            data=video_bytes,
        )

        return jsonify({
            "status": "ok",
            "tiktok_status": put_resp.status_code,
            "publish_id": init_data.get("data", {}).get("publish_id"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/privacy", methods=["GET"])
def privacy():
    return """
    <h1>Privacy Policy</h1>
    <p>This application is a personal tool used to upload the owner's own
    videos to the owner's own TikTok account as drafts.</p>
    <p>We do not collect, store, share, or sell any personal data from any
    third party. The app only accesses the owner's own TikTok account after
    explicit authorization, solely to upload video content the owner provides.</p>
    <p>No data is shared with any third parties.</p>
    <p>For any questions, contact the app owner.</p>
    """


@app.route("/terms", methods=["GET"])
def terms():
    return """
    <h1>Terms of Service</h1>
    <p>This application is provided for personal use by its owner to upload
    the owner's own video content to the owner's own TikTok account as drafts.</p>
    <p>The app is provided "as is" without warranty of any kind. The owner is
    responsible for the content uploaded and for complying with TikTok's
    Community Guidelines and Terms of Service.</p>
    """


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
