"""server.py —— MCAP 视频查看器本地服务

只用标准库 + zstandard。启动后浏览器访问 http://127.0.0.1:<port>/
"""

import os
import sys
import json
import time
import html
import shutil
import hashlib
import threading
import subprocess
import mimetypes
import webbrowser
import posixpath
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, 'web')
CACHE_ROOT = os.path.join(HERE, 'cache')

sys.path.insert(0, HERE)
import mcap_reader as MR      # noqa: E402
import prepare as PREP        # noqa: E402

APP_NAME = 'MCAP 视频查看器'

# 文件登记表：file_id -> 状态
FILES = {}
FILES_LOCK = threading.Lock()

# 关掉应用窗口后自动退出（留出宽限期，页面刷新不会误触发）
AUTO_EXIT = {'enabled': True, 'timer': None, 'grace': 3.5}


def schedule_exit():
    if not AUTO_EXIT['enabled']:
        return
    cancel_exit()
    t = threading.Timer(AUTO_EXIT['grace'], lambda: os._exit(0))
    t.daemon = True
    t.start()
    AUTO_EXIT['timer'] = t


def cancel_exit():
    t = AUTO_EXIT['timer']
    if t is not None:
        t.cancel()
        AUTO_EXIT['timer'] = None


def file_id(path):
    try:
        st = os.stat(path)
        raw = '%s|%d|%d' % (os.path.abspath(path), st.st_size, int(st.st_mtime))
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def cache_dir(fid):
    return os.path.join(CACHE_ROOT, fid)


def register(path):
    path = os.path.abspath(path)
    fid = file_id(path)
    with FILES_LOCK:
        entry = FILES.get(fid)
        if entry and entry.get('path') == path:
            return fid, entry
    info = dict(path=path, id=fid, state='idle', progress=0.0, message='',
                manifest=None, error=None, started=None, finished=None)
    try:
        info['summary'] = MR.scan(path)
    except Exception as e:
        info['summary'] = None
        info['error'] = str(e)
        info['state'] = 'error'
    with FILES_LOCK:
        FILES[fid] = info
    return fid, info


def start_prepare(fid):
    with FILES_LOCK:
        entry = FILES.get(fid)
        if not entry:
            return
        if entry['state'] in ('running', 'done'):
            return
        entry['state'] = 'running'
        entry['progress'] = 0.0
        entry['message'] = '准备中…'
        entry['error'] = None
        entry['started'] = time.time()
        path = entry['path']

    def worker():
        outdir = cache_dir(fid)

        def prog(p, msg):
            with FILES_LOCK:
                e = FILES.get(fid)
                if e:
                    e['progress'] = p
                    e['message'] = msg

        try:
            man = PREP.prepare(path, outdir, progress=prog)
            with FILES_LOCK:
                e = FILES[fid]
                e['manifest'] = man
                e['state'] = 'done'
                e['progress'] = 1.0
                e['message'] = '就绪'
                e['finished'] = time.time()
        except Exception as e:
            import traceback
            with FILES_LOCK:
                en = FILES.get(fid)
                if en:
                    en['state'] = 'error'
                    en['error'] = '%s' % e
                    en['message'] = '失败'
                    en['traceback'] = traceback.format_exc()

    threading.Thread(target=worker, daemon=True).start()


def load_cached(fid, path):
    """若缓存已存在，直接使用"""
    outdir = cache_dir(fid)
    man = PREP.load_manifest(outdir)
    if not man:
        return False
    src = (man.get('source') or '').replace('\\', '/')
    if os.path.normcase(src) != os.path.normcase(os.path.abspath(path).replace('\\', '/')):
        return False
    try:
        if man.get('summary', {}).get('size') != os.path.getsize(path):
            return False
    except OSError:
        return False
    with FILES_LOCK:
        e = FILES.get(fid)
        if e:
            e['manifest'] = man
            e['state'] = 'done'
            e['progress'] = 1.0
            e['message'] = '就绪（来自缓存）'
    return True


