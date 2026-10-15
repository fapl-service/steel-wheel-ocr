import os
# Configure OpenVINO model caching
os.makedirs("model/cache", exist_ok=True)
os.environ["OV_CACHE_DIR"] = "model/cache"

# Performance tuning for single-core / low-vCPU cloud containers to eliminate CPU thrashing
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import base64
import difflib
import io
import json
import time
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np
import openpyxl
import torch
torch.set_num_threads(1)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ultralytics import YOLO

# -----------------------------
# Configuration
# -----------------------------
MODEL_PATH = "best.pt"
LOGO_PATH = "logo.png"
PROCESSED_DIR = "Processed_Images"
TEMPLATES_FILE = "templates.json"
APPROVALS_FILE = "approvals.json"

CLASS_MAPPING = {
    "slash": "/",
    "IST": "logo.png",
}

DEFAULT_CONFIDENCE = 0.1
CHAR_IOU_THRESH = 0.15
BOX_IOU_THRESH = 0.10
EXPAND_CANVAS = False

# Ensure output directories exist
os.makedirs(PROCESSED_DIR, exist_ok=True)

# -----------------------------
# App Initialization
# -----------------------------
app = FastAPI(
    title="Steel Wheel OCR Verification System",
    description="Full Web GUI for automated wheel straightening, interactive multi-ROI selection, and OCR verification.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    error_msg = f"{type(exc).__name__}: {str(exc)}"
    print(f"\n[FASTAPI ERROR] {request.method} {request.url.path} - {error_msg}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": error_msg,
            "detail": error_msg,
            "traceback": tb
        }
    )

# Load OpenVINO / YOLO Segmentation model and warm it up on startup
OPENVINO_DIR = "best_openvino_model"
model = None

if os.path.exists(OPENVINO_DIR):
    try:
        print(f"[STARTUP] Loading OpenVINO Segmentation Model from {OPENVINO_DIR} ...")
        model = YOLO(OPENVINO_DIR, task="segment")
        print("[STARTUP] OpenVINO model loaded! Warming up with dummy frame...")
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        model(dummy, imgsz=640, verbose=False)
        print("[STARTUP] OpenVINO Segmentation Model warmed up and ready!")
    except Exception as e:
        print(f"[STARTUP ERROR] Failed loading OpenVINO model: {e}. Falling back to PyTorch model.")
        model = None

if model is None:
    print(f"[STARTUP] Loading PyTorch YOLO model from {MODEL_PATH} ...")
    model = YOLO(MODEL_PATH)
    dummy = np.zeros((640, 640, 3), dtype=np.uint8)
    model(dummy, imgsz=640, verbose=False)
    print("[STARTUP] PyTorch YOLO Model loaded & warmed up!")

logo_img = None
if os.path.exists(LOGO_PATH):
    logo_img = cv2.imread(LOGO_PATH, cv2.IMREAD_UNCHANGED)
    print(f"[STARTUP] Loaded logo from {LOGO_PATH} (shape: {logo_img.shape})")
else:
    print(f"[STARTUP] Warning: Logo not found at {LOGO_PATH}")


# ==============================================================================
# Helper Algorithms (Geometry, Deskewing & OCR Processing)
# ==============================================================================

def order_points_clockwise(pts: np.ndarray) -> np.ndarray:
    """Arranges 4 box corner points in clockwise order: [TL, TR, BR, BL]"""
    pts = np.array(pts, dtype=np.float32)
    x_sorted = pts[np.argsort(pts[:, 0]), :]
    left_most = x_sorted[:2, :]
    right_most = x_sorted[2:, :]

    left_most = left_most[np.argsort(left_most[:, 1]), :]
    tl, bl = left_most[0], left_most[1]

    right_most = right_most[np.argsort(right_most[:, 1]), :]
    tr, br = right_most[0], right_most[1]

    return np.array([tl, tr, br, bl], dtype=np.float32)


def straighten_full_image(image: np.ndarray, pts: np.ndarray, expand: bool = False) -> Tuple[np.ndarray, float]:
    """Rotates/deskews the entire image based on the tilt angle of the 4 corner points."""
    rect = order_points_clockwise(pts)
    tl, tr, br, bl = rect

    dx = tr[0] - tl[0]
    dy = tr[1] - tl[1]
    angle = float(np.degrees(np.arctan2(dy, dx)))

    (h, w) = image.shape[:2]
    center = (w / 2.0, h / 2.0)

    M = cv2.getRotationMatrix2D(center, angle, 1.0)

    if expand:
        cos = np.abs(M[0, 0])
        sin = np.abs(M[0, 1])
        new_w = int((h * sin) + (w * cos))
        new_h = int((h * cos) + (w * sin))
        M[0, 2] += (new_w / 2.0) - center[0]
        M[1, 2] += (new_h / 2.0) - center[1]
        rotated = cv2.warpAffine(image, M, (new_w, new_h), flags=cv2.INTER_LINEAR)
    else:
        rotated = cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_LINEAR)

    return rotated, angle


def calculate_iou(box1, box2):
    """Calculates Intersection-over-Union (IoU) between two bounding boxes [x1, y1, x2, y2]."""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    inter_area = max(0, x2 - x1) * max(0, y2 - y1)
    box1_area = (box1[2] - box1[0]) * (box1[3] - box1[1])
    box2_area = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union_area = box1_area + box2_area - inter_area

    return inter_area / union_area if union_area > 0 else 0.0


def deduplicate_detections(detections, iou_thresh=0.35):
    """Filters duplicate overlapping detections, keeping the one with higher confidence."""
    sorted_dets = sorted(detections, key=lambda x: x["conf"], reverse=True)
    kept = []
    for det in sorted_dets:
        if not any(calculate_iou(det["bbox"], k["bbox"]) > iou_thresh for k in kept):
            kept.append(det)
    return kept


def overlay_transparent(background, overlay, x, y, w, h):
    """Overlays a transparent RGBA image onto a BGR background image at (x, y) with size (w, h)."""
    if overlay is None or w <= 0 or h <= 0:
        return

    resized = cv2.resize(overlay, (w, h), interpolation=cv2.INTER_AREA)
    h_bg, w_bg = background.shape[:2]

    bx1 = max(0, x)
    by1 = max(0, y)
    bx2 = min(w_bg, x + w)
    by2 = min(h_bg, y + h)

    if bx2 <= bx1 or by2 <= by1:
        return

    ox1 = bx1 - x
    oy1 = by1 - y
    ox2 = ox1 + (bx2 - bx1)
    oy2 = oy1 + (by2 - by1)

    roi_bg = background[by1:by2, bx1:bx2]
    roi_ov = resized[oy1:oy2, ox1:ox2]

    if roi_ov.shape[2] == 4:
        alpha = (roi_ov[:, :, 3] / 255.0)[:, :, None]
        fg = roi_ov[:, :, :3]
        background[by1:by2, bx1:bx2] = (fg * alpha + roi_bg * (1.0 - alpha)).astype(np.uint8)
    else:
        background[by1:by2, bx1:bx2] = roi_ov[:, :, :3]


