from flask import Flask, request, render_template, send_file
from PIL import Image, ImageOps
from io import BytesIO
from dotenv import load_dotenv
import requests
import cloudinary
import cloudinary.uploader
import cloudinary.utils
import os
import hashlib

app = Flask(__name__)

# Server-side cache: {cache_id: {image_hash: png_bytes}}
PROCESSED_IMAGE_CACHE = {}


def get_image_hash(image_bytes):
    """Generate a hash for an image based on its content."""
    return hashlib.md5(image_bytes).hexdigest()

# Load variables from .env for local development.
load_dotenv()

REMOVE_BG_API_KEY = os.getenv("REMOVE_BG_API_KEY")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)


@app.route("/")
def index():
    return render_template("index.html")


def process_single_image(input_image_bytes, cache_id=None, image_hash=None):
    """Remove background, enhance, and return a ready-to-paste passport PIL image."""
    if not REMOVE_BG_API_KEY:
        raise ValueError("missing_remove_bg_api_key")

    # Check if we have a cached enhanced image
    cache_bucket = PROCESSED_IMAGE_CACHE.get(cache_id, {}) if cache_id else {}
    if image_hash and image_hash in cache_bucket:
        print(f"DEBUG: Using cached image for hash {image_hash}")
        img_data = cache_bucket[image_hash]
        return Image.open(BytesIO(img_data))

    # Step 1: Background removal
    response = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        files={"image_file": input_image_bytes},
        data={"size": "auto"},
        headers={"X-Api-Key": REMOVE_BG_API_KEY},
    )

    if response.status_code != 200:
        try:
            error_info = response.json()
            if error_info.get("errors"):
                error_code = error_info["errors"][0].get("code", "unknown_error")
                raise ValueError(f"bg_removal_failed:{error_code}:{response.status_code}")
        except ValueError:
            raise
        except Exception:
            pass
        raise ValueError(f"bg_removal_failed:unknown:{response.status_code}")

    bg_removed = BytesIO(response.content)
    img = Image.open(bg_removed)

    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        processed_img = background
    else:
        processed_img = img.convert("RGB")

    # Step 2: Upload to Cloudinary
    buffer = BytesIO()
    processed_img.save(buffer, format="PNG")
    buffer.seek(0)
    upload_result = cloudinary.uploader.upload(buffer, resource_type="image")
    image_url = upload_result.get("secure_url")
    public_id = upload_result.get("public_id")

    if not image_url:
        raise ValueError("cloudinary_upload_failed")

    # Step 3: Enhance via Cloudinary AI
    enhanced_url = cloudinary.utils.cloudinary_url(
        public_id,
        transformation=[
            {"effect": "gen_restore"},
            {"quality": "auto"},
            {"fetch_format": "auto"},
        ],
    )[0]

    enhanced_img_data = requests.get(enhanced_url).content
    img = Image.open(BytesIO(enhanced_img_data))

    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        passport_img = background
    else:
        passport_img = img.convert("RGB")

    # Step 4: Delete the resource from Cloudinary to save storage
    print(f"DEBUG: Deleting Cloudinary resource {public_id}")
    try:
        cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"WARNING: Failed to delete Cloudinary resource {public_id}: {e}")

    # Step 5: Cache the enhanced image on server-side memory
    if cache_id and image_hash:
        if cache_id not in PROCESSED_IMAGE_CACHE:
            PROCESSED_IMAGE_CACHE[cache_id] = {}
        buffer = BytesIO()
        passport_img.save(buffer, format="PNG")
        buffer.seek(0)
        PROCESSED_IMAGE_CACHE[cache_id][image_hash] = buffer.getvalue()
        print(f"DEBUG: Cached enhanced image for hash {image_hash} in cache_id {cache_id}")

    return passport_img


