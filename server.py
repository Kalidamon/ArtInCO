# server.py (versión corregida para desarrollo)
import os
import datetime
import tempfile
import shutil
import shlex
import subprocess
import logging
from io import BytesIO
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
import base64

from flask import Response, stream_with_context, Flask, request, jsonify, g, send_file
from flask_cors import CORS
import jwt
import bcrypt
import yt_dlp
from dotenv import load_dotenv

# -------------------------
# Config logging EARLY
# -------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

load_dotenv()

# -------------------------
# App config
# -------------------------
app = Flask(__name__)
CORS(app)  # Solo para desarrollo; en producción restringir orígenes

app.config['SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'super-secret-key')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = datetime.timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = datetime.timedelta(days=7)

# -------------------------
# "DB" de usuarios (solo demo)
# -------------------------
# Guardaremos hashes como bytes para evitar problemas de encode/decode
USERS = {}

def register_user(username, password):
    if username in USERS:
        logging.debug(f"Usuario {username} ya existe")
        return
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)  # bytes
    USERS[username] = hashed
    logging.info(f"Usuario {username} registrado (demo)")

# Registro inicial de ejemplo
register_user("admin", "T.ssV1uKLm<091mO9")

# -------------------------
# JWT helpers
# -------------------------
def encode_token(username, expires_delta):
    payload = {
        'sub': username,
        'exp': datetime.datetime.utcnow() + expires_delta
    }
    token = jwt.encode(payload, app.config['SECRET_KEY'], algorithm="HS256")
    # PyJWT v2 devuelve str, en v1 podía devolver bytes; garantizar str
    if isinstance(token, bytes):
        token = token.decode('utf-8')
    return token

def decode_token(token):
    # Retorna payload o lanza excepción (ExpiredSignatureError / InvalidTokenError)
    return jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])

# -------------------------
# Decorador para endpoints que requieren access token
# -------------------------
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', None)
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'message': 'Token faltante'}), 401
        token = auth_header.split(" ", 1)[1]
        try:
            data = decode_token(token)
            # expos user en g
            g.current_user = data.get('sub')
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token expirado'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token inválido'}), 401
        return f(*args, **kwargs)
    return decorated

# -------------------------
# Rutas de autenticación
# -------------------------

@app.route('/ping', methods=['GET'])
def ping():
    print("DEBUG: /ping ejecutado", flush=True)
    return jsonify({"pong": True})

@app.route('/login', methods=['POST'])
def login():
    print("Raw data:", request.data)  # 👈 ver qué llega realmente
    data = request.get_json(silent=True) or {}
    print("Parsed JSON:", data)
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({'message': 'Credenciales incompletas'}), 400

    stored_hash = USERS.get(username)
    if not stored_hash:
        return jsonify({'message': 'Credenciales inválidas'}), 401

    print("DEBUG: verificando hash...", flush=True)
    if bcrypt.checkpw(password.encode('utf-8'), stored_hash):
        print("DEBUG: hash correcto", flush=True)
        access_token = encode_token(username, app.config['JWT_ACCESS_TOKEN_EXPIRES'])
        print("DEBUG: access_token generado", flush=True)
        refresh_token = encode_token(username, app.config['JWT_REFRESH_TOKEN_EXPIRES'])
        print("DEBUG: refresh_token generado", flush=True)
        return jsonify({
            'access_token': access_token,
            'refresh_token': refresh_token
        })

    else:
        return jsonify({'message': 'Credenciales inválidas'}), 401