# ---------------------------------------------------------------- 文件检索
def scan_mcap_files(roots, max_depth=3, limit=400):
    out = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        base_depth = root.rstrip('\\/').count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.rstrip('\\/').count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d not in ('node_modules', 'cache')]
            for fn in filenames:
                if fn.lower().endswith('.mcap'):
                    fp = os.path.join(dirpath, fn)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    try:
                        st = os.stat(fp)
                        out.append(dict(path=fp, name=fn, size=st.st_size,
                                        mtime=int(st.st_mtime),
                                        dir=dirpath))
                    except OSError:
                        pass
                    if len(out) >= limit:
                        return out
    out.sort(key=lambda x: -x['mtime'])
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = 'McapViewer/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if os.environ.get('MCAPVIEW_VERBOSE'):
            sys.stderr.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({'ok': False, 'error': msg}, code)

    def _file(self, path, ctype=None, download=None):
        if not os.path.isfile(path):
            self._err('文件不存在: %s' % os.path.basename(path), 404)
            return
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith('bytes='):
            try:
                spec = rng.split('=', 1)[1].split(',')[0].strip()
                if '-' in spec:
                    a, b = spec.split('-', 1)
                    if a:
                        start = int(a)
                        end = int(b) if b else size - 1
                    else:
                        start = max(0, size - int(b))
                        end = size - 1
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
                partial = True
            except Exception:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if partial:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        if download:
            self.send_header('Content-Disposition',
                             'attachment; filename="%s"' % urllib.parse.quote(download))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        try:
            with open(path, 'rb') as fh:
                fh.seek(start)
                remain = length
                while remain > 0:
                    chunk = fh.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _entry(self, q):
        fid = (q.get('f') or [''])[0]
        with FILES_LOCK:
            e = FILES.get(fid)
        return fid, e

    def _manifest(self, q):
        fid, e = self._entry(q)
        if not e:
            return None, None, '会话已失效，请重新打开文件'
        man = e.get('manifest') or PREP.load_manifest(cache_dir(fid))
        if not man:
            return fid, None, '文件尚未准备好'
        return fid, man, None

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            import traceback
            traceback.print_exc()
            try:
                self._err('服务内部错误: %s' % exc, 500)
            except Exception:
                pass

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        q = urllib.parse.parse_qs(parsed.query)

        if path == '/' or path == '/index.html':
            cancel_exit()
            return self._file(os.path.join(WEB_DIR, 'index.html'), 'text/html; charset=utf-8')

        if path.startswith('/static/'):
            rel = posixpath.normpath(path[len('/static/'):]).lstrip('/')
            fp = os.path.join(WEB_DIR, *rel.split('/'))
            if not os.path.abspath(fp).startswith(os.path.abspath(WEB_DIR)):
                return self._err('非法路径', 403)
            return self._file(fp)

        if path == '/api/env':
            cancel_exit()
            return self._json(dict(
                ok=True, app=APP_NAME,
                python=sys.executable,
                version=sys.version.split()[0],
                zstd=bool(MR._zstd), lz4=bool(MR._lz4),
                roots=DEFAULT_ROOTS,
                cwd=os.getcwd(),
            ))

        if path == '/api/files':
            roots = q.get('dir') or DEFAULT_ROOTS
            return self._json(dict(ok=True, roots=roots,
                                   files=scan_mcap_files(roots)))

        if path == '/api/open':
            target = (q.get('path') or [''])[0]
            if not target:
                return self._err('缺少 path 参数')
            target = os.path.abspath(target)
            if not os.path.isfile(target):
                return self._err('文件不存在: %s' % target)
            if not target.lower().endswith('.mcap'):
                return self._err('只支持 .mcap 文件')
            fid, entry = register(target)
            if entry['state'] != 'done':
                if not load_cached(fid, target):
                    start_prepare(fid)
            with FILES_LOCK:
                e = FILES[fid]
                state = e['state']
                err = e.get('error')
            return self._json(dict(ok=True, id=fid, state=state, error=err,
                                   summary=e.get('summary')))

        if path == '/api/status':
            fid, e = self._entry(q)
            if not e:
                return self._json(dict(ok=False, error='未知会话'), 404)
            man = e.get('manifest')
            cams = []
            if man:
                cams = [dict(key=c['key'], topic=c['topic'], frames=c['frames'],
                             playable=c['playable'], kind=c['kind'],
                             error=c.get('error'), width=c.get('width'),
                             height=c.get('height')) for c in man['cameras']]
            return self._json(dict(
                ok=True, id=fid, state=e['state'], progress=e['progress'],
                message=e['message'], error=e.get('error'),
                cameras=cams,
                duration=(man or {}).get('duration') or (e.get('summary') or {}).get('duration'),
            ))

        if path == '/api/manifest':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            return self._json(dict(ok=True, manifest=man))

        if path == '/api/video':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            key = (q.get('cam') or [''])[0]
            cam = next((c for c in man['cameras'] if c['key'] == key), None)
            if not cam:
                return self._err('找不到相机 %s' % key, 404)
            if cam['kind'] != 'mp4':
                return self._err('该通道不是视频流', 400)
            return self._file(os.path.join(cache_dir(fid), cam['file']),
                              'video/mp4', download=q.get('dl', [None])[0])

        if path == '/api/raw':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            key = (q.get('cam') or [''])[0]
            cam = next((c for c in man['cameras'] if c['key'] == key), None)
            if not cam or not cam.get('source_raw'):
                return self._err('没有原始码流', 404)
            return self._file(os.path.join(cache_dir(fid), cam['source_raw']),
                              'application/octet-stream',
                              download='%s.%s' % (key, 'h264'))

        if path == '/api/frame':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            key = (q.get('cam') or [''])[0]
            cam = next((c for c in man['cameras'] if c['key'] == key), None)
            if not cam:
                return self._err('找不到相机 %s' % key, 404)
            if cam['kind'] != 'images':
                return self._err('该通道不是图片序列', 400)
            try:
                i = int((q.get('i') or ['0'])[0])
            except ValueError:
                i = 0
            lst = cam.get('frames_list') or []
            if not (0 <= i < len(lst)):
                return self._err('帧号越界', 404)
            fp = os.path.join(cache_dir(fid), cam['dir'], lst[i])
            ct = 'image/png' if fp.lower().endswith('.png') else 'image/jpeg'
            return self._file(fp, ct)

        if path == '/api/imu':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            fp = os.path.join(cache_dir(fid), 'imu.json')
            if not os.path.isfile(fp):
                return self._json(dict(ok=False, error='该文件不含 IMU 数据'), 404)
            return self._file(fp, 'application/json; charset=utf-8')

        if path == '/api/audio':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            fp = os.path.join(cache_dir(fid), 'audio.wav')
            if not os.path.isfile(fp):
                return self._json(dict(ok=False, error='该文件不含音频'), 404)
            return self._file(fp, 'audio/wav', download=q.get('dl', [None])[0])

        if path == '/api/imu.csv':
            fid, man, err = self._manifest(q)
            if err:
                return self._json(dict(ok=False, error=err), 409)
            fp = os.path.join(cache_dir(fid), 'imu.json')
            if not os.path.isfile(fp):
                return self._err('该文件不含 IMU 数据', 404)
            with open(fp, encoding='utf-8') as fh:
                d = json.load(fh)
            lines = ['time_s,gyro_x,gyro_y,gyro_z,acc_x,acc_y,acc_z']
            for i, t in enumerate(d['t']):
                lines.append('%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f' % (
                    t, d['av'][0][i], d['av'][1][i], d['av'][2][i],
                    d['la'][0][i], d['la'][1][i], d['la'][2][i]))
            body = ('\n'.join(lines)).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition',
                             'attachment; filename="imu.csv"')
            self.end_headers()
            self.wfile.write(body)
            return

        if path == '/api/pick':
            return self._json(pick_file_dialog())

        if path == '/api/cache/clear':
            freed = clear_cache()
            return self._json(dict(ok=True, freed=freed))

        if path == '/api/shutdown':
            self._json(dict(ok=True))
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                             daemon=True).start()
            return

        self._err('未知接口: %s' % path, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/upload':
            return self._upload(parsed)
        if parsed.path == '/api/leave':
            # 由页面关闭时 sendBeacon 触发：等宽限期，若期间没有新请求就退出
            cancel_exit()
            schedule_exit()
            try:
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
            except Exception:
                pass
            return
        if parsed.path == '/api/shutdown':
            self._json(dict(ok=True))
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                             daemon=True).start()
            return
        self._err('未知接口', 404)

    def _upload(self, parsed):
        q = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote((q.get('name') or ['upload.mcap'])[0])
        name = os.path.basename(name) or 'upload.mcap'
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._err('上传内容为空')
        updir = os.path.join(HERE, 'uploads')
        os.makedirs(updir, exist_ok=True)
        stem, ext = os.path.splitext(name)
        target = os.path.join(updir, name)
        n = 1
        while os.path.exists(target):
            target = os.path.join(updir, '%s(%d)%s' % (stem, n, ext))
            n += 1
        remain = length
        with open(target, 'wb') as fh:
            while remain > 0:
                chunk = self.rfile.read(min(1048576, remain))
                if not chunk:
                    break
                fh.write(chunk)
                remain -= len(chunk)
        fid, entry = register(target)
        if entry['state'] != 'done' and not load_cached(fid, target):
            start_prepare(fid)
        self._json(dict(ok=True, id=fid, path=target, state=entry['state']))