@app.route("/process", methods=["POST"])
def process():
    print("==== /process endpoint hit ====")
    cache_id = request.form.get("cache_id")

    # Layout settings (A4 at 300 DPI)
    stroke = int(request.form.get("stroke", request.form.get("border", 2)))
    try:
        spacing_in = float(request.form.get("spacing_in", 0))
    except (TypeError, ValueError):
        spacing_in = 0

    if spacing_in > 0:
        spacing = max(0, int(spacing_in * 300))
    else:
        # Fallback for legacy clients that still send spacing in px.
        spacing = max(0, int(request.form.get("spacing", 60)))
    margin_x = 10
    margin_y = 10
    a4_w, a4_h = 2480, 3508
    try:
        skip_slots = max(0, int(request.form.get("skip_slots", 0)))
    except (TypeError, ValueError):
        skip_slots = 0

    # New mode: flattened print items where each item can have its own size.
    print_items = []
    item_count = int(request.form.get("item_count", 0))

    if item_count > 0:
        for i in range(item_count):
            file = request.files.get(f"image_{i}")
            if not file:
                continue
            copies = max(1, int(request.form.get(f"copies_{i}", 4)))
            width_px = max(50, int(request.form.get(f"width_px_{i}", 390)))
            height_px = max(50, int(request.form.get(f"height_px_{i}", 480)))
            print_items.append((file.read(), copies, width_px, height_px, f"item_{i}"))

    # Legacy mode compatibility (single size for all images)
    if not print_items:
        passport_width = int(request.form.get("width", 390))
        passport_height = int(request.form.get("height", 480))

        i = 0
        while f"image_{i}" in request.files:
            file = request.files[f"image_{i}"]
            copies = max(1, int(request.form.get(f"copies_{i}", 4)))
            print_items.append((file.read(), copies, passport_width, passport_height, f"legacy_{i}"))
            i += 1

        if not print_items and "image" in request.files:
            file = request.files["image"]
            copies = max(1, int(request.form.get("copies", 4)))
            print_items.append((file.read(), copies, passport_width, passport_height, "legacy_single"))

    if not print_items:
        return "No image uploaded", 400

    print(f"DEBUG: Processing {len(print_items)} print item(s)")

    # Process all print items (image + per-item size + copies)
    processed_items = []
    for idx, (img_bytes, copies, width_px, height_px, item_name) in enumerate(print_items):
        image_hash = get_image_hash(img_bytes)
        print(
            f"DEBUG: Processing {item_name} ({idx + 1}/{len(print_items)}) "
            f"with size {width_px}x{height_px} and {copies} copies (hash: {image_hash})"
        )
        try:
            img = process_single_image(img_bytes, cache_id=cache_id, image_hash=image_hash)
            img = img.resize((width_px, height_px), Image.LANCZOS)
            img = ImageOps.expand(img, border=stroke, fill="black")
            processed_items.append((img, copies, width_px + 2 * stroke, height_px + 2 * stroke))
        except ValueError as e:
            err_str = str(e)
            if "410" in err_str or "face" in err_str.lower():
                return {"error": "face_detection_failed"}, 410
            elif "429" in err_str or "quota" in err_str.lower():
                return {"error": "quota_exceeded"}, 429
            elif "auth_failed" in err_str.lower() or "403" in err_str:
                return {"error": "remove_bg_auth_failed"}, 403
            elif "missing_remove_bg_api_key" in err_str:
                return {"error": "missing_remove_bg_api_key"}, 500
            else:
                print(err_str)
                return {"error": err_str}, 500

    # Build all pages
    pages = []
    current_page = Image.new("RGB", (a4_w, a4_h), "white")
    x, y = margin_x, margin_y
    row_max_height = 0

    def new_page():
        nonlocal current_page, x, y, row_max_height
        pages.append(current_page)
        current_page = Image.new("RGB", (a4_w, a4_h), "white")
        x, y = margin_x, margin_y
        row_max_height = 0

    for passport_img, copies, paste_w, paste_h in processed_items:
        for _ in range(copies):
            # Reserve leading empty slots without consuming actual copy count.
            while skip_slots > 0:
                if x + paste_w > a4_w - margin_x:
                    x = margin_x
                    y += row_max_height + spacing
                    row_max_height = 0

                if y + paste_h > a4_h - margin_y:
                    new_page()

                skip_slots -= 1
                row_max_height = max(row_max_height, paste_h)
                x += paste_w + spacing
                print(f"DEBUG: Skipped slot, remaining skips = {skip_slots}")

            # Move to next row if this photo does not fit in current row.
            if x + paste_w > a4_w - margin_x:
                x = margin_x
                y += row_max_height + spacing
                row_max_height = 0

            # Move to next page if this row position overflows page height.
            if y + paste_h > a4_h - margin_y:
                new_page()

            current_page.paste(passport_img, (x, y))
            print(f"DEBUG: Placed at x={x}, y={y}")
            row_max_height = max(row_max_height, paste_h)
            x += paste_w + spacing

    pages.append(current_page)
    print(f"DEBUG: Total pages = {len(pages)}")

    # Export multi-page PDF
    output = BytesIO()
    if len(pages) == 1:
        pages[0].save(output, format="PDF", dpi=(300, 300))
    else:
        pages[0].save(
            output,
            format="PDF",
            dpi=(300, 300),
            save_all=True,
            append_images=pages[1:],
        )
    output.seek(0)
    print("DEBUG: Returning PDF to client")

    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="id-photo-cut-sheet.pdf",
    )


@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """Clear processed images cache for a specific cache_id or all caches."""
    cache_id = request.form.get("cache_id")
    if not cache_id and request.is_json:
        payload = request.get_json(silent=True) or {}
        cache_id = payload.get("cache_id")

    if cache_id:
        PROCESSED_IMAGE_CACHE.pop(cache_id, None)
        return {"success": True, "message": f"Cache cleared for {cache_id}"}

    PROCESSED_IMAGE_CACHE.clear()
    return {"success": True, "message": "All caches cleared"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)