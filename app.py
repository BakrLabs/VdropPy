from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import threading
import uuid
import json
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track progress per job
progress_store = {}

def get_progress_hook(job_id):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total else 0
            speed = d.get('speed', 0) or 0
            eta = d.get('eta', 0) or 0
            progress_store[job_id] = {
                'status': 'downloading',
                'percent': round(percent, 1),
                'speed': format_speed(speed),
                'eta': eta,
                'filename': d.get('filename', '')
            }
        elif d['status'] == 'finished':
            progress_store[job_id] = {
                'status': 'processing',
                'percent': 99,
                'filename': d.get('filename', '')
            }
        elif d['status'] == 'error':
            progress_store[job_id] = {'status': 'error', 'message': str(d.get('error', 'Unknown error'))}
    return hook

def format_speed(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024**2:
        return f"{bps/1024:.1f} KB/s"
    else:
        return f"{bps/1024**2:.1f} MB/s"

@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        is_playlist = info.get('_type') == 'playlist'
        entries = info.get('entries', []) if is_playlist else [info]

        # Get available formats from first entry
        first = entries[0] if entries else info
        if is_playlist:
            # Fetch full info for first entry to get formats
            with yt_dlp.YoutubeDL({'quiet': True}) as ydl2:
                first = ydl2.extract_info(first.get('url') or first.get('webpage_url', ''), download=False)

        formats = []
        seen = set()
        for f in (first.get('formats') or []):
            height = f.get('height')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            if vcodec != 'none' and height and height not in seen:
                seen.add(height)
                formats.append({
                    'format_id': f['format_id'],
                    'height': height,
                    'label': f"{height}p",
                    'ext': f.get('ext', 'mp4')
                })

        formats.sort(key=lambda x: x['height'], reverse=True)

        result = {
            'title': info.get('title', 'Unknown'),
            'thumbnail': info.get('thumbnail', ''),
            'uploader': info.get('uploader', ''),
            'duration': info.get('duration'),
            'is_playlist': is_playlist,
            'playlist_count': len(entries) if is_playlist else 1,
            'formats': formats
        }
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.json
    url = data.get('url', '').strip()
    quality = data.get('quality', 'best')  # e.g. '1080', '720', 'audio'
    audio_only = data.get('audio_only', False)

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    job_id = str(uuid.uuid4())
    progress_store[job_id] = {'status': 'starting', 'percent': 0}

    def run_download():
        try:
            outtmpl = os.path.join(DOWNLOAD_DIR, job_id, '%(title)s.%(ext)s')
            os.makedirs(os.path.join(DOWNLOAD_DIR, job_id), exist_ok=True)

            if audio_only:
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'outtmpl': outtmpl,
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'progress_hooks': [get_progress_hook(job_id)],
                    'quiet': True,
                }
            else:
                if quality == 'best':
                    fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
                else:
                    fmt = f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best[height<={quality}]'

                ydl_opts = {
                    'format': fmt,
                    'outtmpl': outtmpl,
                    'merge_output_format': 'mp4',
                    'progress_hooks': [get_progress_hook(job_id)],
                    'quiet': True,
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # Find downloaded file(s)
            job_dir = os.path.join(DOWNLOAD_DIR, job_id)
            files = []
            for f in os.listdir(job_dir):
                fpath = os.path.join(job_dir, f)
                files.append({'name': f, 'size': os.path.getsize(fpath), 'path': fpath})

            progress_store[job_id] = {
                'status': 'done',
                'percent': 100,
                'files': files
            }

        except Exception as e:
            progress_store[job_id] = {'status': 'error', 'message': str(e)}

    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        while True:
            state = progress_store.get(job_id, {'status': 'unknown'})
            yield f"data: {json.dumps(state)}\n\n"
            if state.get('status') in ('done', 'error'):
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/file/<job_id>/<filename>')
def download_file(job_id, filename):
    path = os.path.join(DOWNLOAD_DIR, job_id, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.route('/')
def index():
    return app.send_static_file('index.html') if app.static_folder else \
           open('templates/index.html').read(), 200, {'Content-Type': 'text/html'}

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
      
