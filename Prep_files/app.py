import os
from flask import Flask, render_template, send_file, jsonify, Response
from pathlib import Path

app = Flask(__name__)

HOST = os.getenv("NEXA_HOST", "0.0.0.0")
PORT = int(os.getenv("NEXA_PORT", "5000"))
PUBLIC_URL = os.getenv("NEXA_PUBLIC_URL", f"http://localhost:{PORT}")

# Output directories
OUTPUT_DIRS = [
    Path('bulk_output_GR12_biology_ai'),
    Path('bulk_output_local'),
    Path('bulk_output_full_local'),
]

def get_pdf_files():
    """Scan all output directories for PDF files."""
    pdfs = []
    for output_dir in OUTPUT_DIRS:
        if output_dir.exists():
            for pdf_file in output_dir.glob('*.pdf'):
                pdfs.append({
                    'name': pdf_file.name,
                    'path': str(pdf_file),
                    'size': f"{pdf_file.stat().st_size / (1024 * 1024):.2f} MB",
                    'folder': output_dir.name,
                })
    return sorted(pdfs, key=lambda x: x['name'])

@app.route('/')
def index():
    """Display available PDFs for download."""
    pdfs = get_pdf_files()
    return render_template(
        'index.html',
        pdfs=pdfs,
        embed_mode=False,
        public_url=PUBLIC_URL,
        iframe_src=f"{PUBLIC_URL.rstrip('/')}/embed",
    )


@app.route('/embed')
def embed_view():
    """Compact LMS-friendly embed view."""
    pdfs = get_pdf_files()
    return render_template(
        'index.html',
        pdfs=pdfs,
        embed_mode=True,
        public_url=PUBLIC_URL,
        iframe_src=f"{PUBLIC_URL.rstrip('/')}/embed",
    )


@app.route('/lms-snippet')
def lms_snippet():
    """Return a copy-paste iframe snippet for LMS pages."""
    embed_url = f"{PUBLIC_URL.rstrip('/')}/embed"
    snippet = (
        '<iframe '
        f'src="{embed_url}" '
        'title="Nexa Lecture Notes" '
        'style="width:100%;height:900px;border:0;border-radius:12px;overflow:hidden;" '
        'loading="lazy" '
        'allowfullscreen>'
        '</iframe>'
    )
    return Response(snippet, mimetype='text/plain')


@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok", "public_url": PUBLIC_URL})

@app.route('/api/pdfs')
def list_pdfs():
    """API endpoint to list all PDFs."""
    pdfs = get_pdf_files()
    return jsonify(pdfs)

@app.route('/download/<folder>/<filename>')
def download_file(folder, filename):
    """Download a PDF file."""
    file_path = Path(folder) / filename
    
    # Security: ensure the file is within allowed directories
    if not any(file_path.resolve().is_relative_to(d.resolve()) for d in OUTPUT_DIRS):
        return "File not found", 404
    
    if file_path.exists() and file_path.suffix == '.pdf':
        return send_file(file_path, as_attachment=True, download_name=filename)
    
    return "File not found", 404

if __name__ == '__main__':
    print("Starting Nexa PDF Download Server...")
    print(f"Open {PUBLIC_URL} in your browser")
    print(f"LMS iframe snippet available at {PUBLIC_URL.rstrip('/')}/lms-snippet")
    app.run(debug=True, host=HOST, port=PORT)