def draw_text_with_logo_and_background(img, text, origin, logo=None, font_scale=0.9, thickness=2,
                                       text_color=(0, 255, 0), bg_color=(20, 20, 20), padding=6):
    """Draws text with solid background and inline logo.png replacement."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    x, y = origin

    target_token = "logo.png" if "logo.png" in text else ("logo" if "logo" in text else None)
    if target_token and logo is not None:
        parts = text.split(target_token)
        sizes = [cv2.getTextSize(p, font, font_scale, thickness)[0] for p in parts]
        lh = max(s[1] for s in sizes) if sizes else 25

        logo_h = max(24, int(lh * 1.35))
        logo_w = int(logo.shape[1] * (logo_h / logo.shape[0]))

        total_text_w = sum(s[0] for s in sizes)
        total_w = total_text_w + (len(parts) - 1) * (logo_w + padding * 2)
        total_h = max(lh, logo_h)

        y1 = max(0, y - total_h - padding)
        y2 = min(img.shape[0], y + padding * 2)
        x1 = max(0, x - padding)
        x2 = min(img.shape[1], x + total_w + padding * 2)

        cv2.rectangle(img, (x1, y1), (x2, y2), bg_color, -1)

        curr_x = x
        for i, part in enumerate(parts):
            if part:
                cv2.putText(img, part, (curr_x, y), font, font_scale, text_color, thickness, cv2.LINE_AA)
                curr_x += sizes[i][0]
            if i < len(parts) - 1:
                curr_x += padding
                logo_y = y - logo_h + (lh - logo_h) // 4
                overlay_transparent(img, logo, curr_x, logo_y, logo_w, logo_h)
                curr_x += logo_w + padding
    else:
        (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        y1 = max(0, y - text_h - padding * 2)
        y2 = min(img.shape[0], y + baseline + padding)
        x1 = max(0, x - padding)
        x2 = min(img.shape[1], x + text_w + padding)

        cv2.rectangle(img, (x1, y1), (x2, y2), bg_color, -1)
        cv2.putText(img, text, (x, y - padding), font, font_scale, text_color, thickness, cv2.LINE_AA)


def image_to_base64(img: np.ndarray, format_ext: str = ".jpg") -> str:
    """Encodes a BGR/BGRA numpy image to a base64 string."""
    success, buffer = cv2.imencode(format_ext, img)
    if not success:
        raise ValueError("Could not encode image to base64")
    b64_str = base64.b64encode(buffer).decode("utf-8")
    mime_type = "image/jpeg" if format_ext in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime_type};base64,{b64_str}"


def base64_to_image(b64_str: str) -> np.ndarray:
    """Decodes a base64 string to a BGR numpy image."""
    if "," in b64_str:
        b64_str = b64_str.split(",")[1]
    img_data = base64.b64decode(b64_str)
    nparr = np.frombuffer(img_data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image from base64")

    # Downscale oversized images (> 1280px) to prevent Out-Of-Memory & slow inference on cloud
    h, w = img.shape[:2]
    max_dim = 1280
    if max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    return img


# ==============================================================================
# Template Storage Helpers
# ==============================================================================

def load_templates() -> Dict[str, Any]:
    """Load templates from JSON file."""
    if not os.path.exists(TEMPLATES_FILE):
        return {"templates": []}
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_templates(data: Dict[str, Any]):
    """Save templates to JSON file."""
    with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_template_by_id(template_id: str) -> Optional[Dict]:
    """Find a template by its ID/wheel_code."""
    data = load_templates()
    for t in data.get("templates", []):
        if t["id"] == template_id:
            return t
    return None


# ==============================================================================
# Pydantic Request Models
# ==============================================================================

class StraightenRequest(BaseModel):
    image_base64: str
    confidence: Optional[float] = DEFAULT_CONFIDENCE


class ProcessOCRRequest(BaseModel):
    straightened_image_base64: Optional[str] = None
    image_base64: Optional[str] = None
    rois: Optional[List[List[int]]] = []  # List of [x1, y1, x2, y2]
    tilt_angle: Optional[float] = 0.0
    confidence: Optional[float] = DEFAULT_CONFIDENCE


class ValidateOCRRequest(BaseModel):
    template_id: str
    side: str = "front"  # "front" or "rear"
    ocr_texts: List[str] = []  # Detected OCR texts per ROI

# ==============================================================================
# API Endpoints
# ==============================================================================


@app.post("/api/straighten")
def api_straighten(req: StraightenRequest):
    """
    Pass 1: Detects 'box' on the input image, calculates tilt angle, and returns straightened full image.
    """
    try:
        frame = base64_to_image(req.image_base64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {str(e)}")

    conf = req.confidence or DEFAULT_CONFIDENCE
    start_time = time.perf_counter()

    # Pass 1 Inference with fixed imgsz=640 for fast CPU processing
    res1 = model(frame, imgsz=640, conf=conf, verbose=False)[0]

    straight_frame = frame.copy()
    tilt_angle = 0.0
    box_detected = False

    # Extract 4 corners from segmentation mask if present
    if res1.masks is not None and len(res1.masks) > 0:
        masks_xy = res1.masks.xy
        classes = res1.boxes.cls.cpu().numpy().astype(int)
        confidences = res1.boxes.conf.cpu().numpy()

        best_box_pts = None
        max_conf = -1.0

        for poly, cls_id, c_conf in zip(masks_xy, classes, confidences):
            cname = res1.names.get(cls_id, str(cls_id))
            if cname == "box" and len(poly) >= 3 and c_conf > max_conf:
                rect = cv2.minAreaRect(poly.astype(np.float32))
                best_box_pts = cv2.boxPoints(rect)
                max_conf = c_conf

        if best_box_pts is not None:
            straight_frame, tilt_angle = straighten_full_image(frame, best_box_pts, expand=EXPAND_CANVAS)
            box_detected = True

    elif res1.boxes is not None and len(res1.boxes) > 0:
        # Fallback to standard bounding box
        for b in res1.boxes:
            cls_id = int(b.cls[0].item())
            if res1.names[cls_id] == "box":
                bx1, by1, bx2, by2 = b.xyxy[0].tolist()
                box_pts = np.array([[bx1, by1], [bx2, by1], [bx2, by2], [bx1, by2]], dtype=np.float32)
                straight_frame, tilt_angle = straighten_full_image(frame, box_pts, expand=EXPAND_CANVAS)
                box_detected = True
                break

    latency_ms = (time.perf_counter() - start_time) * 1000
    h, w = straight_frame.shape[:2]

    return {
        "success": True,
        "box_detected": box_detected,
        "tilt_angle": round(tilt_angle, 2),
        "width": w,
        "height": h,
        "latency_ms": round(latency_ms, 1),
        "straightened_image": image_to_base64(straight_frame, ".jpg"),
    }


@app.post("/api/process_ocr")
def api_process_ocr(req: ProcessOCRRequest):
    """
    Pass 2: Detects characters on the straightened image, filters by selected ROIs,
    sorts coordinates, replaces slash and IST with logo.png, and overlays visual results.
    """
    img_b64 = req.image_base64 or req.straightened_image_base64
    if not img_b64:
        raise HTTPException(status_code=400, detail="No image provided for OCR processing")
    try:
        straight_frame = base64_to_image(img_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image: {str(e)}")

    conf = req.confidence or DEFAULT_CONFIDENCE
    start_time = time.perf_counter()

    # Pass 2 Inference on Straightened Image with fixed imgsz=640 for fast CPU processing
    res2 = model(straight_frame, imgsz=640, conf=conf, verbose=False)[0]
    inference_time = (time.perf_counter() - start_time) * 1000

    annotated = straight_frame.copy()

    all_detected_boxes = []
    all_detected_chars = []

    if res2.boxes is not None and len(res2.boxes) > 0:
        for b in res2.boxes:
            cls_id = int(b.cls[0].item())
            cls_name = str(res2.names[cls_id])
            box_conf = float(b.conf[0].item())
            xyxy = [int(v) for v in b.xyxy[0].tolist()]

            if cls_name == "box":
                all_detected_boxes.append({"bbox": xyxy, "conf": box_conf})
            else:
                mapped_char = CLASS_MAPPING.get(cls_name, cls_name)
                all_detected_chars.append({
                    "cls_name": cls_name,
                    "char": mapped_char,
                    "bbox": xyxy,
                    "conf": box_conf,
                })

    ocr_results = []
    has_ist_logo = False
    rois = req.rois or []

    # If user selected ROIs, process each ROI
    if rois:
        # Sort ROIs top-to-bottom
        sorted_rois = sorted(rois, key=lambda r: r[1])

        for r_idx, (rx1, ry1, rx2, ry2) in enumerate(sorted_rois, 1):
            chars_in_roi = []
            for char_info in all_detected_chars:
                cx1, cy1, cx2, cy2 = char_info["bbox"]
                mid_x = (cx1 + cx2) / 2
                mid_y = (cy1 + cy2) / 2
                in_roi_by_center = (rx1 - 10) <= mid_x <= (rx2 + 10) and (ry1 - 10) <= mid_y <= (ry2 + 10)
                inter_w = max(0, min(rx2, cx2) - max(rx1, cx1))
                inter_h = max(0, min(ry2, cy2) - max(ry1, cy1))
                char_area = max(1, (cx2 - cx1) * (cy2 - cy1))
                in_roi_by_overlap = ((inter_w * inter_h) / char_area) >= 0.35

                if in_roi_by_center or in_roi_by_overlap:
                    chars_in_roi.append(char_info)

            chars_in_roi = deduplicate_detections(chars_in_roi, iou_thresh=CHAR_IOU_THRESH)
            chars_in_roi.sort(key=lambda c: c["bbox"][0])

            final_text = "".join(c["char"] for c in chars_in_roi)
            roi_has_ist = any(c["cls_name"] == "IST" for c in chars_in_roi)
            if roi_has_ist:
                has_ist_logo = True

            ocr_results.append({
                "roi_idx": r_idx,
                "bbox": [rx1, ry1, rx2, ry2],
                "text": final_text,
                "has_logo": roi_has_ist,
                "char_count": len(chars_in_roi),
                "chars": [
                    {"char": c["char"], "conf": round(c["conf"], 2), "bbox": c["bbox"]}
                    for c in chars_in_roi
                ],
            })

            # Draw ROI bounding box (Magenta)
            cv2.rectangle(annotated, (rx1, ry1), (rx2, ry2), (255, 0, 255), 2)
            cv2.putText(annotated, f"ROI {r_idx}", (rx1, max(22, ry1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2, cv2.LINE_AA)

            # Draw characters & overlay IST logo
            for c in chars_in_roi:
                cx1, cy1, cx2, cy2 = c["bbox"]
                cw = cx2 - cx1
                ch = cy2 - cy1

                if c["cls_name"] == "IST":
                    if logo_img is not None:
                        overlay_transparent(annotated, logo_img, cx1, cy1, cw, ch)
                        cv2.rectangle(annotated, (cx1, cy1), (cx2, cy2), (0, 255, 0), 2)
                else:
                    cv2.rectangle(annotated, (cx1, cy1), (cx2, cy2), (0, 255, 255), 1)
                    cv2.putText(annotated, c["char"], (cx1, max(15, cy1 - 3)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

            # (OCR text displayed cleanly in GUI results panel, not plastered on image)
    else:
        # Fallback: Auto-detect all boxes if no ROIs were selected
        detected_boxes = deduplicate_detections(all_detected_boxes, iou_thresh=BOX_IOU_THRESH)
        detected_boxes.sort(key=lambda b: b["bbox"][1])

        if detected_boxes:
            for b_idx, box_info in enumerate(detected_boxes, 1):
                bx1, by1, bx2, by2 = box_info["bbox"]
                chars_in_box = []
                for char_info in all_detected_chars:
                    cx1, cy1, cx2, cy2 = char_info["bbox"]
                    mid_x = (cx1 + cx2) / 2
                    mid_y = (cy1 + cy2) / 2
                    if (bx1 - 5) <= mid_x <= (bx2 + 5) and (by1 - 5) <= mid_y <= (by2 + 5):
                        chars_in_box.append(char_info)

                chars_in_box = deduplicate_detections(chars_in_box, iou_thresh=CHAR_IOU_THRESH)
                chars_in_box.sort(key=lambda c: c["bbox"][0])
                final_text = "".join(c["char"] for c in chars_in_box)
                box_has_ist = any(c["cls_name"] == "IST" for c in chars_in_box)
                if box_has_ist:
                    has_ist_logo = True

                ocr_results.append({
                    "roi_idx": b_idx,
                    "bbox": [bx1, by1, bx2, by2],
                    "text": final_text,
                    "has_logo": box_has_ist,
                    "char_count": len(chars_in_box),
                    "chars": [
                        {"char": c["char"], "conf": round(c["conf"], 2), "bbox": c["bbox"]}
                        for c in chars_in_box
                    ],
                })

                cv2.rectangle(annotated, (bx1, by1), (bx2, by2), (255, 200, 0), 3)

                for c in chars_in_box:
                    cx1, cy1, cx2, cy2 = c["bbox"]
                    cw = cx2 - cx1
                    ch = cy2 - cy1
                    if c["cls_name"] == "IST":
                        if logo_img is not None:
                            overlay_transparent(annotated, logo_img, cx1, cy1, cw, ch)
                            cv2.rectangle(annotated, (cx1, cy1), (cx2, cy2), (0, 255, 0), 2)
                    else:
                        cv2.rectangle(annotated, (cx1, cy1), (cx2, cy2), (0, 255, 255), 1)
                        cv2.putText(annotated, c["char"], (cx1, max(15, cy1 - 3)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
        else:
            all_chars = deduplicate_detections(all_detected_chars, iou_thresh=CHAR_IOU_THRESH)
            all_chars.sort(key=lambda c: c["bbox"][0])
            final_text = "".join(c["char"] for c in all_chars)
            ocr_results.append({
                "roi_idx": 1,
                "bbox": [0, 0, straight_frame.shape[1], straight_frame.shape[0]],
                "text": final_text,
                "has_logo": any(c["cls_name"] == "IST" for c in all_chars),
                "char_count": len(all_chars),
                "chars": [],
            })

    # Save snapshot of clean detection image
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(PROCESSED_DIR, f"web_result_{timestamp}.jpg")
    cv2.imwrite(save_path, annotated)

    return {
        "success": True,
        "inference_time_ms": round(inference_time, 1),
        "has_ist_logo": has_ist_logo,
        "results": ocr_results,
        "annotated_image": image_to_base64(annotated, ".jpg"),
    }


# ==============================================================================
# Template CRUD API Endpoints
# ==============================================================================

@app.get("/api/templates")
def api_list_templates():
    """Returns all marking templates."""
    data = load_templates()
    return {"success": True, "templates": data.get("templates", [])}


@app.get("/api/templates/{template_id}")
def api_get_template(template_id: str):
    """Returns a single template by ID."""
    t = find_template_by_id(template_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return {"success": True, "template": t}


@app.post("/api/templates/save")
async def api_save_template_json(request: Request):
    """Creates or updates a marking template from JSON body."""
    body = await request.json()
    
    if not body.get("id") or not body.get("wheel_code"):
        raise HTTPException(status_code=400, detail="Template must have 'id' and 'wheel_code'")
    
    data = load_templates()
    templates = data.get("templates", [])
    
    # Check if updating existing template
    existing_idx = None
    for idx, t in enumerate(templates):
        if t["id"] == body["id"]:
            existing_idx = idx
            break
    
    if existing_idx is not None:
        templates[existing_idx] = body
    else:
        templates.append(body)
    
    data["templates"] = templates
    save_templates(data)
    
    return {"success": True, "message": f"Template '{body['id']}' saved", "template": body}


@app.delete("/api/templates/{template_id}")
def api_delete_template(template_id: str):
    """Deletes a marking template."""
    data = load_templates()
    templates = data.get("templates", [])
    original_len = len(templates)
    templates = [t for t in templates if t["id"] != template_id]
    
    if len(templates) == original_len:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    
    data["templates"] = templates
    save_templates(data)
    return {"success": True, "message": f"Template '{template_id}' deleted"}


@app.post("/api/validate_ocr")
async def api_validate_ocr(request: Request):
    """
    Validates OCR results against a wheel template.
    Compares expected OCR text with detected OCR text using space-insensitive sequence matching.
    Evaluates individual ROIs as well as combined text to support multi-ROI setups.
    Returns OK if match percentage >= 70% or exact match, NG otherwise.
    """
    try:
        body = await request.json()
        template_id = body.get("template_id", "")
        side = body.get("side", "front")  # "front" or "rear"
        ocr_texts = body.get("ocr_texts", [])  # List of detected texts from each ROI
        
        if not template_id:
            raise HTTPException(status_code=400, detail="template_id is required")
        
        template = find_template_by_id(template_id)
        if not template:
            raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
        
        # Get expected marking based on side
        if side == "front":
            marking_info = template.get("front_marking", {})
            expected_pattern = template.get("expected_front_pattern", "")
        else:
            marking_info = template.get("rear_marking", {})
            expected_pattern = template.get("expected_rear_pattern", "")
        
        expected_full = marking_info.get("full_text", "")
        
        if not expected_full or expected_full.upper() in ("N/A", "N.A.", "-", "NONE"):
            return {
                "success": True,
                "judgment": "N/A",
                "message": f"No {side} marking defined for this template",
                "expected_text": "N/A",
                "detected_text": " | ".join(ocr_texts) if ocr_texts else "(none)",
                "match_percentage": 0,
                "matched_chars": 0,
                "total_chars": 0,
                "template_id": template_id,
                "side": side,
                "logo_required": False,
                "logo": ""
            }
        
        # Normalize text: strip all whitespace, uppercase, remove logo references
        def normalize_text(text):
            if not text:
                return ""
            text = text.upper()
            text = text.replace("LOGO.PNG", "").replace("LOGO", "")
            text = re.sub(r'\s+', '', text)
            return text
        
        target_expected = expected_pattern if expected_pattern else expected_full
        norm_expected = normalize_text(target_expected)
        
        # Build list of candidate texts: each individual ROI and combined
        candidates = []
        for idx, t in enumerate(ocr_texts):
            raw = t.strip() if t else ""
            if raw:
                candidates.append({"text": raw, "source": f"ROI {idx + 1}"})
        
        combined_text = " ".join(ocr_texts).strip()
        if combined_text and len(ocr_texts) > 1:
            candidates.append({"text": combined_text, "source": "Combined"})
            
        if not candidates:
            return {
                "success": True,
                "judgment": "NG",
                "expected_text": expected_full,
                "expected_pattern": target_expected,
                "detected_text": "(no text detected)",
                "match_percentage": 0,
                "matched_chars": 0,
                "total_chars": len(norm_expected),
                "template_id": template_id,
                "side": side,
                "logo_required": template.get("logo_required", False),
                "logo": template.get("logo", "")
            }
        
        # Evaluate each candidate against norm_expected using SequenceMatcher
        best_score = -1.0
        best_matched_chars = 0
        best_total_chars = max(len(norm_expected), 1)
        best_detected_text = candidates[0]["text"]
        
        for cand in candidates:
            norm_cand = normalize_text(cand["text"])
            if not norm_cand:
                continue
            
            len_exp = len(norm_expected)
            len_cand = len(norm_cand)

            if norm_expected == norm_cand:
                score = 1000.0  # Exact match
                matched_chars = len_exp
            elif norm_expected in norm_cand and len_cand <= len_exp + 2:
                score = 900.0
                matched_chars = len_exp
            else:
                sm = difflib.SequenceMatcher(None, norm_expected, norm_cand)
                matched_chars = sum(block.size for block in sm.get_matching_blocks())
                
                # SequenceMatcher similarity ratio (0.0 - 1.0)
                sim_ratio = sm.ratio()
                
                # Length similarity penalty: strongly penalize candidates that are far longer than expected
                # (prevents combined 35-char Front+Rear strings from winning over a 6-char Rear ROI)
                len_penalty = min(len_exp, len_cand) / max(len_exp, len_cand)
                
                # Small bonus for targeted individual ROI over artificial combined text
                source_bonus = 1.08 if cand.get("source", "").startswith("ROI") else 1.0
                
                # Score combines character similarity, length match, and source preference
                score = (sim_ratio * 0.7 + len_penalty * 0.3) * 100.0 * source_bonus
            
            if score > best_score:
                best_score = score
                best_matched_chars = matched_chars
                best_total_chars = len(norm_expected)
                best_detected_text = cand["text"]
                
        # Determine if logo is required for this specific side
        logo_side = template.get("logo_side")
        if not logo_side:
            logo_side = "front" if template.get("logo_required", False) else "none"

        if side == "front":
            side_logo_required = logo_side in ("front", "both") or template.get("front_marking", {}).get("logo_required", False)
        else:
            side_logo_required = logo_side in ("rear", "both") or template.get("rear_marking", {}).get("logo_required", False)

        # Check if logo was detected in this candidate text
        has_detected_logo = "LOGO.PNG" in best_detected_text.upper() or "LOGO" in best_detected_text.upper()
        is_logo_ok = (not side_logo_required) or has_detected_logo

        # Strict character verification:
        # Expected and detected must be 100% exact match (best_score >= 900 or norm_expected == norm_cand)
        is_text_exact = (
            best_score >= 900.0 or
            norm_expected == normalize_text(best_detected_text)
        )

        display_match_pct = round(min(100.0, (best_matched_chars / max(best_total_chars, 1)) * 100.0), 1)

        if is_text_exact and is_logo_ok:
            judgment = "OK"
            message = "Verification Passed"
        elif not is_text_exact:
            judgment = "NG"
            message = f"Text Mismatch ({best_matched_chars}/{best_total_chars} chars match)"
        else:
            judgment = "NG"
            message = "Logo Missing"

        return {
            "success": True,
            "judgment": judgment,
            "message": message,
            "expected_text": expected_full,
            "expected_pattern": target_expected,
            "detected_text": best_detected_text,
            "match_percentage": display_match_pct,
            "matched_chars": best_matched_chars,
            "total_chars": best_total_chars,
            "template_id": template_id,
            "side": side,
            "logo_required": side_logo_required,
            "logo": template.get("logo", "") if side_logo_required else "",
            "logo_detected": has_detected_logo
        }
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] validate_ocr error: {str(e)}\n{tb}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e), "detail": str(e), "traceback": tb}
        )

def parse_single_sheet_data(ws, filename: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single worksheet for steel wheel marking check sheet data.
    Extracts Wheel Code, Customer, Front & Rear markings under PETROL 1,
    detects Logo Option, and extracts Segments.
    Returns template dict or None if sheet contains no marking data.
    """
    try:
        def clean_val(v):
            if v is None:
                return ""
            # Replace non-breaking spaces and collapse all whitespace & newlines into single spaces
            s = str(v).replace('\u00a0', ' ').replace('\ufeff', '')
            return re.sub(r'\s+', ' ', s).strip()

        def extract_dist(txt):
            if not txt:
                return "N/A"
            txt_s = str(txt)
            if "Spec" in txt_s or "spec" in txt_s:
                for sep in [":-", ":", "="]:
                    if sep in txt_s:
                        val = txt_s.split(sep, 1)[1].strip()
                        val = val.replace('\ufffd', '±').replace('?', '±')
                        return val + " mm" if "mm" not in val.lower() else val
            return "N/A"
            
        max_r = min(ws.max_row or 1, 80)
        max_c = min(ws.max_column or 1, 25)

        if max_r < 2:
            return None

        wheel_code = ""
        customer = ""
        front_full_text = ""
        rear_full_text = ""
        front_dist = "N/A"
        rear_dist = "N/A"

        # -------------------------------------------------------------
        # 1. Extract WHEEL CODE and CUSTOMER from header rows
        # -------------------------------------------------------------
        for r in range(1, min(max_r, 6)):
            for c in range(1, max_c + 1):
                val = clean_val(ws.cell(r, c).value)
                if not val:
                    continue
                v_up = val.upper()
                
                # Check for Wheel Code (e.g. "WHEEL CODE :-LP2137")
                if "WHEEL CODE" in v_up or "WHEELCODE" in v_up or "MODEL" in v_up:
                    for sep in [":-", ":", "="]:
                        if sep in val:
                            extracted = val.split(sep, 1)[1].strip()
                            if extracted:
                                wheel_code = extracted
                                break
                    if not wheel_code:
                        adj = clean_val(ws.cell(r, c + 1).value)
                        if adj:
                            wheel_code = adj

                # Check for Customer (e.g. "CUSTOMER :- MSIL")
                if "CUSTOMER" in v_up:
                    for sep in [":-", ":", "="]:
                        if sep in val:
                            extracted = val.split(sep, 1)[1].strip()
                            if extracted:
                                customer = extracted
                                break
                    if not customer:
                        adj = clean_val(ws.cell(r, c + 1).value)
                        if adj:
                            customer = adj
                        else:
                            below = clean_val(ws.cell(r + 1, c).value)
                            if below and len(below) <= 15:
                                customer = below

        # Clean any leading punctuation/symbols from wheel_code and customer
        if wheel_code:
            wheel_code = re.sub(r'^[^\w]+', '', wheel_code).strip()
        if customer:
            customer = re.sub(r'^[^\w]+', '', customer).strip()

        # Fallback for wheel code from sheet title
        if not wheel_code and ws.title:
            st = ws.title.strip().upper()
            m_sheet = re.search(r'LP\d+', st)
            if m_sheet:
                wheel_code = m_sheet.group(0)
            elif st not in ["SHEET1", "SHEET", "SHEET2", "SHEET3", "COVER", "INDEX", "SUMMARY", "PAGE1"]:
                wheel_code = st

        # Fallback for wheel code from filename
        if not wheel_code:
            fn = os.path.splitext(filename)[0].upper()
            m_fn = re.search(r'LP\d+', fn)
            if m_fn:
                wheel_code = m_fn.group(0)

        if not customer:
            customer = "MSIL"

        # -------------------------------------------------------------
        # 2. Locate PETROL 1 / PATROL 1 and boundary of PETROL 2
        # -------------------------------------------------------------
        petrol1_row = None
        petrol2_row = None

        for r in range(1, max_r + 1):
            for c in range(1, min(max_c, 6)):
                raw_cell = ws.cell(r, c).value
                if raw_cell is None:
                    continue
                val = clean_val(raw_cell).upper()
                if not val:
                    continue
                # Locate PETROL 1 / PATROL 1
                if re.search(r'\b(PETROL|PATROL)\s*1\b', val) or val.startswith("PETROL 1") or val.startswith("PATROL 1") or val in ("PETROL1", "PATROL1", "PETROL-1", "PATROL-1"):
                    if petrol1_row is None:
                        petrol1_row = r
                # Locate PETROL 2 / PATROL 2 (or 3, 4) to establish a strict boundary
                elif re.search(r'\b(PETROL|PATROL)\s*[2-9]\b', val) or val.startswith("PETROL 2") or val.startswith("PATROL 2") or val in ("PETROL2", "PATROL2", "PETROL-2", "PATROL-2", "PETROL 3", "PATROL 3", "PETROL 4", "PATROL 4"):
                    if petrol2_row is None and petrol1_row is not None and r > petrol1_row:
                        petrol2_row = r
                        break
            if petrol2_row is not None:
                break

        # Define row boundaries strictly for PETROL 1
        if petrol1_row is not None:
            start_row = petrol1_row
            end_row = petrol2_row if petrol2_row is not None else min(petrol1_row + 7, max_r + 1)
        else:
            start_row = 1
            end_row = petrol2_row if petrol2_row is not None else min(max_r + 1, 20)

        # -------------------------------------------------------------
        # 3. Extract FRONT and REAR MARKING strictly within PETROL 1
        # -------------------------------------------------------------
        front_row = None
        rear_row = None

        for r in range(start_row, end_row):
            for c in range(1, max_c + 1):
                val = clean_val(ws.cell(r, c).value).upper()
                if not val:
                    continue
                if "FRONT" in val and "MARKING" in val:
                    if front_row is None:
                        front_row = r
                elif "REAR" in val and "MARKING" in val:
                    if rear_row is None:
                        rear_row = r

        NOISE = [
            "FRONT MARKING", "REAR MARKING", "FRONT", "REAR", "PETROL", "PATROL", "SPEC", "ACTUAL", "ACT.",
            "START", "MIDDLE", "END", "CHECKED", "VERIFIED", "OPERATOR", "SUPERVISOR",
            "DATE", "SHIFT", "MARKING DISTANCE", "PATROL TIME", "SIGN", "REMARKS"
        ]
        def is_cell_noise(txt):
            up = txt.upper()
            return any(n in up for n in NOISE)

        # Extract Front Marking text from within PETROL 1
        front_raw_cells = []
        if front_row is not None:
            f_end_r = rear_row if rear_row and rear_row > front_row else min(front_row + 2, end_row)
            candidates = []
            for r in range(front_row, f_end_r):
                for c in range(1, max_c + 1):
                    raw_v = ws.cell(r, c).value
                    txt = clean_val(raw_v)
                    if txt and len(txt) >= 2 and not is_cell_noise(txt):
                        front_raw_cells.append(str(raw_v).strip())
                        if len(txt) >= 5:
                            candidates.append(txt)
                    if "Spec" in txt or "spec" in txt:
                        d = extract_dist(txt)
                        if d != "N/A":
                            front_dist = d
            if candidates:
                candidates.sort(key=len, reverse=True)
                front_full_text = candidates[0]

        # Extract Rear Marking text from within PETROL 1
        rear_raw_cells = []
        if rear_row is not None:
            candidates = []
            for r in range(rear_row, end_row):
                for c in range(1, max_c + 1):
                    raw_v = ws.cell(r, c).value
                    txt = clean_val(raw_v)
                    if txt:
                        if txt.upper() in ["N/A", "N.A.", "NA", "-", "NONE"]:
                            candidates.append("N/A")
                        elif len(txt) >= 2 and not is_cell_noise(txt):
                            rear_raw_cells.append(str(raw_v).strip())
                            candidates.append(txt)
                        if "Spec" in txt or "spec" in txt:
                            d = extract_dist(txt)
                            if d != "N/A":
                                rear_dist = d
            if candidates:
                candidates.sort(key=len, reverse=True)
                rear_full_text = candidates[0]
            else:
                rear_full_text = "N/A"
        else:
            rear_full_text = "N/A"

        # Cleanup whitespace
        front_full_text = re.sub(r'\s+', ' ', front_full_text).strip()
        rear_full_text = re.sub(r'\s+', ' ', rear_full_text).strip() or "N/A"

        # If neither front nor rear has marking text, sheet is empty or not a marking sheet
        if not front_full_text and (not rear_full_text or rear_full_text == "N/A"):
            return None

        # -------------------------------------------------------------
        # 4. Automatic Logo Detection (Front vs Rear vs Both vs None)
        # -------------------------------------------------------------
        front_has_image = False
        rear_has_image = False

        ws_images = getattr(ws, '_images', [])
        for img in ws_images:
            anchor = getattr(img, 'anchor', None)
            img_row = None
            if anchor is not None:
                img_from = getattr(anchor, '_from', None)
                if img_from and hasattr(img_from, 'row'):
                    img_row = img_from.row + 1
                elif hasattr(anchor, 'row'):
                    img_row = anchor.row + 1
            if img_row is not None:
                if front_row and abs(img_row - front_row) <= 1:
                    front_has_image = True
                elif rear_row and abs(img_row - rear_row) <= 1:
                    rear_has_image = True

        def detect_logo_in_marking(text, has_image=False):
            if has_image:
                return True
            if not text or text.upper() in ('N/A', 'N.A.', '-', 'NONE'):
                return False
            t_up = text.upper()
            has_bis = bool(re.search(r'IS\s*\d+', t_up) and ('CM/L' in t_up or 'CML' in t_up))
            has_logo_kw = any(k in t_up for k in ['LOGO', 'IST', 'ISI', 'BIS'])
            return has_bis or has_logo_kw

        front_has_logo = detect_logo_in_marking(front_full_text, front_has_image)
        rear_has_logo = detect_logo_in_marking(rear_full_text, rear_has_image)

        if front_has_logo and rear_has_logo:
            detected_logo_side = "both"
        elif front_has_logo:
            detected_logo_side = "front"
        elif rear_has_logo:
            detected_logo_side = "rear"
        else:
            detected_logo_side = "none"

        # -------------------------------------------------------------
        # 5. Automatic Segment Extraction (Number of Segments & Texts)
        # -------------------------------------------------------------
        def split_marking_segments(raw_texts: List[str], full_text: str, has_logo: bool = False) -> List[str]:
            if not full_text or full_text.upper() in ('N/A', 'N.A.', '-', 'NONE'):
                return []
            if raw_texts and len(raw_texts) > 1:
                clean_cells = [re.sub(r'\s+', ' ', t).strip() for t in raw_texts if len(t.strip()) >= 2 and not is_cell_noise(t)]
                if len(clean_cells) > 1:
                    return clean_cells
            for raw in raw_texts:
                if '\n' in raw or '\r' in raw:
                    lines = [re.sub(r'\s+', ' ', l).strip() for l in raw.replace('\r', '').split('\n')]
                    lines = [l for l in lines if l and len(l) >= 2 and not is_cell_noise(l)]
                    if len(lines) > 1:
                        return lines
            for raw in raw_texts:
                gaps = re.split(r'\s{2,}', raw.strip())
                gaps = [re.sub(r'\s+', ' ', g).strip() for g in gaps if len(g.strip()) >= 2 and not is_cell_noise(g)]
                if len(gaps) > 1:
                    return gaps
            bis_match = re.search(r'(IS\s*\d+.*?(?:CM/L|CML)[-\s]*\d+)', full_text, flags=re.IGNORECASE)
            if bis_match:
                start_bis, end_bis = bis_match.span()
                part1 = full_text[:start_bis].strip()
                part2 = full_text[start_bis:end_bis].strip()
                part3 = full_text[end_bis:].strip()
                parts = [p for p in [part1, part2, part3] if p and len(p) >= 2]
                if len(parts) > 1:
                    return parts
            return [full_text]

        front_raw_segs = split_marking_segments(front_raw_cells, front_full_text, front_has_logo)
        front_segments = []
        for idx, s in enumerate(front_raw_segs):
            s_txt = s
            if front_has_logo and "logo.png" not in s_txt and "IST" not in s_txt:
                s_txt = re.sub(r'(IS\s*\d+)\s+(CM/L)', r'\1 logo.png \2', s_txt)
            front_segments.append({"segment_num": idx + 1, "text": s_txt})

        rear_raw_segs = split_marking_segments(rear_raw_cells, rear_full_text, rear_has_logo)
        rear_segments = []
        for idx, s in enumerate(rear_raw_segs):
            s_txt = s
            if rear_has_logo and "logo.png" not in s_txt and "IST" not in s_txt:
                s_txt = re.sub(r'(IS\s*\d+)\s+(CM/L)', r'\1 logo.png \2', s_txt)
            rear_segments.append({"segment_num": idx + 1, "text": s_txt})

        # -------------------------------------------------------------
        # 6. Build Expected Patterns with Logo
        # -------------------------------------------------------------
        dynamic_tokens = ['YYMM', 'YY', 'MM', 'AA', 'WCWL', 'WCWLB', 'WWYY']
        
        def build_pattern(text, has_logo):
            if not text or text.upper() in ('N/A', 'N.A.', '-'):
                return 'N/A'
            words = text.split()
            filtered = [w for w in words if w.upper() not in dynamic_tokens]
            res = ' '.join(filtered)
            if has_logo and "logo.png" not in res and "IST" not in res:
                res = re.sub(r'(IS\s*\d+)\s+(CM/L)', r'\1 logo.png \2', res)
            return res

        front_full_text_with_logo = front_full_text
        if front_has_logo and "logo.png" not in front_full_text_with_logo and "IST" not in front_full_text_with_logo:
            front_full_text_with_logo = re.sub(r'(IS\s*\d+)\s+(CM/L)', r'\1 logo.png \2', front_full_text_with_logo)

        rear_full_text_with_logo = rear_full_text
        if rear_has_logo and "logo.png" not in rear_full_text_with_logo and "IST" not in rear_full_text_with_logo:
            rear_full_text_with_logo = re.sub(r'(IS\s*\d+)\s+(CM/L)', r'\1 logo.png \2', rear_full_text_with_logo)

        expected_front = build_pattern(front_full_text, front_has_logo)
        expected_rear = build_pattern(rear_full_text, rear_has_logo) if rear_full_text.upper() not in ('N/A', 'N.A.', '-') else 'N/A'

        final_id = wheel_code or ws.title or "MODEL"

        return {
            "id": final_id,
            "wheel_code": final_id,
            "customer": customer or "MSIL",
            "active": True,
            "logo": "IST" if detected_logo_side != "none" else "",
            "logo_required": detected_logo_side != "none",
            "logo_side": detected_logo_side,
            "front_marking": {
                "full_text": front_full_text_with_logo,
                "segments": front_segments,
                "distance_spec": front_dist,
                "logo_required": front_has_logo
            },
            "rear_marking": {
                "full_text": rear_full_text_with_logo,
                "segments": rear_segments,
                "distance_spec": rear_dist,
                "logo_required": rear_has_logo
            },
            "ocr_type": "Full Text Match",
            "expected_front_pattern": expected_front,
            "expected_rear_pattern": expected_rear
        }
    except Exception as e:
        print(f"[EXCEL PARSER] Error parsing sheet '{ws.title}': {str(e)}")
        return None