# ---------------------------------------------------------------- 选择文件
def pick_file_dialog():
    code = (
        'import tkinter as tk\n'
        'from tkinter import filedialog\n'
        'r = tk.Tk()\n'
        'r.withdraw()\n'
        'try:\n'
        '    r.attributes("-topmost", True)\n'
        'except Exception:\n'
        '    pass\n'
        'p = filedialog.askopenfilename(title="选择 MCAP 文件",\n'
        '    filetypes=[("MCAP 视频文件", "*.mcap"), ("所有文件", "*.*")])\n'
        'print(p or "")\n'
    )
    try:
        r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                           text=True, timeout=600)
        p = (r.stdout or '').strip().splitlines()
        p = p[-1].strip() if p else ''
        if not p:
            return dict(ok=False, canceled=True)
        return dict(ok=True, path=p)
    except Exception as e:
        return dict(ok=False, error='无法打开文件选择框：%s' % e)


def clear_cache():
    freed = 0
    if not os.path.isdir(CACHE_ROOT):
        return 0
    for name in os.listdir(CACHE_ROOT):
        fp = os.path.join(CACHE_ROOT, name)
        try:
            for dirpath, _dirs, files in os.walk(fp):
                for f in files:
                    freed += os.path.getsize(os.path.join(dirpath, f))
            shutil.rmtree(fp, ignore_errors=True)
        except OSError:
            pass
    return freed