@app.route('/refresh', methods=['POST'])
def refresh():
    """
    Espera Authorization: Bearer <refresh_token>
    Valida el refresh token y devuelve un nuevo access_token.
    NOTA: en producción deberías guardar/invalidar refresh tokens, revisar revocación, etc.
    """
    auth_header = request.headers.get('Authorization', None)
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'message': 'Refresh token faltante'}), 401
    refresh_token = auth_header.split(" ", 1)[1]
    try:
        data = decode_token(refresh_token)
        username = data.get('sub')
        # emitir nuevo access token
        new_access = encode_token(username, app.config['JWT_ACCESS_TOKEN_EXPIRES'])
        return jsonify({
            'access_token': new_access,
            'refresh_token': refresh_token
        })
    except jwt.ExpiredSignatureError:
        return jsonify({'message': 'Token de actualización expirado'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'message': 'Token de actualización inválido'}), 402

# -------------------------
# Endpoint para ejecutar comandos (SÓLO DEMO y CON WHITELIST)
# -------------------------
ALLOWED_COMMANDS = {
    # comando -> lista posible args (no wildcards)
    "uptime": ["uptime"],
    "ls": ["ls", "-la"],   # ejemplo limitado
    "echo": ["echo"]       # ejemplo
}

@app.route('/execute_command', methods=['POST'])
@token_required
def execute_command():
    data = request.get_json(silent=True) or {}
    cmd = data.get('command')
    if not cmd:
        return jsonify({'message': 'Comando no proporcionado'}), 400

    # Solo permitir comandos exactos (evitar shell injection)
    # Ej: el cliente debe enviar "uptime" o "ls" o ["ls","/tmp"]
    if isinstance(cmd, str):
        parts = shlex.split(cmd)
    elif isinstance(cmd, list):
        parts = cmd
    else:
        return jsonify({'message': 'Formato de comando inválido'}), 400

    base = parts[0]
    if base not in ALLOWED_COMMANDS:
        return jsonify({'message': 'Comando no permitido'}), 403

    # Construir una lista segura de argumentos
    # Permitimos solo un subconjunto sencillo — en producción restringir mucho más
    try:
        output = subprocess.check_output(parts, stderr=subprocess.STDOUT, text=True, timeout=30)
        return jsonify({'output': output})
    except subprocess.CalledProcessError as e:
        return jsonify({'error': 'Error al ejecutar comando', 'output': e.output}), 400
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Tiempo de ejecución excedido'}), 408
    except Exception as e:
        logging.exception("execute_command error")
        return jsonify({'error': str(e)}), 500

# -------------------------
# ENDPOINT: download_mp3
# -------------------------
SUPPORTED_DOMAINS = [
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "vimeo.com",
    "dailymotion.com"
]