@app.post("/api/templates/parse_excel")
async def api_parse_excel(file: UploadFile = File(...)):
    """
    Parses an uploaded Excel (.xlsx) check sheet.
    Supports multi-sheet workbooks (e.g. 50+ sheets):
      - Loops through all sheets in the workbook.
      - Automatically extracts Wheel Code, Customer, PETROL 1 Front/Rear markings, Logo Option, and Segments for every sheet.
      - Automatically saves all created/updated models into templates.json.
      - Completely ignores PETROL 2, 3, 4 rows.
    """
    if not file.filename.endswith(('.xlsx', '.xls')):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Only .xlsx and .xls files are supported", "detail": "Invalid file extension"}
        )
    
    try:
        contents = await file.read()
        print(f"\n{'='*60}")
        print(f"[EXCEL PARSER] Processing Uploaded File: {file.filename} ({len(contents)} bytes)")
        print(f"{'='*60}")
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
        sheet_names = wb.sheetnames
        print(f"[EXCEL PARSER] Found {len(sheet_names)} sheet(s) in workbook: {sheet_names}")

        parsed_templates = []
        skipped_sheets = []

        for sheet_idx, sname in enumerate(sheet_names):
            ws = wb[sname]
            tmpl = parse_single_sheet_data(ws, file.filename)
            if tmpl and tmpl.get("wheel_code") and (tmpl.get("front_marking", {}).get("full_text") or tmpl.get("rear_marking", {}).get("full_text")):
                parsed_templates.append(tmpl)
                print(f"  ✓ Sheet [{sheet_idx + 1}/{len(sheet_names)}] '{sname}' -> Model: '{tmpl['wheel_code']}', Logo: {tmpl['logo_side']}, Segments: Front {len(tmpl['front_marking']['segments'])}, Rear {len(tmpl['rear_marking']['segments'])}")
            else:
                skipped_sheets.append(sname)
                print(f"  - Sheet [{sheet_idx + 1}/{len(sheet_names)}] '{sname}' -> (Skipped, no valid marking data)")

        if not parsed_templates:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"No valid wheel model check sheets could be extracted from {len(sheet_names)} sheet(s).",
                    "detail": "Make sure sheets contain PETROL 1 with FRONT MARKING and REAR MARKING."
                }
            )

        # Automatically save all created/updated models to templates.json
        data = load_templates()
        existing_templates = {t["id"]: t for t in data.get("templates", [])}
        created_count = 0
        updated_count = 0

        for tmpl in parsed_templates:
            t_id = tmpl["id"]
            if t_id in existing_templates:
                updated_count += 1
            else:
                created_count += 1
            existing_templates[t_id] = tmpl

        data["templates"] = list(existing_templates.values())
        save_templates(data)

        print(f"[EXCEL PARSER] Batch Complete: Saved {len(parsed_templates)} model(s) to templates.json ({created_count} new, {updated_count} updated)")
        print(f"{'='*60}\n")

        single_tmpl = parsed_templates[0] if len(parsed_templates) == 1 else None

        return {
            "success": True,
            "total_sheets": len(sheet_names),
            "imported_count": len(parsed_templates),
            "created_count": created_count,
            "updated_count": updated_count,
            "message": f"Successfully created/updated {len(parsed_templates)} model(s) from {len(sheet_names)} sheets.",
            "templates": parsed_templates,
            "template": single_tmpl or parsed_templates[0],
            "single_template": single_tmpl,
            "skipped_sheets": skipped_sheets,
            "source_file": file.filename
        }
        
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ERROR] Failed to parse Excel: {str(e)}\n{tb}")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"Failed to parse Excel: {str(e)}",
                "detail": f"Failed to parse Excel: {str(e)}",
                "traceback": tb
            }
        )


