from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, abort
import os
from PIL import Image, UnidentifiedImageError

app = Flask(__name__)
DATA_FOLDER = os.environ.get('DATA_FOLDER', '/data')
THUMBNAIL_CACHE_DIR = os.path.join(DATA_FOLDER, '.thumb_cache')
THUMBNAIL_SIZE = (400, 400)
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp')

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Miso Gallery</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: #0d0d0d;
            color: #e0e0e0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh;
        }
        header {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 20px 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #333;
        }
        h1 { font-size: 1.5rem; background: linear-gradient(90deg, #f5a623, #f76c1c); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .breadcrumb { color: #888; font-size: 0.9rem; }
        .breadcrumb a { color: #f5a623; text-decoration: none; }
        .container { padding: 20px; }
        .toolbar {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 15px;
        }
        .toolbar button {
            background: #2a2a2a;
            color: #f0f0f0;
            border: 1px solid #444;
            border-radius: 6px;
            padding: 8px 12px;
            cursor: pointer;
            font-size: 0.85rem;
        }
        .toolbar button:hover { border-color: #f5a623; }
        .toolbar .danger {
            background: #a52834;
            border-color: #dc3545;
        }
        .toolbar .danger:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 15px;
        }
        .folder, .image-card {
            background: #1a1a1a;
            border-radius: 10px;
            overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s;
            cursor: pointer;
        }
        .folder:hover, .image-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 25px rgba(245, 166, 35, 0.15);
        }
        .folder {
            padding: 30px;
            text-align: center;
            border: 1px dashed #444;
        }
        .folder-icon { font-size: 3rem; margin-bottom: 10px; }
        .folder-name { color: #f5a623; font-weight: 500; }
        .image-card {
            position: relative;
            border: 1px solid transparent;
        }
        .image-card.selected {
            border-color: #f5a623;
            box-shadow: 0 0 0 2px rgba(245, 166, 35, 0.3);
        }
        .image-card img {
            width: 100%;
            height: 180px;
            object-fit: cover;
            display: block;
        }
        .image-info {
            padding: 10px;
            font-size: 0.8rem;
            color: #888;
        }
        .image-name {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .delete-btn {
            position: absolute;
            top: 10px;
            right: 10px;
            background: rgba(220, 53, 69, 0.9);
            color: white;
            border: none;
            padding: 8px 12px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 0.8rem;
            opacity: 0;
            transition: opacity 0.2s;
        }
        .image-card:hover .delete-btn { opacity: 1; }
        .delete-btn:hover { background: #dc3545; }
        .selector {
            position: absolute;
            top: 10px;
            left: 10px;
            z-index: 2;
            transform: scale(1.2);
            cursor: pointer;
        }
        .empty { text-align: center; padding: 50px; color: #666; }
        .stats { color: #666; font-size: 0.85rem; margin-top: 20px; text-align: center; }
    </style>
</head>
<body>
    <header>
        <h1>🍲 Miso Gallery</h1>
        <div class="breadcrumb">{{ breadcrumb|safe }}</div>
    </header>
    <div class="container">
        {% if items %}
        <form id="bulkDeleteForm" method="POST" action="/bulk-delete">
            <input type="hidden" name="current_subpath" value="{{ current_subpath }}">
            <div class="toolbar">
                <button type="button" id="selectAllBtn">Select all</button>
                <button type="button" id="deselectAllBtn">Deselect all</button>
                <button type="submit" id="bulkDeleteBtn" class="danger" disabled onclick="return confirmBulkDelete()">Delete selected (0)</button>
            </div>
            <div class="grid">
                {% for item in items %}
                {% if item.is_dir %}
                <a href="{{ item.url }}" class="folder">
                    <div class="folder-icon">📁</div>
                    <div class="folder-name">{{ item.name }}</div>
                </a>
                {% else %}
                <div class="image-card" data-image-card>
                    <input class="selector" type="checkbox" name="filenames" value="{{ item.rel_path }}" onchange="syncSelectionState()">
                    <a href="{{ item.view_url }}" target="_blank">
                        <img src="{{ item.thumb_url }}" alt="{{ item.name }}" loading="lazy">
                    </a>
                    <div class="image-info">
                        <div class="image-name">{{ item.name }}</div>
                        <div>{{ item.size }}</div>
                    </div>
                    <button type="submit" class="delete-btn" formaction="{{ item.delete_url }}" formmethod="POST" onclick="return confirm('Delete {{ item.name }}?')">🗑️</button>
                </div>
                {% endif %}
                {% endfor %}
            </div>
        </form>
        {% else %}
        <div class="empty">No images in this folder</div>
        {% endif %}
        <div class="stats">{{ stats.folders }} folders • {{ stats.images }} images</div>
    </div>
    <script>
        function getSelectors() {
            return Array.from(document.querySelectorAll('input.selector[name="filenames"]'));
        }

        function syncSelectionState() {
            const selectors = getSelectors();
            const selectedCount = selectors.filter(s => s.checked).length;
            const bulkDeleteBtn = document.getElementById('bulkDeleteBtn');

            selectors.forEach((selector) => {
                const card = selector.closest('[data-image-card]');
                if (card) {
                    card.classList.toggle('selected', selector.checked);
                }
            });

            if (bulkDeleteBtn) {
                bulkDeleteBtn.disabled = selectedCount === 0;
                bulkDeleteBtn.textContent = `Delete selected (${selectedCount})`;
            }
        }

        function setAllSelections(checked) {
            getSelectors().forEach((selector) => {
                selector.checked = checked;
            });
            syncSelectionState();
        }

        function confirmBulkDelete() {
            const selectedCount = getSelectors().filter(s => s.checked).length;
            if (selectedCount === 0) return false;
            return confirm(`Delete ${selectedCount} selected image(s)?`);
        }

        const selectAllBtn = document.getElementById('selectAllBtn');
        const deselectAllBtn = document.getElementById('deselectAllBtn');

        if (selectAllBtn) {
            selectAllBtn.addEventListener('click', () => setAllSelections(true));
        }
        if (deselectAllBtn) {
            deselectAllBtn.addEventListener('click', () => setAllSelections(false));
        }

        syncSelectionState();
    </script>
</body>
</html>
'''


def ensure_thumbnail_cache_dir():
    os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)


def sanitize_rel_path(rel_path: str) -> str:
    normalized = os.path.normpath(rel_path).replace('\\', '/').lstrip('/')
    if normalized.startswith('..'):
        abort(404)
    return normalized


def source_file_path(rel_path: str) -> str:
    safe_rel_path = sanitize_rel_path(rel_path)
    return os.path.join(DATA_FOLDER, safe_rel_path)


def thumbnail_filename(rel_path: str, source_path: str) -> str:
    stat = os.stat(source_path)
    safe_name = rel_path.replace('/', '__')
    return f"{safe_name}.{stat.st_mtime_ns}.{stat.st_size}.jpg"


def generate_thumbnail(source_path: str, output_path: str):
    with Image.open(source_path) as img:
        img = img.convert('RGB')
        img.thumbnail(THUMBNAIL_SIZE)
        img.save(output_path, format='JPEG', quality=85, optimize=True)


def remove_thumbnail_cache_for(rel_path: str):
    ensure_thumbnail_cache_dir()
    safe_name = sanitize_rel_path(rel_path).replace('/', '__')
    for cached_file in os.listdir(THUMBNAIL_CACHE_DIR):
        if cached_file.startswith(f"{safe_name}."):
            try:
                os.remove(os.path.join(THUMBNAIL_CACHE_DIR, cached_file))
            except OSError:
                pass


@app.route('/')
@app.route('/<path:subpath>')
def index(subpath=''):
    safe_subpath = sanitize_rel_path(subpath) if subpath else ''
    folder_path = os.path.join(DATA_FOLDER, safe_subpath)

    if not os.path.exists(folder_path):
        return "Folder not found", 404

    items = []
    stats = {'folders': 0, 'images': 0}

    try:
        for item in sorted(os.listdir(folder_path)):
            if item == '.thumb_cache':
                continue

            item_path = os.path.join(folder_path, item)
            rel_path = os.path.join(safe_subpath, item) if safe_subpath else item
            rel_path = rel_path.replace('\\', '/')

            if os.path.isdir(item_path):
                stats['folders'] += 1
                items.append({
                    'name': item,
                    'url': url_for('index', subpath=rel_path),
                    'is_dir': True
                })
            elif item.lower().endswith(IMAGE_EXTENSIONS):
                stats['images'] += 1
                items.append({
                    'name': item,
                    'rel_path': rel_path,
                    'thumb_url': url_for('thumb', filename=rel_path),
                    'view_url': url_for('view', filename=rel_path),
                    'delete_url': url_for('delete', filename=rel_path),
                    'size': format_size(os.path.getsize(item_path)),
                    'is_dir': False
                })
    except Exception as e:
        return f"Error: {e}", 500

    if safe_subpath:
        parts = safe_subpath.split('/')
        crumbs = ['<a href="/">Home</a>']
        for i, part in enumerate(parts[:-1]):
            path = '/'.join(parts[:i + 1])
            crumbs.append(f'<a href="/{path}">{part}</a>')
        crumbs.append(parts[-1])
        breadcrumb = ' / '.join(crumbs)
    else:
        breadcrumb = 'All Images'

    return render_template_string(
        HTML_TEMPLATE,
        items=items,
        breadcrumb=breadcrumb,
        stats=stats,
        current_subpath=safe_subpath,
    )


@app.route('/thumb/<path:filename>')
def thumb(filename):
    rel_path = sanitize_rel_path(filename)
    source_path = source_file_path(rel_path)

    if not os.path.exists(source_path) or not source_path.lower().endswith(IMAGE_EXTENSIONS):
        return "Not found", 404

    ensure_thumbnail_cache_dir()
    cached_name = thumbnail_filename(rel_path, source_path)
    cached_path = os.path.join(THUMBNAIL_CACHE_DIR, cached_name)

    if not os.path.exists(cached_path):
        try:
            generate_thumbnail(source_path, cached_path)
        except (UnidentifiedImageError, OSError):
            return send_from_directory(DATA_FOLDER, rel_path)

    return send_from_directory(THUMBNAIL_CACHE_DIR, cached_name)


@app.route('/view/<path:filename>')
def view(filename):
    rel_path = sanitize_rel_path(filename)
    return send_from_directory(DATA_FOLDER, rel_path)


@app.route('/delete/<path:filename>', methods=['POST'])
def delete(filename):
    rel_path = sanitize_rel_path(filename)
    file_path = source_file_path(rel_path)

    if os.path.exists(file_path):
        os.remove(file_path)
        remove_thumbnail_cache_for(rel_path)

    folder = os.path.dirname(rel_path)
    return redirect(url_for('index', subpath=folder if folder else ''))


@app.route('/bulk-delete', methods=['POST'])
def bulk_delete():
    current_subpath = sanitize_rel_path(request.form.get('current_subpath', '')) if request.form.get('current_subpath') else ''
    selected = request.form.getlist('filenames')

    for rel_path in selected:
        safe_rel_path = sanitize_rel_path(rel_path)
        file_path = source_file_path(safe_rel_path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)
            remove_thumbnail_cache_for(safe_rel_path)

    return redirect(url_for('index', subpath=current_subpath))


def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