@app.route('/download_mp3', methods=['POST'])
@token_required
def download_mp3():
    ################ COMPROBAR QUE EXISTE FFMPEG ##########################
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    
    if not ffmpeg_path or not ffprobe_path:
        return jsonify({
            'error': 'ffmpeg/ffprobe no instalados en el servidor'
        }), 500
    
    ffmpeg_dir = os.path.dirname(ffmpeg_path)
    ########################################################################

    data = request.get_json(silent=True) or {}
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'URL no proporcionada'}), 400

    if not any(domain in url for domain in SUPPORTED_DOMAINS):
        return jsonify({'error': 'Plataforma no soportada'}), 400

    # preparar tmpdir manual (no usar TemporaryDirectory si vamos a stream y limpiar después)
    tmp_dir = tempfile.mkdtemp(prefix="ydl_")
    try:
        # decide usar aria2c si está instalado
        external_downloader = shutil.which("aria2c")
        ydl_opts = {
            'format': 'bestaudio/best',
            'cookiefile': 'cookies.txt'
            'nocheckcertificate': True,
            'geo_bypass': True,
            'check_formats': True, 
            'javascript_error_fatal': False,
            'remote_components': ['ejs:github'],
            # escribir thumbnail y metadata, y usar ffmpeg_location
            'writethumbnail': True,
            'addmetadata': True,
            'outtmpl': os.path.join(tmp_dir, '%(title)s.%(ext)s'),
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                },
                {
                    'key': 'EmbedThumbnail',
                },
                {
                    'key': 'FFmpegMetadata',
                },
            ],
            'extractor_args': {
                'youtube': {
                    # Intenta forzar que se comporte como un cliente de Android o TV
                    'player_client': ['android', 'ios'], 
                }
            },
            'quiet': False,
            'no_warnings': False,
            'ffmpeg_location': ffmpeg_dir,
        }

        if external_downloader:
            # si aria2c está disponible, usa conexiones paralelas
            ydl_opts['external_downloader'] = 'aria2c'
            ydl_opts['external_downloader_args'] = ['-x', '16', '-k', '1M']


        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            app.logger.info("yt-dlp info: %s", info.get('id') if isinstance(info, dict) else str(info))

        # localizar el mp3 generado
        mp3_files = [f for f in os.listdir(tmp_dir) if f.lower().endswith('.mp3')]
        if not mp3_files:
            app.logger.error("No se encontró MP3 en %s", tmp_dir)
            # limpiar
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass
            return jsonify({'error': 'No se pudo generar el MP3'}), 500
            
        mp3_path = os.path.join(tmp_dir, mp3_files[0])
        filename = secure_filename(mp3_files[0])

        # Ahora hacemos streaming del archivo al cliente y limpiamos el tmpdir al final
        def generate():
            try:
                with open(mp3_path, 'rb') as fh:
                    while True:
                        chunk = fh.read(64 * 1024)  # 64KB
                        if not chunk:
                            break
                        yield chunk
            finally:
                # cleanup: eliminar tempdir y su contenido
                try:
                    shutil.rmtree(tmp_dir)
                    app.logger.debug("Tempdir %s eliminado", tmp_dir)
                except Exception as e:
                    app.logger.warning("No se pudo limpiar tmpdir %s: %s", tmp_dir, e)

        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
        return Response(stream_with_context(generate()), mimetype='audio/mpeg', headers=headers)

    except yt_dlp.utils.DownloadError as e:
        app.logger.exception("yt-dlp download error")
        # asegurar limpieza si ocurrió antes de llegar a la sección de cleanup
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
        return jsonify({'error': 'Error en la descarga/conversión: ' + str(e)}), 500

    except Exception as e:
        app.logger.exception("download_mp3 error")
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500

@app.route('/playlist_info', methods=['POST'], strict_slashes=False)
@token_required
def playlist_info():
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    app.logger.debug("playlist_info called; url=%s", url)
    if not url:
        return jsonify({'error': 'URL no proporcionada'}), 400

    try:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = []
        if isinstance(info, dict) and 'entries' in info:
            for e in info['entries']:
                if not e:
                    continue
                entries.append({
                    'id': e.get('id'),
                    'title': e.get('title') or e.get('id'),
                    'webpage_url': e.get('url') or e.get('webpage_url') or e.get('id')
                })
        else:
            entries.append({
                'id': info.get('id'),
                'title': info.get('title'),
                'webpage_url': info.get('webpage_url') or info.get('id')
            })

        return jsonify({'entries': entries, 'count': len(entries)})
    except Exception as e:
        app.logger.exception("playlist_info error")
        return jsonify({'error': str(e)}), 500


# -------------------------
# after_request logging (seguro)
# -------------------------
#@app.after_request
#def log_requests(response):
#    payload = None
#    if request.is_json:
#        try:
#            payload = request.get_json(silent=True)
#        except Exception:
#            payload = None
#    app.logger.debug(
#        f"[{request.method}] {request.url} | Payload: {payload} | Status: {response.status_code}"
#    )
#    return response

# -------------------------
# Run (desarrollo)
# -------------------------
if __name__ == '__main__':
    # Para desarrollo local es más simple correr sin ssl_context y usar ngrok o similar.
    # Si quieres SSL local, asegúrate de que cert.pem/key.pem existan.
    debug_mode = os.getenv('FLASK_DEBUG', '1') == '1'
    host = os.getenv('HOST', '0.0.0.0')
    port = int(os.getenv('PORT', '5001'))
    app.run(host=host, port=port, debug=False, threaded=True)