# ==============================================================================
# Approval Screen API Endpoints
# ==============================================================================

class CreateApprovalRequest(BaseModel):
    wheel_code: Optional[str] = "N/A"
    side: Optional[str] = "Front"
    captured_image: Optional[str] = ""
    master_ocr: str
    detected_ocr: str
    ocr_confidence: float = 98.7
    similarity: float = 100.0
    captured_by: Optional[str] = "operator1"
    captured_date: Optional[str] = None


class RejectApprovalRequest(BaseModel):
    rejection_reason: str


def load_approvals() -> List[Dict[str, Any]]:
    if not os.path.exists(APPROVALS_FILE):
        initial_records = [
            {
                "id": 25,
                "verification_num": 25,
                "status": "pending",
                "wheel_code": "LP2137",
                "side": "Front",
                "captured_image": "",
                "master_ocr": "ABC123456",
                "detected_ocr": "ABC123456",
                "ocr_confidence": 98.7,
                "similarity": 100.0,
                "captured_by": "operator1",
                "captured_date": "21-Aug-2026",
                "reviewed_by": None,
                "reviewed_at": None,
                "rejection_reason": None
            },
            {
                "id": 24,
                "verification_num": 24,
                "status": "pending",
                "wheel_code": "LP1123",
                "side": "Front",
                "captured_image": "",
                "master_ocr": "E13X4.00B DOT IS9436 logo.png CM/L-9512455524",
                "detected_ocr": "E13X4.00B DOT IS9436 logo.png CM/L-9512455524",
                "ocr_confidence": 99.2,
                "similarity": 100.0,
                "captured_by": "operator1",
                "captured_date": "21-Aug-2026",
                "reviewed_by": None,
                "reviewed_at": None,
                "rejection_reason": None
            },
            {
                "id": 23,
                "verification_num": 23,
                "status": "pending",
                "wheel_code": "LP2152",
                "side": "Front",
                "captured_image": "",
                "master_ocr": "13 X 4.00B H2 45 DOT IS9436 logo.png CM/L-9512455524",
                "detected_ocr": "13 X 4.00B H2 45 DOT IS9436 logo.png CM/L-9512455524",
                "ocr_confidence": 96.5,
                "similarity": 97.8,
                "captured_by": "operator2",
                "captured_date": "20-Aug-2026",
                "reviewed_by": None,
                "reviewed_at": None,
                "rejection_reason": None
            }
        ]
        save_approvals(initial_records)
        return initial_records

    try:
        with open(APPROVALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("approvals", [])
    except Exception as e:
        print(f"[APPROVALS ERROR] Error reading approvals: {e}")
        return []


def save_approvals(approvals: List[Dict[str, Any]]) -> bool:
    try:
        with open(APPROVALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"approvals": approvals}, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[APPROVALS ERROR] Error saving approvals: {e}")
        return False


@app.get("/api/approvals")
def api_get_approvals(status: Optional[str] = None):
    """Retrieves all verification approval records with optional status filtering."""
    all_records = load_approvals()
    records = all_records
    if status and status.lower() != "all":
        records = [r for r in records if r.get("status", "").lower() == status.lower()]
    records.sort(key=lambda r: r.get("verification_num", 0), reverse=True)
    pending_count = sum(1 for r in all_records if r.get("status") == "pending")
    approved_count = sum(1 for r in all_records if r.get("status") == "approved")
    rejected_count = sum(1 for r in all_records if r.get("status") == "rejected")
    return {
        "success": True,
        "count": len(records),
        "total_count": len(all_records),
        "pending_count": pending_count,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "approvals": records
    }


@app.post("/api/approvals")
def api_create_approval(req: CreateApprovalRequest):
    """Creates a new verification record to be reviewed and approved by admin."""
    records = load_approvals()
    next_num = max([r.get("verification_num", 0) for r in records], default=25) + 1
    record = {
        "id": next_num,
        "verification_num": next_num,
        "status": "pending",
        "wheel_code": req.wheel_code or "N/A",
        "side": req.side or "Front",
        "captured_image": req.captured_image or "",
        "master_ocr": req.master_ocr,
        "detected_ocr": req.detected_ocr,
        "ocr_confidence": round(req.ocr_confidence, 1),
        "similarity": round(req.similarity, 1),
        "captured_by": req.captured_by or "operator1",
        "captured_date": req.captured_date or time.strftime("%d-%b-%Y"),
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": None
    }
    records.append(record)
    save_approvals(records)
    return {"success": True, "approval": record}


@app.post("/api/approvals/{record_id}/approve")
def api_approve_record(record_id: int):
    """Admin approves a pending verification record."""
    records = load_approvals()
    found = False
    for r in records:
        if r.get("id") == record_id or r.get("verification_num") == record_id:
            r["status"] = "approved"
            r["reviewed_by"] = "admin"
            r["reviewed_at"] = time.strftime("%d-%b-%Y %H:%M")
            r["rejection_reason"] = None
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"Pending Verification #{record_id} not found")
    save_approvals(records)
    return {"success": True, "message": f"Verification #{record_id} Approved successfully"}