DEFAULT_ROOTS = []


def find_browser():
    """优先找 Chrome / Edge，用 --app 模式开出无地址栏的独立窗口"""
    local = os.environ.get('LOCALAPPDATA', '')
    cands = [
        os.path.join(local, r'Google\Chrome\Application\chrome.exe'),
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        os.path.join(local, r'Microsoft\Edge\Application\msedge.exe'),
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def open_window(url, mode='app'):
    """mode: app(独立窗口) / tab(默认浏览器标签页) / none(不打开)"""
    if mode == 'none':
        return 'none'
    if mode == 'app':
        exe = find_browser()
        if exe:
            args = [exe,
                    '--app=' + url,
                    '--window-size=1680,1020',
                    '--window-position=60,40',
                    '--no-first-run',
                    '--no-default-browser-check']
            try:
                if os.name == 'nt':
                    subprocess.Popen(args, close_fds=True,
                                     creationflags=0x00000008)  # DETACHED_PROCESS
                else:
                    subprocess.Popen(args, close_fds=True,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                return 'app:' + os.path.basename(exe)
            except Exception:
                pass
    try:
        webbrowser.open(url)
        return 'tab'
    except Exception:
        return 'failed'


def build_roots(extra=None):
    roots = []
    for cand in [os.getcwd(), os.path.dirname(HERE), HERE,
                 os.path.join(os.path.expanduser('~'), 'Desktop'),
                 os.path.join(os.path.expanduser('~'), 'Downloads'),
                 os.path.join(os.path.expanduser('~'), 'Documents')]:
        if cand and os.path.isdir(cand) and cand not in roots:
            roots.append(cand)
    for e in (extra or []):
        if os.path.isdir(e) and e not in roots:
            roots.append(e)
    return roots


def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--open-mode', choices=['app', 'tab', 'none'], default='app',
                    help='app=独立应用窗口(默认) tab=浏览器标签页 none=不自动打开')
    ap.add_argument('--no-auto-exit', action='store_true',
                    help='关闭应用窗口后不自动退出服务')
    ap.add_argument('--dir', action='append', default=[],
                    help='额外扫描的目录（可多次指定）')
    ap.add_argument('file', nargs='?', help='启动后自动打开的 mcap 文件')
    args = ap.parse_args()

    global DEFAULT_ROOTS
    DEFAULT_ROOTS = build_roots(args.dir)
    AUTO_EXIT['enabled'] = not args.no_auto_exit
    os.makedirs(CACHE_ROOT, exist_ok=True)

    port = args.port
    httpd = None
    for cand in ([port] if port else [8765, 8766, 8767, 8770, 8781, 8790, 0]):
        try:
            httpd = ThreadingHTTPServer(('127.0.0.1', cand), Handler)
            port = httpd.server_address[1]
            break
        except OSError:
            continue
    if httpd is None:
        print('无法绑定端口')
        return 1

    url = 'http://127.0.0.1:%d/' % port
    if args.file and os.path.isfile(args.file):
        fid, entry = register(os.path.abspath(args.file))
        url += '?open=' + urllib.parse.quote(os.path.abspath(args.file))
        if entry['state'] != 'done' and not load_cached(fid, os.path.abspath(args.file)):
            start_prepare(fid)

    print('=' * 58)
    print('  已启动：%s' % APP_NAME)
    print('  地址：%s' % url)
    print('  数据目录：%s' % CACHE_ROOT)
    print('  关闭方式：关闭程序窗口，或在本窗口按 Ctrl+C')
    print('=' * 58)

    opmode = 'none' if args.no_browser else args.open_mode
    if opmode != 'none':
        threading.Timer(0.6, lambda: open_window(url, opmode)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
