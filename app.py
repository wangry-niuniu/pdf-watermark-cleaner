from __future__ import annotations

import io
import json
import os
import time
import uuid
from pathlib import Path

import fitz
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
PREVIEW_DIR = BASE_DIR / "previews"

for directory in (UPLOAD_DIR, OUTPUT_DIR, PREVIEW_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


@app.after_request
def disable_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def allowed_file(filename: str) -> bool:
    return filename.lower().endswith(".pdf")


def session_paths(session_id: str) -> dict[str, Path]:
    return {
        "upload": UPLOAD_DIR / f"{session_id}.pdf",
        "meta": UPLOAD_DIR / f"{session_id}.json",
    }


def parse_page_spec(spec: str, page_count: int) -> list[int]:
    spec = spec.strip()
    if not spec:
        raise ValueError("请填写要处理的页码。")

    pages: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            if start > end:
                start, end = end, start
            for page_num in range(start, end + 1):
                if 1 <= page_num <= page_count:
                    pages.add(page_num - 1)
        else:
            page_num = int(token)
            if 1 <= page_num <= page_count:
                pages.add(page_num - 1)

    if not pages:
        raise ValueError("页码范围无效，超出了 PDF 总页数。")
    return sorted(pages)


def preview_path(session_id: str, page_index: int) -> Path:
    return PREVIEW_DIR / f"{session_id}-page-{page_index + 1}.png"


def create_preview(doc: fitz.Document, page_index: int, output_path: Path) -> dict[str, float]:
    page = doc[page_index]
    rect = page.rect
    scale = min(1000 / rect.width, 1400 / rect.height)
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    pixmap.save(output_path)
    return {
        "page_width": rect.width,
        "page_height": rect.height,
        "preview_width": pixmap.width,
        "preview_height": pixmap.height,
    }


def erase_regions(
    input_path: Path,
    output_path: Path,
    region_specs: list[dict[str, object]],
    page_count: int,
) -> None:
    doc = fitz.open(input_path)

    try:
        for spec in region_specs:
            rect = fitz.Rect(
                float(spec["x0"]),
                float(spec["y0"]),
                float(spec["x1"]),
                float(spec["y1"]),
            )
            target_mode = str(spec["target_mode"])
            source_page = int(spec["source_page"])

            if target_mode == "all":
                page_indexes = list(range(page_count))
            elif target_mode == "same_page":
                page_indexes = [source_page]
            elif target_mode == "custom":
                page_indexes = [int(page_index) for page_index in spec["page_indexes"]]
            else:
                continue

            for page_index in page_indexes:
                page = doc[page_index]
                rect = rect & page.rect
                if rect.is_empty:
                    continue
                page.draw_rect(
                    rect,
                    color=(1, 1, 1),
                    fill=(1, 1, 1),
                    overlay=True,
                    width=0,
                )

        doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload_pdf():
    uploaded = request.files.get("pdf")
    if uploaded is None or uploaded.filename == "":
        return jsonify({"error": "请先拖入或选择一个 PDF 文件。"}), 400

    if not allowed_file(uploaded.filename):
        return jsonify({"error": "目前只支持 PDF 文件。"}), 400

    session_id = uuid.uuid4().hex
    safe_name = secure_filename(Path(uploaded.filename).name) or "document.pdf"
    paths = session_paths(session_id)
    uploaded.save(paths["upload"])

    try:
        doc = fitz.open(paths["upload"])
        first_preview = preview_path(session_id, 0)
        preview_meta = create_preview(doc, 0, first_preview)
        meta = {
            "session_id": session_id,
            "original_name": safe_name,
            "page_count": len(doc),
            "default_preview": preview_meta,
        }
        paths["meta"].write_text(json.dumps(meta), encoding="utf-8")
    except Exception as exc:
        for path in paths.values():
            if path.exists():
                path.unlink()
        return jsonify({"error": f"文件解析失败：{exc}"}), 400
    finally:
        try:
            doc.close()
        except Exception:
            pass

    return jsonify(
        {
            "session_id": session_id,
            "original_name": safe_name,
            "page_count": meta["page_count"],
            "preview_url": f"/api/preview/{session_id}?page=1",
            "preview_width": meta["default_preview"]["preview_width"],
            "preview_height": meta["default_preview"]["preview_height"],
        }
    )


@app.get("/api/preview/<session_id>")
def preview(session_id: str):
    paths = session_paths(session_id)
    if not paths["upload"].exists() or not paths["meta"].exists():
        return jsonify({"error": "预览不存在，请重新上传文件。"}), 404

    meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
    page_count = int(meta["page_count"])
    page_number = request.args.get("page", "1").strip()

    try:
        page_index = int(page_number) - 1
    except ValueError:
        return jsonify({"error": "页码无效。"}), 400

    if page_index < 0 or page_index >= page_count:
        return jsonify({"error": "页码超出范围。"}), 400

    current_preview = preview_path(session_id, page_index)
    doc = fitz.open(paths["upload"])
    try:
        page = doc[page_index]
        page_rect = page.rect
        if not current_preview.exists():
            preview_meta = create_preview(doc, page_index, current_preview)
        else:
            pix = fitz.Pixmap(current_preview)
            preview_meta = {
                "page_width": page_rect.width,
                "page_height": page_rect.height,
                "preview_width": pix.width,
                "preview_height": pix.height,
            }
            pix = None
    finally:
        doc.close()

    response = send_file(current_preview, mimetype="image/png")
    response.headers["X-Page-Width"] = str(preview_meta["page_width"])
    response.headers["X-Page-Height"] = str(preview_meta["page_height"])
    response.headers["X-Preview-Width"] = str(preview_meta["preview_width"])
    response.headers["X-Preview-Height"] = str(preview_meta["preview_height"])
    return response


@app.post("/api/process")
def process():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    regions = payload.get("regions") or []

    if not session_id:
        return jsonify({"error": "缺少会话信息，请重新上传 PDF。"}), 400

    paths = session_paths(session_id)
    if not paths["upload"].exists() or not paths["meta"].exists():
        return jsonify({"error": "文件已失效，请重新上传。"}), 400

    if not isinstance(regions, list) or not regions:
        return jsonify({"error": "请先在预览图上框选要擦除的区域。"}), 400

    meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
    page_count = int(meta["page_count"])
    normalized_regions: list[dict[str, object]] = []
    for region in regions:
        try:
            source_page = int(region["source_page"])
            preview_width = float(region["preview_width"])
            preview_height = float(region["preview_height"])
            page_width = float(region["page_width"])
            page_height = float(region["page_height"])
            x0 = max(0.0, min(page_width, float(region["x"]) / preview_width * page_width))
            y0 = max(0.0, min(page_height, float(region["y"]) / preview_height * page_height))
            x1 = max(0.0, min(page_width, (float(region["x"]) + float(region["width"])) / preview_width * page_width))
            y1 = max(0.0, min(page_height, (float(region["y"]) + float(region["height"])) / preview_height * page_height))
            target_mode = str(region["target_mode"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "擦除区域数据不正确，请重新框选。"}), 400

        if source_page < 0 or source_page >= page_count:
            return jsonify({"error": "区域来源页码无效。"}), 400
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue

        if target_mode == "custom":
            page_spec = str(region.get("page_spec", "")).strip()
            try:
                page_indexes = parse_page_spec(page_spec, page_count)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        elif target_mode == "all":
            page_indexes = []
        else:
            target_mode = "same_page"
            page_indexes = [source_page]

        normalized_regions.append(
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "source_page": source_page,
                "target_mode": target_mode,
                "page_indexes": page_indexes,
            }
        )

    if not normalized_regions:
        return jsonify({"error": "框选区域过小，请重新选择。"}), 400

    stamp = time.strftime("%Y%m%d-%H%M%S")
    original_stem = Path(meta["original_name"]).stem
    output_path = OUTPUT_DIR / f"{original_stem}_cleaned_regions_{stamp}.pdf"

    try:
        erase_regions(paths["upload"], output_path, normalized_regions, page_count)
    except Exception as exc:
        return jsonify({"error": f"处理失败：{exc}"}), 500

    return jsonify(
        {
            "download_url": f"/api/download/{output_path.name}",
            "filename": output_path.name,
        }
    )


@app.get("/api/download/<filename>")
def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"error": "结果文件不存在。"}), 404
    return send_file(
        io.BytesIO(path.read_bytes()),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=path.name,
    )


if __name__ == "__main__":
    host = os.environ.get("PDF_CLEANER_HOST", "127.0.0.1")
    port = int(os.environ.get("PDF_CLEANER_PORT", "5050"))
    app.run(host=host, port=port, debug=False)