@app.post("/api/approvals/{record_id}/reject")
def api_reject_record(record_id: int, req: RejectApprovalRequest):
    """Admin rejects a pending verification record with reason."""
    reason = req.rejection_reason.strip() if req.rejection_reason else ""
    if not reason:
        raise HTTPException(status_code=400, detail="Rejection reason is required")
    records = load_approvals()
    found = False
    for r in records:
        if r.get("id") == record_id or r.get("verification_num") == record_id:
            r["status"] = "rejected"
            r["reviewed_by"] = "admin"
            r["reviewed_at"] = time.strftime("%d-%b-%Y %H:%M")
            r["rejection_reason"] = reason
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"Pending Verification #{record_id} not found")
    save_approvals(records)
    return {"success": True, "message": f"Verification #{record_id} Rejected", "rejection_reason": reason}


@app.delete("/api/approvals/{record_id}")
def api_delete_approval(record_id: int):
    """Deletes an approval record."""
    records = load_approvals()
    records = [r for r in records if r.get("id") != record_id and r.get("verification_num") != record_id]
    save_approvals(records)
    return {"success": True, "message": f"Verification #{record_id} deleted"}


@app.get("/logo.png")
def serve_logo():
    """Serves the IST replacement logo."""
    if os.path.exists(LOGO_PATH):
        return FileResponse(LOGO_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


# Mount static directory
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def serve_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Steel Wheel OCR System API</h1><p>Frontend loading...</p>")


def open_browser_when_ready(host: str, port: int):
    """Waits for FastAPI server to become responsive, then automatically launches the default browser."""
    import urllib.request
    import webbrowser

    url = f"http://{host}:{port}"
    for _ in range(40):
        time.sleep(0.3)
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    print(f"\n[AUTO-LAUNCH] Server is online. Opening browser at {url} ...\n")
                    webbrowser.open(url)
                    break
        except Exception:
            pass


if __name__ == "__main__":
    import socket
    import threading
    import uvicorn

    HOST = os.environ.get("HOST", "0.0.0.0")
    # Hugging Face Spaces uses 7860; Render passes PORT env variable; default locally to 8000
    PORT = int(os.environ.get("PORT", 7860 if os.environ.get("SPACE_ID") else 8000))
    is_cloud = bool(os.environ.get("SPACE_ID") or os.environ.get("RENDER"))

    # Determine local LAN IP address for phone connection
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("🚀 Steel Wheel OCR Verification System is Starting...")
    print(f"📡 Local PC Link:     http://localhost:{PORT}")
    print(f"📱 Phone Wi-Fi Link:  http://{local_ip}:{PORT}")
    if is_cloud:
        print(f"☁️  Cloud Mode Active: Running on Port {PORT}")
    print("=" * 60 + "\n")

    # Launch local browser automatically only when running on a local desktop
    if not is_cloud and os.environ.get("AUTO_OPEN", "1") == "1":
        threading.Thread(target=open_browser_when_ready, args=("127.0.0.1", PORT), daemon=True).start()

    uvicorn.run("app:app", host=HOST, port=PORT, reload=False if is_cloud else True)


