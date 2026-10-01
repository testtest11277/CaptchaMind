"""
GUI Information Acquisition pipeline for CAPTCHAMind.

This module only implements the paper's GUI acquisition stage. It preserves the
legacy helper interfaces used by shiyan.py while adding the structured GUI
context required by the methodology:

1. Page information
2. Component information
3. Component filtering
4. Component-level textual semantics
5. Structural and spatial context
6. Global interface context
"""

import os
import re
import json
import base64
import math
import subprocess
from typing import Any, Dict, List, Optional, Tuple


Bounds = Tuple[int, int, int, int]
Component = Dict[str, Any]
GuiContext = Dict[str, Any]
RecognitionResult = Dict[str, Any]
DEFAULT_MAX_SOLVING_ATTEMPTS = 3
DEFAULT_ZHIPUAI_MODEL = "glm-4.6v"

SUPPORTED_CAPTCHA_TYPES = {
    "none",
    "text_selection",
    "slider",
    "character",
    "image_rotation",
    "image_selection",
    "recaptcha",
    "unknown",
}


CAPTCHA_TYPE_TO_LEGACY_CODE = {
    "none": 0,
    "text_selection": 1,
    "slider": 2,
    "character": 3,
    "image_rotation": 4,
    "image_selection": 5,
    "recaptcha": 6,
    "unknown": 0,
}


# Paper module: example pool construction.
# Each entry stores representative textual, structural, and interaction signals.
# The retrieval stage below compares the current GUI context against this pool.
CAPTCHA_EXAMPLE_POOL = [
    {
        "type": "text_selection",
        "keywords": ["click", "select", "in order", "点击", "选择", "依次", "文字", "字符"],
        "structural_signals": ["middle-center", "android.widget.ImageView", "android.view.View"],
        "normalized_centers": [(0.50, 0.38), (0.38, 0.52), (0.50, 0.52), (0.62, 0.52), (0.50, 0.72)],
        "interaction_signals": ["ordered_click", "multi_tap"],
    },
    {
        "type": "slider",
        "keywords": ["slider", "drag", "slide", "滑块", "拖动", "滑动", "拼图"],
        "structural_signals": ["middle-center", "wide_control", "android.widget.SeekBar"],
        "normalized_centers": [(0.50, 0.36), (0.50, 0.56), (0.50, 0.72), (0.80, 0.84)],
        "interaction_signals": ["drag"],
    },
    {
        "type": "image_rotation",
        "keywords": ["rotate", "rotation", "upright", "旋转", "转正", "方向"],
        "structural_signals": ["middle-center", "round_image", "android.widget.ImageView"],
        "normalized_centers": [(0.50, 0.38), (0.50, 0.55), (0.50, 0.74), (0.80, 0.84)],
        "interaction_signals": ["rotate_or_drag"],
    },
    {
        "type": "image_selection",
        # Keep only task-level instruction words. Generic words such as
        # "image"/"图片" are too broad and cause launcher icon grids to be
        # misclassified as CAPTCHA image-selection grids.
        "keywords": ["select all", "containing", "matching", "squares", "选择所有", "请选择", "包含", "九宫格", "验证"],
        "structural_signals": ["grid_like", "middle-center", "android.widget.ImageView"],
        "normalized_centers": [(0.33, 0.38), (0.50, 0.38), (0.67, 0.38), (0.33, 0.52), (0.50, 0.52), (0.67, 0.52), (0.33, 0.66), (0.50, 0.66), (0.67, 0.66), (0.80, 0.84)],
        "interaction_signals": ["region_click"],
    },
    {
        "type": "recaptcha",
        "keywords": ["recaptcha", "not a robot", "robot", "我不是机器人", "重新验证"],
        "structural_signals": ["checkbox", "grid_like", "middle-center"],
        "normalized_centers": [(0.26, 0.50), (0.53, 0.50), (0.33, 0.42), (0.50, 0.42), (0.67, 0.42), (0.33, 0.58), (0.50, 0.58), (0.67, 0.58)],
        "interaction_signals": ["multi_round_region_click"],
    },
]


VERIFICATION_KEYWORDS = sorted(
    {
        keyword.lower()
        for example in CAPTCHA_EXAMPLE_POOL
        for keyword in example["keywords"]
    }
    | {
        "captcha",
        "verify",
        "verification",
        "click in order",
        "target",
        "gap",
        "验证码",
        "验证",
        "校验",
        "安全验证",
        "目标",
        "缺口",
        "完成",
        "提交",
    },
    key=len,
    reverse=True,
)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _xml_element_to_dict(element: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {f"@{key}": value for key, value in element.attrib.items()}
    children = [_xml_element_to_dict(child) for child in list(element)]
    if children:
        node["node"] = children if len(children) > 1 else children[0]
    return node


def parse_hierarchy_xml(page_source: str) -> Dict[str, Any]:
    """
    Parse UIAutomator XML into the xmltodict-compatible shape used here.

    xmltodict is convenient but not always installed in the phone-test
    environment. The stdlib fallback keeps the ADB backend runnable with only
    Python + adb.
    """
    try:
        import xmltodict

        return xmltodict.parse(page_source)
    except ImportError:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(page_source)
        return {root.tag: _xml_element_to_dict(root)}


def parse_bounds(bounds: Optional[str]) -> Optional[Bounds]:
    """Convert UIAutomator bounds like '[0,1][2,3]' into numeric bounds."""
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    return tuple(int(match.group(i)) for i in range(1, 5))


def bounds_center(bounds: Optional[str]) -> Optional[Tuple[float, float]]:
    parsed = parse_bounds(bounds)
    if not parsed:
        return None
    left, top, right, bottom = parsed
    return (left + right) / 2, (top + bottom) / 2


def bounds_area(bounds: Optional[str]) -> int:
    parsed = parse_bounds(bounds)
    if not parsed:
        return 0
    left, top, right, bottom = parsed
    return max(0, right - left) * max(0, bottom - top)


def get_screen_size(device_info: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Extract screen size from uiautomator2 device.info."""
    if not device_info:
        return None
    width = device_info.get("displayWidth") or device_info.get("width")
    height = device_info.get("displayHeight") or device_info.get("height")
    if width and height:
        return {"width": int(width), "height": int(height)}
    return None


def encode_screenshot_for_multimodal(
    screenshot_path: Optional[str],
    max_bytes: int = 512 * 1024,
) -> Optional[Dict[str, Any]]:
    """
    Paper mapping: screenshot-level multimodal input.

    The paper requires screenshot-level global interface context. This function
    creates a base64 image payload that can be passed to a multimodal LLM. To
    keep prompts manageable, very large screenshots are represented by metadata
    and path only unless the file is under max_bytes.
    """
    if not screenshot_path or not os.path.exists(screenshot_path):
        return None

    size = os.path.getsize(screenshot_path)
    payload = {
        "path": screenshot_path,
        "bytes": size,
        "mime_type": "image/png",
        "base64": None,
        "omitted_reason": None,
    }
    if size > max_bytes:
        payload["omitted_reason"] = f"image exceeds max_bytes={max_bytes}"
        return payload

    with open(screenshot_path, "rb") as image_file:
        payload["base64"] = base64.b64encode(image_file.read()).decode("utf-8")
    return payload


def _load_image_base64(image_path: Optional[str]) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
        return None
    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("utf-8")
    if os.getenv("CAPTCHAMIND_ZHIPU_IMAGE_URL_MODE", "raw").lower() == "data_url":
        return f"data:image/png;base64,{encoded}"
    return encoded


def _get_zhipu_api_key() -> Optional[str]:
    return os.getenv("ZHIPUAI_API_KEY") or os.getenv("ZHIPU_API_KEY") or os.getenv("GLM_API_KEY")


def _zhipu_enabled(gui_context: GuiContext) -> bool:
    config = gui_context.get("model_config", {})
    if config.get("provider") != "zhipuai":
        return False
    if config.get("enabled") is False:
        return False
    return bool(_get_zhipu_api_key())


def _patch_typing_backports():
    """Monkey-patch typing module for Python 3.7 compatibility with zhipuai SDK."""
    import typing as _typing
    try:
        import typing_extensions as _te
    except ImportError:
        return
    for _attr in ("Literal", "TypedDict", "Protocol", "Final", "final"):
        if not hasattr(_typing, _attr) and hasattr(_te, _attr):
            setattr(_typing, _attr, getattr(_te, _attr))


def _make_zhipu_client(api_key: str) -> Any:
    _patch_typing_backports()
    try:
        from zhipuai import ZhipuAI

        return ZhipuAI(api_key=api_key)
    except Exception:
        from zhipuai import ZhipuAiClient

        return ZhipuAiClient(api_key=api_key)



def _extract_response_text(response: Any) -> str:
    choice = response.choices[0]
    message = choice.get("message") if isinstance(choice, dict) else choice.message
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def invoke_zhipuai_multimodal(
    prompt: str,
    image_paths: Optional[List[str]] = None,
    model: Optional[str] = None,
    thinking_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Optional GLM-4.5V bridge used by the paper's LLM/MLLM stages.

    Configure the API key with ZHIPUAI_API_KEY. The code keeps the model
    optional so local tests still run without network access or SDK packages.
    """
    api_key = _get_zhipu_api_key()
    if not api_key:
        return {
            "ok": False,
            "provider": "zhipuai",
            "model": model or DEFAULT_ZHIPUAI_MODEL,
            "error": "missing ZHIPUAI_API_KEY",
        }

    content = []
    for image_path in image_paths or []:
        image_b64 = _load_image_base64(image_path)
        if image_b64:
            content.append({"type": "image_url", "image_url": {"url": image_b64}})
    content.append({"type": "text", "text": prompt})

    proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "no_proxy")
    _saved = {k: os.environ.pop(k, None) for k in proxy_vars}
    try:
        client = _make_zhipu_client(api_key)
        kwargs = {
            "model": model or DEFAULT_ZHIPUAI_MODEL,
            "messages": [{"role": "user", "content": content}],
        }
        if thinking_enabled:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response = client.chat.completions.create(**kwargs)
        raw_text = _extract_response_text(response)
        used_model = model or DEFAULT_ZHIPUAI_MODEL
        print(f"[ZhipuAI] {used_model} call SUCCESS")
        return {
            "ok": True,
            "provider": "zhipuai",
            "model": used_model,
            "raw_text": raw_text,
            "parsed_json": _extract_json_from_text(raw_text),
        }
    except Exception as exc:
        used_model = model or DEFAULT_ZHIPUAI_MODEL
        print(f"[ZhipuAI] {used_model} call FAILED: {exc}")
        return {
            "ok": False,
            "provider": "zhipuai",
            "model": used_model,
            "error": str(exc),
        }
    finally:
        for k, v in _saved.items():
            if v is not None:
                os.environ[k] = v


def extract_screenshot_semantics(screenshot_path: Optional[str]) -> Dict[str, Any]:
    """
    Paper mapping: screenshot-level global interface context.

    This is a lightweight visual-semantic summary available without a VLM:
    dimensions, brightness, color statistics, and coarse layout density. It does
    not replace a captioning model, but it makes the screenshot actively used by
    the pipeline instead of storing only a path.
    """
    if not screenshot_path or not os.path.exists(screenshot_path):
        return {"available": False, "reason": "missing_screenshot"}

    try:
        from PIL import Image
    except Exception as exc:
        return {"available": False, "reason": f"PIL unavailable: {exc}"}

    try:
        with Image.open(screenshot_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            small = rgb.resize((32, 32))
            pixels = list(small.getdata())
            avg_r = sum(pixel[0] for pixel in pixels) / len(pixels)
            avg_g = sum(pixel[1] for pixel in pixels) / len(pixels)
            avg_b = sum(pixel[2] for pixel in pixels) / len(pixels)
            brightness = (avg_r + avg_g + avg_b) / 3
            color_variance = sum(
                abs(pixel[0] - avg_r) + abs(pixel[1] - avg_g) + abs(pixel[2] - avg_b)
                for pixel in pixels
            ) / len(pixels)
            return {
                "available": True,
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 4) if height else None,
                "average_rgb": [round(avg_r, 2), round(avg_g, 2), round(avg_b, 2)],
                "brightness": round(brightness, 2),
                "color_variance": round(color_variance, 2),
                "visual_density": "high" if color_variance > 80 else "medium" if color_variance > 35 else "low",
            }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def _get_baidu_ocr_access_token(api_key: str, secret_key: str) -> Optional[str]:
    import requests

    url = "https://aip.baidubce.com/oauth/2.0/token"
    params = {
        "grant_type": "client_credentials",
        "client_id": api_key,
        "client_secret": secret_key,
    }
    response = requests.post(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("access_token")


def _ocr_with_baidu(screenshot_path: str) -> List[Dict[str, Any]]:
    """
    Real OCR provider: Baidu accurate_basic.

    Configure with environment variables:
    BAIDU_OCR_API_KEY and BAIDU_OCR_SECRET_KEY.
    """
    import requests

    api_key = os.getenv("BAIDU_OCR_API_KEY")
    secret_key = os.getenv("BAIDU_OCR_SECRET_KEY")
    if not api_key or not secret_key:
        return []

    token = _get_baidu_ocr_access_token(api_key, secret_key)
    if not token:
        return []

    with open(screenshot_path, "rb") as image_file:
        image_b64 = base64.b64encode(image_file.read()).decode("utf-8")

    url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token={token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = requests.post(
        url,
        headers=headers,
        data={
            "image": image_b64,
            "detect_direction": "false",
            "paragraph": "false",
            "probability": "false",
        },
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    return [
        {
            "text": item.get("words", ""),
            "bounds": None,
            "source": "ocr:baidu",
        }
        for item in result.get("words_result", [])
        if item.get("words")
    ]


def _ocr_with_tesseract(screenshot_path: str) -> List[Dict[str, Any]]:
    """
    Real OCR provider: pytesseract, when installed locally.

    This is optional and dependency-light: if pytesseract/Tesseract is not
    installed, the pipeline simply falls through to other providers.
    """
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        return []

    try:
        text = pytesseract.image_to_string(Image.open(screenshot_path), lang=os.getenv("TESSERACT_LANG", "chi_sim+eng"))
    except Exception:
        return []

    return [
        {
            "text": line.strip(),
            "bounds": None,
            "source": "ocr:tesseract",
        }
        for line in text.splitlines()
        if line.strip()
    ]


def extract_text_from_screenshot(
    screenshot_path: Optional[str],
    provider: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Paper mapping: screenshots are captured and processed with OCR.

    provider:
    - "baidu": use Baidu OCR with BAIDU_OCR_API_KEY/BAIDU_OCR_SECRET_KEY.
    - "tesseract": use local pytesseract if installed.
    - "auto": try Baidu first, then Tesseract.
    """
    if not screenshot_path or not os.path.exists(screenshot_path):
        return []

    selected_provider = provider or os.getenv("CAPTCHA_OCR_PROVIDER", "auto")
    if selected_provider in {"auto", "baidu"}:
        try:
            baidu_items = _ocr_with_baidu(screenshot_path)
            if baidu_items or selected_provider == "baidu":
                return baidu_items
        except Exception:
            if selected_provider == "baidu":
                return []

    if selected_provider in {"auto", "tesseract"}:
        return _ocr_with_tesseract(screenshot_path)

    return []


def relative_position(
    center: Optional[Tuple[float, float]],
    screen_size: Optional[Dict[str, int]],
) -> Optional[str]:
    """Encode the paper's spatial layout cue as a coarse screen position."""
    if not center or not screen_size:
        return None
    width = screen_size.get("width", 0)
    height = screen_size.get("height", 0)
    if width <= 0 or height <= 0:
        return None

    x, y = center
    horizontal = "left" if x < width / 3 else "right" if x > width * 2 / 3 else "center"
    vertical = "top" if y < height / 3 else "bottom" if y > height * 2 / 3 else "middle"
    return f"{vertical}-{horizontal}"


def is_system_component(component: Component) -> bool:
    resource_id = component.get("@resource-id", "") or ""
    package = component.get("@package", "") or ""
    return "com.android.systemui" in resource_id or "com.android.systemui" in package


def getAllComponents(jsondata: Dict[str, Any], include_containers: bool = False) -> List[Component]:
    """
    Legacy-compatible component traversal.

    Paper mapping:
    - Component information: collect resource-id, text, class, package, bounds.
    - Structural and spatial context: preserve depth, parent, sibling, semantic row, and spatial metadata.

    Compatibility:
    - Existing shiyan.py calls getAllComponents(data_dict). That still returns
      leaf components by default.
    - New callers can pass include_containers=True to preserve layout containers.
    """
    root = jsondata["hierarchy"]
    queue = [(root, None, 0, 0, 1)]
    results: List[Component] = []

    while queue:
        node, parent, depth, sibling_index, sibling_count = queue.pop(0)
        if not isinstance(node, dict):
            continue

        children = _as_list(node.get("node"))
        should_collect = include_containers or not children

        if should_collect and not is_system_component(node):
            copied = dict(node)
            copied["__depth"] = depth
            copied["__sibling_index"] = sibling_index
            copied["__sibling_count"] = sibling_count
            copied["__parent_class"] = parent.get("@class") if isinstance(parent, dict) else None
            copied["__parent_resource_id"] = parent.get("@resource-id") if isinstance(parent, dict) else None
            results.append(copied)

        for index, child in enumerate(children):
            queue.append((child, node, depth + 1, index, len(children)))

    return results


def find_EditText(jsondata: Dict[str, Any]) -> List[Component]:
    """Legacy-compatible EditText finder."""
    edit_classes = {"android.widget.EditText", "android.widget.AutoCompleteTextView"}
    return [
        component
        for component in getAllComponents(jsondata)
        if component.get("@class") in edit_classes
    ]


def get_basic_info(e_component: Component) -> Dict[str, Optional[str]]:
    """
    Legacy-compatible primitive textual extraction.

    Paper mapping:
    - Component-level textual semantics: extract id, class, text, label/content-desc, and
      package. This fixes the old 'class' 'label' string-concatenation bug.
    """
    return {
        "id": e_component.get("@resource-id"),
        "class": e_component.get("@class"),
        "text": e_component.get("@text"),
        "label": e_component.get("@label"),
        "text-hint": e_component.get("@content-desc") or e_component.get("@hint"),
        "app_name": e_component.get("@package"),
    }


def normalize_component(component: Component, screen_size: Optional[Dict[str, int]] = None) -> Component:
    """
    Convert raw UIAutomator attributes into the paper's component-level record.
    """
    bounds = component.get("@bounds")
    center = bounds_center(bounds)
    return {
        "resource_id": component.get("@resource-id"),
        "text": component.get("@text"),
        "hint": component.get("@hint"),
        "content_desc": component.get("@content-desc"),
        "class": component.get("@class"),
        "package": component.get("@package"),
        "bounds": bounds,
        "center": center,
        "area": bounds_area(bounds),
        "relative_position": relative_position(center, screen_size),
        "clickable": component.get("@clickable") == "true",
        "enabled": component.get("@enabled") == "true",
        "focusable": component.get("@focusable") == "true",
        "visible": component.get("@visible-to-user", "true") == "true",
        "depth": component.get("__depth"),
        "sibling_index": component.get("__sibling_index"),
        "sibling_count": component.get("__sibling_count"),
        "parent_class": component.get("__parent_class"),
        "parent_resource_id": component.get("__parent_resource_id"),
        "raw": component,
    }


def filter_interactive_components(components: List[Component]) -> List[Component]:
    """
    Paper mapping: Component Filtering.

    Keep visible components that satisfy clickable=true, enabled=true, or
    focusable=true. This is the paper's first-stage candidate reduction before
    CAPTCHA recognition and solving.
    """
    return [
        component
        for component in components
        if component.get("visible", True)
        and (
            component.get("clickable")
            or component.get("enabled")
            or component.get("focusable")
        )
    ]


def _lower_join(values: List[Any]) -> str:
    return " ".join(str(value).lower() for value in values if value)


def _dedupe_preserve_order(values: List[Any]) -> List[Any]:
    seen = set()
    results = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        results.append(value)
    return results


def _extract_keyword_set(context_text: str) -> List[str]:
    """
    Paper retrieval input: K_c/K_e keyword sets.

    Keywords are matched from textual attributes and OCR text. Returning a set
    rather than a hit count lets retrieval use the Jaccard formula in the paper.
    """
    lowered = context_text.lower()
    return [keyword for keyword in VERIFICATION_KEYWORDS if keyword in lowered]


def _jaccard_similarity(current_keywords: List[str], example_keywords: List[str]) -> float:
    current_set = {keyword.lower() for keyword in current_keywords}
    example_set = {keyword.lower() for keyword in example_keywords}
    union = current_set | example_set
    if not union:
        return 0.0
    return len(current_set & example_set) / len(union)


def _normalized_component_centers(gui_context: GuiContext) -> List[Tuple[float, float]]:
    screen_size = gui_context.get("global_interface_context", {}).get("screen_size") or {}
    width = screen_size.get("width") or 0
    height = screen_size.get("height") or 0
    if width <= 0 or height <= 0:
        return []

    centers = []
    for component in gui_context.get("components", []):
        center = _component_center(component)
        if center:
            x, y = center
            centers.append((max(0.0, min(1.0, x / width)), max(0.0, min(1.0, y / height))))
    return centers


def _coordinate_structural_similarity(
    current_centers: List[Tuple[float, float]],
    example_centers: List[Tuple[float, float]],
) -> float:
    """
    Paper retrieval input: lightweight coordinate-based structural similarity.

    S_struct = 1 - 1/(n*sqrt(2)) * sum_i min_j distance(p_i, p'_j)
    """
    if not current_centers or not example_centers:
        return 0.0

    distance_sum = 0.0
    for x_i, y_i in current_centers:
        distance_sum += min(
            math.sqrt((x_i - x_j) ** 2 + (y_i - y_j) ** 2)
            for x_j, y_j in example_centers
        )
    score = 1 - distance_sum / (len(current_centers) * math.sqrt(2))
    return max(0.0, min(1.0, score))


def _context_text(gui_context: GuiContext) -> str:
    """
    Paper recognition input: component-level textual semantics.

    This gathers hierarchy text, resource IDs, content descriptions, hints, and
    OCR text into a single searchable semantic surface for type inference.
    """
    text_units = []
    for item in gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", [])):
        text_units.extend(
            [
                item.get("text"),
                item.get("hint"),
                item.get("content_desc"),
                item.get("resource_id"),
            ]
        )
    return _lower_join(text_units)


def _context_structural_signals(gui_context: GuiContext) -> List[str]:
    """
    Paper recognition input: structural and spatial context.

    The paper uses spatial layout and component organization. This deterministic
    implementation extracts coarse signals that can be used for retrieval and
    downstream LLM prompts without changing the existing data model.
    """
    signals = []
    components = gui_context.get("components", [])

    for component in components:
        class_name = component.get("class")
        position = component.get("relative_position")
        if class_name:
            signals.append(str(class_name))
        if position:
            signals.append(str(position))

        parsed = parse_bounds(component.get("bounds"))
        if parsed:
            left, top, right, bottom = parsed
            width = right - left
            height = bottom - top
            if width > height * 3:
                signals.append("wide_control")
            if component.get("clickable") and "checkbox" in str(class_name).lower():
                signals.append("checkbox")

    image_like = [
        component
        for component in components
        if "image" in str(component.get("class", "")).lower()
        or "view" in str(component.get("class", "")).lower()
    ]
    if len(image_like) >= 4:
        signals.append("grid_like")

    return signals


def _is_excluded_runtime_context(gui_context: GuiContext) -> bool:
    """
    Recognition guardrail: exclude non-app verification surfaces.

    The paper's global interface context should prevent system launcher/home
    screens from being treated as CAPTCHA pages. Launcher grids look visually
    similar to image-selection CAPTCHAs, so they must be rejected before type
    inference.
    """
    global_context = gui_context.get("global_interface_context", {})
    package_values = _lower_join(
        [
            global_context.get("activity"),
            global_context.get("dominant_package"),
        ]
    )
    excluded_packages = [
        "com.android.launcher",
        "launcher3",
        "com.android.systemui",
        "com.miui.home",
        "com.huawei.android.launcher",
        "com.oppo.launcher",
        "com.vivo.launcher",
    ]
    return any(package in package_values for package in excluded_packages)


def _has_captcha_task_semantics(context_text: str) -> bool:
    """
    Recognition guardrail: require verification/task semantics.

    Structure alone is not enough. A CAPTCHA page should expose instruction
    semantics such as verify, drag, select all, click in order, rotate, etc.
    """
    task_keywords = [
        "captcha",
        "verify",
        "verification",
        "select all",
        "click in order",
        "drag",
        "slider",
        "rotate",
        "matching",
        "验证码",
        "验证",
        "校验",
        "安全验证",
        "选择所有",
        "请选择",
        "依次",
        "点击",
        "拖动",
        "滑块",
        "滑动",
        "旋转",
        "拼图",
    ]
    return any(keyword in context_text for keyword in task_keywords)


def _passes_type_specific_gate(captcha_type: str, context_text: str, context_signals: List[str]) -> bool:
    """
    Recognition guardrail: make CAPTCHA type inference stricter.

    In particular, image-selection requires both grid structure and explicit
    task instructions. This prevents normal icon grids from being recognized as
    CAPTCHA challenges.
    """
    signal_set = {signal.lower() for signal in context_signals}
    if captcha_type == "image_selection":
        has_grid = "grid_like" in signal_set
        has_instruction = any(
            keyword in context_text
            for keyword in ["select all", "选择所有", "请选择", "containing", "matching", "包含", "验证", "验证码"]
        )
        return has_grid and has_instruction
    if captcha_type == "recaptcha":
        return any(keyword in context_text for keyword in ["recaptcha", "not a robot", "我不是机器人"])
    return True


def build_detection_prompt(gui_context: GuiContext, candidate_components: List[Dict[str, Any]]) -> str:
    """
    Paper module: CAPTCHA detection and localization prompt.

    The prompt is kept as data so a caller can submit it to an LLM. The local
    recognizer still returns deterministic output when no model credentials are
    configured.
    """
    global_context = gui_context.get("global_interface_context", {})
    screenshot_semantics = global_context.get("screenshot_semantics")
    candidates = [
        {
            "text": item.get("text"),
            "content_desc": item.get("content_desc"),
            "resource_id": item.get("resource_id"),
            "class": item.get("class"),
            "bounds": item.get("bounds"),
            "candidate_score": item.get("candidate_score"),
            "reasons": item.get("candidate_reasons"),
        }
        for item in candidate_components[:12]
    ]
    return "\n".join(
        [
            "Determine whether the current mobile interface contains a CAPTCHA and localize CAPTCHA-related components.",
            "Use textual attributes, OCR text, structural relationships, spatial layout, and screenshot-derived metadata.",
            f"Global interface context: {global_context}",
            f"Screenshot-derived metadata: {screenshot_semantics}",
            f"Candidate components: {candidates}",
            "Return JSON with: is_captcha, localized_components, evidence.",
        ]
    )


def detect_and_localize_captcha(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Paper module: CAPTCHA detection and localization.

    Candidate components are first selected using visible/clickable/enabled/
    focusable attributes, then ranked with verification-oriented keywords and
    layout signals. The output is consumed by type inference and solving.
    """
    context_text = _context_text(gui_context)
    context_signals = _context_structural_signals(gui_context)
    interactive = gui_context.get("interactive_candidates", [])
    components = interactive or gui_context.get("components", [])

    keyword_set = _extract_keyword_set(context_text)
    localized = []
    for component in components:
        component_text = _component_text(component)
        matched_keywords = [keyword for keyword in VERIFICATION_KEYWORDS if keyword in component_text]
        reasons = [f"keyword:{keyword}" for keyword in matched_keywords]
        if component.get("clickable"):
            reasons.append("clickable")
        if component.get("focusable"):
            reasons.append("focusable")
        if component.get("enabled"):
            reasons.append("enabled")
        if component.get("relative_position") in {"middle-center", "middle-left", "middle-right"}:
            reasons.append(f"position:{component.get('relative_position')}")

        score = len(matched_keywords) * 2 + sum(1 for reason in reasons if not reason.startswith("keyword:"))
        if matched_keywords or score >= 2:
            localized_component = dict(component)
            localized_component["candidate_score"] = score
            localized_component["candidate_reasons"] = _dedupe_preserve_order(reasons)
            localized.append(localized_component)

    localized = sorted(localized, key=lambda item: item.get("candidate_score", 0), reverse=True)
    has_task_semantics = _has_captcha_task_semantics(context_text)
    is_excluded = _is_excluded_runtime_context(gui_context)
    has_structural_hint = any(
        signal in {item.lower() for item in context_signals}
        for signal in ["grid_like", "wide_control", "checkbox"]
    )
    is_captcha_candidate = not is_excluded and has_task_semantics and (bool(localized) or has_structural_hint)

    prompt = build_detection_prompt(gui_context, localized)
    return {
        "is_captcha_candidate": is_captcha_candidate,
        "localized_components": localized[:20],
        "candidate_count": len(localized),
        "keyword_set": keyword_set,
        "structural_signals": _dedupe_preserve_order(context_signals),
        "evidence": {
            "has_task_semantics": has_task_semantics,
            "has_structural_hint": has_structural_hint,
            "excluded_runtime_context": is_excluded,
        },
        "llm_prompt": prompt,
    }


def _keyword_overlap_score(context_text: str, keywords: List[str]) -> float:
    if not keywords:
        return 0.0
    hits = sum(1 for keyword in keywords if keyword.lower() in context_text)
    return hits / len(keywords)


def _structural_overlap_score(context_signals: List[str], expected_signals: List[str]) -> float:
    if not expected_signals:
        return 0.0
    normalized_signals = {signal.lower() for signal in context_signals}
    hits = 0
    for expected in expected_signals:
        expected_lower = expected.lower()
        if expected_lower in normalized_signals:
            hits += 1
            continue
        if any(expected_lower in signal for signal in normalized_signals):
            hits += 1
    return hits / len(expected_signals)


def retrieve_similar_captcha_examples(
    gui_context: GuiContext,
    top_k: int = 3,
    alpha: float = 0.6,
) -> List[Dict[str, Any]]:
    """
    Paper module: similarity-based retrieval.

    Implements S = alpha * S_text + (1 - alpha) * S_struct, matching the paper's
    retrieval-augmented recognition stage. The retrieved examples become
    evidence for type inference and can also be passed to an LLM prompt later.
    """
    context_text = _context_text(gui_context)
    context_keywords = _extract_keyword_set(context_text)
    context_centers = _normalized_component_centers(gui_context)
    context_signals = _context_structural_signals(gui_context)
    scored_examples = []

    for example in CAPTCHA_EXAMPLE_POOL:
        text_score = _jaccard_similarity(context_keywords, example["keywords"])
        struct_score = _coordinate_structural_similarity(context_centers, example.get("normalized_centers", []))
        if struct_score == 0.0:
            struct_score = _structural_overlap_score(context_signals, example["structural_signals"])
        score = alpha * text_score + (1 - alpha) * struct_score
        scored_examples.append(
            {
                **example,
                "matched_keywords": sorted(
                    {keyword.lower() for keyword in context_keywords}
                    & {keyword.lower() for keyword in example["keywords"]}
                ),
                "text_score": round(text_score, 4),
                "structural_score": round(struct_score, 4),
                "score": round(score, 4),
            }
        )

    return sorted(scored_examples, key=lambda item: item["score"], reverse=True)[:top_k]


def build_recognition_prompt(gui_context: GuiContext, retrieved_examples: List[Dict[str, Any]]) -> str:
    """
    Paper module: LLM-based type inference prompt construction.

    This module does not call a model directly. It creates the exact structured
    prompt payload needed by the paper's LLM reasoning stage, while the current
    deterministic recognizer keeps legacy scripts runnable without credentials.
    """
    global_context = gui_context.get("global_interface_context", {})
    detection = gui_context.get("captcha_detection", {})
    primitive_items = gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", []))[:12]
    structural_items = gui_context.get("structural_spatial_context", gui_context.get("structural_contextual_layer", []))[:12]
    example_lines = [
        f"- type={item['type']}, score={item['score']}, S_text={item['text_score']}, S_struct={item['structural_score']}, matched={item.get('matched_keywords', [])}"
        for item in retrieved_examples
    ]
    return "\n".join(
        [
            "Determine whether the current mobile interface contains a CAPTCHA.",
            "Use the structured GUI representation: textual cues, spatial layout, and global context.",
            f"Global interface context: {global_context}",
            f"Detection/localization result: {detection}",
            f"Component-level textual semantics sample: {primitive_items}",
            f"Structural and spatial context sample: {structural_items}",
            "Retrieved similar examples:",
            *example_lines,
            "Return JSON with: is_captcha, captcha_type, confidence, evidence.",
        ]
    )


def _compact_global_interface_context(gui_context: GuiContext) -> Dict[str, Any]:
    global_context = gui_context.get("global_interface_context", {})
    return {
        key: value
        for key, value in global_context.items()
        if key != "screenshot_multimodal_input"
    }


def build_zhipu_recognition_prompt(gui_context: GuiContext, deterministic_result: RecognitionResult) -> str:
    """
    Prompt for GLM-4.5V CAPTCHA detection and type inference.

    The model sees the screenshot plus compact structured GUI metadata and must
    return strict JSON that can be merged into the recognition result.
    """
    payload = {
        "task": "captcha_detection_and_type_inference",
        "allowed_captcha_types": sorted(SUPPORTED_CAPTCHA_TYPES),
        "global_interface_context": _compact_global_interface_context(gui_context),
        "captcha_detection": deterministic_result.get("captcha_detection"),
        "retrieved_examples": deterministic_result.get("retrieved_examples", []),
        "deterministic_result": {
            "is_captcha": deterministic_result.get("is_captcha"),
            "captcha_type": deterministic_result.get("captcha_type"),
            "confidence": deterministic_result.get("confidence"),
            "evidence": deterministic_result.get("evidence"),
        },
        "component_textual_semantics_sample": gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", []))[:20],
        "structural_spatial_context_sample": gui_context.get("structural_spatial_context", gui_context.get("structural_contextual_layer", []))[:20],
    }
    return (
        "你是 Android 自动化测试中的 CAPTCHA 识别模块。请结合截图和结构化 GUI 信息，判断当前界面是否包含 CAPTCHA，"
        "并推断 CAPTCHA 类型。只返回 JSON，不要输出 Markdown，不要加入 emoji。\n"
        "JSON 格式必须为："
        "{\"is_captcha\": boolean, \"captcha_type\": \"none|text_selection|slider|image_rotation|image_selection|recaptcha|unknown\", "
        "\"confidence\": 0到1之间的数字, \"localized_components\": [{\"bounds\": \"[x1,y1][x2,y2]\", \"role\": \"instruction|panel|slider|button|region\"}], "
        "\"evidence\": [\"理由\"]}。\n"
        f"结构化输入：{json.dumps(payload, ensure_ascii=False, default=str)}"
    )


def refine_recognition_with_zhipuai(gui_context: GuiContext, recognition: RecognitionResult) -> RecognitionResult:
    if not _zhipu_enabled(gui_context):
        return recognition

    config = gui_context.get("model_config", {})
    screenshot_path = gui_context.get("global_interface_context", {}).get("screenshot_path")
    model_response = invoke_zhipuai_multimodal(
        prompt=build_zhipu_recognition_prompt(gui_context, recognition),
        image_paths=[screenshot_path] if screenshot_path else [],
        model=config.get("model") or DEFAULT_ZHIPUAI_MODEL,
        thinking_enabled=config.get("thinking_enabled", True),
    )

    refined = dict(recognition)
    refined["model_recognition"] = model_response
    if model_response.get("ok"):
        print("[ZhipuAI] CAPTCHA recognition stage SUCCESS")
    else:
        print(f"[ZhipuAI] CAPTCHA recognition stage FAILED, fallback to deterministic: {model_response.get('error')}")
    parsed = model_response.get("parsed_json") if model_response.get("ok") else None
    if not parsed:
        return refined

    captcha_type = str(parsed.get("captcha_type", refined.get("captcha_type", "unknown"))).strip()
    if captcha_type not in SUPPORTED_CAPTCHA_TYPES:
        captcha_type = "unknown"
    is_captcha = bool(parsed.get("is_captcha", captcha_type not in {"none", "unknown"}))
    confidence = parsed.get("confidence", refined.get("confidence", 0.0))
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except Exception:
        confidence = refined.get("confidence", 0.0)

    refined.update(
        {
            "is_captcha": is_captcha,
            "captcha_type": captcha_type if is_captcha else "none",
            "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE.get(captcha_type, 0) if is_captcha else 0,
            "confidence": round(confidence, 4),
            "localized_components": parsed.get("localized_components", refined.get("localized_components", [])),
            "evidence": parsed.get("evidence", refined.get("evidence", [])),
            "source": "zhipuai" if model_response.get("ok") else refined.get("source", "deterministic"),
        }
    )
    return refined


def infer_captcha_type(
    gui_context: GuiContext,
    retrieved_examples: List[Dict[str, Any]],
    detection_result: Optional[Dict[str, Any]] = None,
) -> RecognitionResult:
    """
    Paper module: CAPTCHA type inference.

    The paper uses LLM reasoning over structured GUI context and retrieved
    examples. This local implementation performs the same staged reasoning
    deterministically and exposes the LLM-ready prompt for later model calls.
    """
    best = retrieved_examples[0] if retrieved_examples else None
    context_text = _context_text(gui_context)
    context_signals = _context_structural_signals(gui_context)
    prompt = build_recognition_prompt(gui_context, retrieved_examples)
    detection_result = detection_result or detect_and_localize_captcha(gui_context)
    if _is_excluded_runtime_context(gui_context):
        return {
            "is_captcha": False,
            "captcha_type": "none",
            "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE["none"],
            "confidence": 0.0,
            "captcha_detection": detection_result,
            "localized_components": [],
            "retrieved_examples": retrieved_examples,
            "evidence": ["excluded_global_interface_context"],
            "llm_prompt": prompt,
        }

    if not best or best["score"] < 0.12:
        return {
            "is_captcha": False,
            "captcha_type": "none",
            "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE["none"],
            "confidence": 0.0,
            "captcha_detection": detection_result,
            "localized_components": detection_result.get("localized_components", []),
            "retrieved_examples": retrieved_examples,
            "evidence": [],
            "llm_prompt": prompt,
        }

    if not detection_result.get("is_captcha_candidate") or not _has_captcha_task_semantics(context_text):
        return {
            "is_captcha": False,
            "captcha_type": "none",
            "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE["none"],
            "confidence": 0.0,
            "captcha_detection": detection_result,
            "localized_components": detection_result.get("localized_components", []),
            "retrieved_examples": retrieved_examples,
            "evidence": ["missing_captcha_task_semantics"],
            "llm_prompt": prompt,
        }

    evidence = []
    for keyword in best["keywords"]:
        if keyword.lower() in context_text:
            evidence.append(f"keyword:{keyword}")
    for signal in best["structural_signals"]:
        if signal.lower() in {item.lower() for item in context_signals}:
            evidence.append(f"structure:{signal}")

    captcha_type = best["type"]
    if not _passes_type_specific_gate(captcha_type, context_text, context_signals):
        return {
            "is_captcha": False,
            "captcha_type": "none",
            "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE["none"],
            "confidence": 0.0,
            "captcha_detection": detection_result,
            "localized_components": detection_result.get("localized_components", []),
            "retrieved_examples": retrieved_examples,
            "evidence": [f"failed_type_specific_gate:{captcha_type}"],
            "llm_prompt": prompt,
        }

    confidence = min(1.0, best["score"] + 0.2 if evidence else best["score"])
    return {
        "is_captcha": confidence >= 0.25,
        "captcha_type": captcha_type if confidence >= 0.25 else "unknown",
        "legacy_code": CAPTCHA_TYPE_TO_LEGACY_CODE.get(captcha_type, 0) if confidence >= 0.25 else 0,
        "confidence": round(confidence, 4),
        "captcha_detection": detection_result,
        "localized_components": detection_result.get("localized_components", []),
        "retrieved_examples": retrieved_examples,
        "evidence": evidence,
        "llm_prompt": prompt,
    }


def recognize_captcha(gui_context: GuiContext) -> RecognitionResult:
    """
    Paper module: CAPTCHA Recognition.

    Full recognition flow:
    1. Consume the GUI Information Acquisition output.
    2. Detect and localize CAPTCHA-related components.
    3. Retrieve similar examples using textual + coordinate-structural similarity.
    4. Infer CAPTCHA existence and category.
    5. Return legacy code for compatibility with shiyan.py branches.
    """
    detection_result = detect_and_localize_captcha(gui_context)
    gui_context["captcha_detection"] = detection_result
    retrieved_examples = retrieve_similar_captcha_examples(gui_context)
    recognition = infer_captcha_type(gui_context, retrieved_examples, detection_result)
    return refine_recognition_with_zhipuai(gui_context, recognition)


def _legacy_components_to_context(components: List[Component]) -> GuiContext:
    """
    Compatibility bridge for the old check_type_verification(components) API.

    The previous implementation inspected raw component keywords directly. That
    conflicts with the paper because recognition must be based on structured GUI
    context. This bridge converts the old input into the same context shape used
    by the new acquisition pipeline.
    """
    normalized_components = []
    for component in components:
        if "@class" in component:
            normalized_components.append(normalize_component(component))
        else:
            # Accept already-normalized component dictionaries produced by
            # build_gui_context/acquire_gui_information.
            normalized_components.append(dict(component))
    component_textual_semantics = [
        {
            "text": component.get("text") or component.get("@text"),
            "hint": component.get("hint") or component.get("@hint"),
            "content_desc": component.get("content_desc") or component.get("@content-desc"),
            "resource_id": component.get("resource_id") or component.get("@resource-id"),
            "bounds": component.get("bounds") or component.get("@bounds"),
            "source": "legacy_components",
        }
        for component in normalized_components
    ]
    return {
        "raw_hierarchy": None,
        "components": normalized_components,
        "all_nodes": normalized_components,
        "interactive_candidates": filter_interactive_components(normalized_components),
        "component_textual_semantics": component_textual_semantics,
        "primitive_textual_layer": component_textual_semantics,  # backward-compatible alias
        "structural_spatial_context": [
            {
                "class": component.get("class"),
                "bounds": component.get("bounds"),
                "relative_position": component.get("relative_position"),
                "depth": component.get("depth"),
                "sibling_index": component.get("sibling_index"),
                "sibling_count": component.get("sibling_count"),
                "parent_class": component.get("parent_class"),
                "parent_resource_id": component.get("parent_resource_id"),
            }
            for component in normalized_components
        ],
        "structural_contextual_layer": [
            {
                "class": component.get("class"),
                "bounds": component.get("bounds"),
                "relative_position": component.get("relative_position"),
                "depth": component.get("depth"),
                "sibling_index": component.get("sibling_index"),
                "sibling_count": component.get("sibling_count"),
                "parent_class": component.get("parent_class"),
                "parent_resource_id": component.get("parent_resource_id"),
            }
            for component in normalized_components
        ],  # backward-compatible alias
        "global_interface_context": {
            "activity": None,
            "dominant_package": None,
            "screen_size": None,
            "screenshot_path": None,
            "component_count": len(normalized_components),
            "container_count": 0,
        },
    }


def check_type_verification(components: List[Component]) -> int:
    """
    Legacy-compatible recognition interface.

    The old code returned 0/1/2/3 using direct keyword checks. It is replaced by
    the paper-aligned recognition flow, then mapped back to the legacy numeric
    code expected by existing shiyan.py branches.
    """
    gui_context = _legacy_components_to_context(components)
    return recognize_captcha(gui_context)["legacy_code"]


def _component_center(component: Component) -> Optional[Tuple[float, float]]:
    center = component.get("center")
    if center:
        return center
    return bounds_center(component.get("bounds") or component.get("@bounds"))


def _component_text(component: Component) -> str:
    return _lower_join(
        [
            component.get("text"),
            component.get("@text"),
            component.get("hint"),
            component.get("@hint"),
            component.get("content_desc"),
            component.get("@content-desc"),
            component.get("resource_id"),
            component.get("@resource-id"),
        ]
    )


def _largest_components(components: List[Component], limit: int = 3) -> List[Component]:
    return sorted(components, key=lambda item: item.get("area", 0), reverse=True)[:limit]


def extract_captcha_components(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Paper solving module: component localization.

    Before solving a CAPTCHA, the paper localizes task instructions, CAPTCHA
    panels, image candidates, slider controls, and confirmation controls from
    the structured GUI representation generated by acquisition + recognition.
    """
    components = gui_context.get("components", [])
    interactive = gui_context.get("interactive_candidates", [])
    primitive_text = gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", []))

    instruction_keywords = [
        "click",
        "select",
        "drag",
        "slide",
        "rotate",
        "verify",
        "点击",
        "选择",
        "拖动",
        "滑动",
        "旋转",
        "验证",
    ]
    confirm_keywords = ["ok", "submit", "verify", "confirm", "确定", "提交", "验证", "完成"]
    slider_keywords = ["slider", "slide", "drag", "滑块", "滑动", "拖动", "seekbar"]

    instruction_items = [
        item
        for item in primitive_text
        if any(keyword in _lower_join([item.get("text"), item.get("hint"), item.get("content_desc"), item.get("resource_id")]) for keyword in instruction_keywords)
    ]
    confirm_buttons = [
        component
        for component in interactive
        if any(keyword in _component_text(component) for keyword in confirm_keywords)
    ]
    image_candidates = [
        component
        for component in components
        if "image" in str(component.get("class", "")).lower()
        or "view" in str(component.get("class", "")).lower()
    ]
    slider_candidates = [
        component
        for component in components
        if any(keyword in _component_text(component) for keyword in slider_keywords)
        or "seekbar" in str(component.get("class", "")).lower()
        or (
            parse_bounds(component.get("bounds")) is not None
            and (parse_bounds(component.get("bounds"))[2] - parse_bounds(component.get("bounds"))[0])
            > 3 * max(1, parse_bounds(component.get("bounds"))[3] - parse_bounds(component.get("bounds"))[1])
        )
    ]
    captcha_panels = _largest_components(image_candidates or components, limit=3)

    return {
        "instruction_items": instruction_items,
        "confirm_buttons": confirm_buttons,
        "image_candidates": image_candidates,
        "slider_candidates": slider_candidates,
        "captcha_panels": captcha_panels,
    }


def segment_candidate_regions(
    component: Component,
    rows: int = 3,
    cols: int = 3,
) -> List[Dict[str, Any]]:
    """
    Paper solving module: candidate region segmentation + coordinate indexing.

    Image-selection and reCAPTCHA tasks require partitioning a visual challenge
    into indexed candidate regions. This returns index -> bounds -> center.
    """
    parsed = parse_bounds(component.get("bounds") or component.get("@bounds"))
    if not parsed or rows <= 0 or cols <= 0:
        return []

    left, top, right, bottom = parsed
    cell_width = (right - left) / cols
    cell_height = (bottom - top) / rows
    regions = []

    for row in range(rows):
        for col in range(cols):
            x1 = left + col * cell_width
            y1 = top + row * cell_height
            x2 = x1 + cell_width
            y2 = y1 + cell_height
            regions.append(
                {
                    "index": row * cols + col + 1,
                    "row": row,
                    "col": col,
                    "bounds": (x1, y1, x2, y2),
                    "center": ((x1 + x2) / 2, (y1 + y2) / 2),
                }
            )

    return regions


def build_solving_prompt(
    gui_context: GuiContext,
    captcha_components: Dict[str, Any],
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Paper solving module: LLM-based task understanding prompt.

    The prompt connects recognition output, instructions, localized components,
    and indexed regions. A future LLM call can fill unresolved targets while the
    deterministic fallback keeps the interface stable today.
    """
    recognition = gui_context.get("captcha_recognition", {})
    global_context = gui_context.get("global_interface_context", {})
    return "\n".join(
        [
            "Generate a CAPTCHA solving action plan from structured GUI context.",
            f"Recognized CAPTCHA type: {recognition.get('captcha_type')}",
            f"Recognition confidence: {recognition.get('confidence')}",
            f"Instructions: {captcha_components.get('instruction_items', [])[:5]}",
            f"Slider candidates: {captcha_components.get('slider_candidates', [])[:3]}",
            f"Image candidates: {captcha_components.get('image_candidates', [])[:3]}",
            f"Candidate regions: {(candidate_regions or [])[:9]}",
            f"Screenshot metadata: {global_context.get('screenshot_semantics')}",
            f"Screenshot multimodal input: {global_context.get('screenshot_multimodal_input')}",
            "Return JSON actions using tap, drag, or wait_for_feedback.",
        ]
    )


def build_zhipu_solving_prompt(
    gui_context: GuiContext,
    captcha_components: Dict[str, Any],
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
    crop_offset: Optional[Tuple[int, int]] = None,
) -> str:
    recognition = gui_context.get("captcha_recognition", {})
    captcha_type = recognition.get("captcha_type")
    payload = {
        "task": "captcha_solving",
        "captcha_type": captcha_type,
        "global_interface_context": _compact_global_interface_context(gui_context),
        "instruction_items": captcha_components.get("instruction_items", [])[:10],
        "localized_components": recognition.get("localized_components", [])[:20],
        "slider_candidates": captcha_components.get("slider_candidates", [])[:5],
        "image_candidates": captcha_components.get("image_candidates", [])[:5],
        "captcha_panels": captcha_components.get("captcha_panels", [])[:5],
        "candidate_regions": (candidate_regions or [])[:12],
    }
    slider_instructions = ""
    if captcha_type == "slider":
        # Remove panel bounds to prevent the model from defaulting to panel center
        payload["captcha_panels"] = []
        crop_note = ""
        if crop_offset:
            crop_note = (
                f"注意：第一张图片是 CAPTCHA 区域的裁剪图，裁剪原点位于屏幕坐标 ({crop_offset[0]}, {crop_offset[1]})。\n"
                f"裁剪图中某点的屏幕绝对坐标 = 裁剪图内坐标 + 裁剪原点。例如裁剪图内 (100, 200) 对应屏幕 ({100 + crop_offset[0]}, {200 + crop_offset[1]})。\n"
            )
        slider_instructions = (
            "【滑块验证码求解 - 必须严格执行以下步骤】\n"
            f"{crop_note}"
            "STEP 1: 仔细观察第一张裁剪图，描述你看到的内容：背景图片中缺口（拼图缺失处）大约在图片的哪个位置（左/中/右）？\n"
            "STEP 2: 缺口有什么视觉特征？（通常有明显的浅色/白色边框、阴影边缘或形状轮廓）\n"
            "STEP 3: 找到缺口中心的精确像素位置，然后转换为屏幕绝对坐标。注意：缺口绝不可能恰好在面板正中心！\n"
            "STEP 4: 找到滑块把手（通常在验证码区域底部），确定其中心屏幕坐标。\n"
            "STEP 5: 验证：handle_center.y 等于 target_gap_center.y（同一水平线），target_gap_center.x 精确对准缺口中心。\n\n"
            "【关键警告】\n"
            "- 严禁将 target_gap_center.x 设为面板中心！面板中心不是缺口位置！\n"
            "- 你必须从图片中视觉定位缺口，不能根据 bounds 推算！\n"
            "- 如果缺口在左侧，x坐标明显小于中心；在右侧则明显大于中心。\n"
            "- 坐标系：屏幕左上角为原点(0,0)，x向右增加，y向下增加。\n\n"
        )

    return (
        "你是 CAPTCHAMind 的 CAPTCHA 求解模块。请结合截图、识别出的 CAPTCHA 类型和结构化 GUI 信息，"
        "输出可映射到屏幕坐标的求解参数。只返回 JSON，不要输出 Markdown，不要加入 emoji。\n"
        f"{slider_instructions}"
        "如果是 slider，请返回 {\"target_gap_center\": {\"x\": 数字, \"y\": 数字}, \"handle_center\": {\"x\": 数字, \"y\": 数字}, \"reasoning\": [\"理由\"]}。\n"
        "如果是 image_rotation，请返回 {\"rotation_angle\": 数字, \"reasoning\": [\"理由\"]}。\n"
        "如果是 image_selection 或 recaptcha，请返回 {\"selected_region_indexes\": [数字], \"reasoning\": [\"理由\"]}。\n"
        "如果是 text_selection，请返回 {\"ordered_click_points\": [{\"x\": 数字, \"y\": 数字}], \"reasoning\": [\"理由\"]}。\n"
        f"结构化输入：{json.dumps(payload, ensure_ascii=False, default=str)}"
    )


def _crop_captcha_region(
    screenshot_path: Optional[str],
    gui_context: GuiContext,
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """Crop screenshot to the CAPTCHA panel region for higher-resolution model input.
    Returns (cropped_path, crop_offset) where crop_offset is (left, top) in screen coords."""
    if not screenshot_path or not os.path.exists(screenshot_path):
        return None, None
    recognition = gui_context.get("captcha_recognition", {})
    panel_bounds = None
    for comp in recognition.get("localized_components", []):
        if comp.get("role") == "panel":
            panel_bounds = comp.get("bounds")
            break
    if not panel_bounds:
        return None, None
    parsed = parse_bounds(panel_bounds)
    if not parsed:
        return None, None
    try:
        from PIL import Image
    except Exception:
        return None, None
    try:
        left, top, right, bottom = parsed
        with Image.open(screenshot_path) as img:
            left = max(0, left - 10)
            top = max(0, top - 10)
            right = min(img.width, right + 10)
            bottom = min(img.height, bottom + 10)
            cropped = img.crop((left, top, right, bottom))
            crop_dir = os.path.dirname(screenshot_path)
            crop_path = os.path.join(crop_dir, "captcha_cropped.png")
            cropped.save(crop_path)
            crop_offset = (left, top)
            print(f"[Solver] cropped CAPTCHA region: ({left},{top})-({right},{bottom}) offset={crop_offset} -> {crop_path}")
            return crop_path, crop_offset
    except Exception as exc:
        print(f"[Solver] crop screenshot failed: {exc}")
        return None, None


def _detect_gap_opencv(
    screenshot_path: Optional[str],
    gui_context: GuiContext,
    captcha_components: Dict[str, Any],
) -> Optional[Tuple[int, int]]:
    """Use OpenCV to locate the slider gap in the background image.

    Two-stage detection:
    1. Contour-based: find the gap border outline via Canny edge detection
    2. Edge-density scan: slide a window horizontally, find peak edge concentration
    Falls back to method 2 if method 1 produces no credible candidate.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("[Solver] OpenCV not available, skipping gap detection")
        return None

    if not screenshot_path or not os.path.exists(screenshot_path):
        return None

    # Determine the background area (above the slider track)
    slider_candidates = captcha_components.get("slider_candidates", [])
    slider_bounds = None
    for cand in slider_candidates:
        parsed = parse_bounds(cand.get("bounds"))
        if parsed:
            slider_bounds = parsed
            break

    recognition = gui_context.get("captcha_recognition", {})
    panel_bounds = None
    for comp in recognition.get("localized_components", []):
        if comp.get("role") == "panel":
            panel_bounds = parse_bounds(comp.get("bounds"))
            break
    if not panel_bounds:
        for panel in captcha_components.get("captcha_panels", []):
            panel_bounds = parse_bounds(panel.get("bounds"))
            if panel_bounds:
                break

    if not panel_bounds and not slider_bounds:
        return None

    img = cv2.imread(screenshot_path)
    if img is None:
        return None

    h, w = img.shape[:2]

    if panel_bounds:
        bg_left, bg_top, bg_right, bg_bottom = panel_bounds
    else:
        bg_left, bg_top, bg_right, bg_bottom = slider_bounds
        bg_top = max(0, bg_top - 200)

    if slider_bounds:
        bg_bottom = min(bg_bottom, slider_bounds[1])

    bg_left = max(0, bg_left)
    bg_top = max(0, bg_top)
    bg_right = min(w, bg_right)
    bg_bottom = min(h, bg_bottom)

    if bg_right <= bg_left or bg_bottom <= bg_top:
        return None

    bg_region = img[bg_top:bg_bottom, bg_left:bg_right]
    gray = cv2.cvtColor(bg_region, cv2.COLOR_BGR2GRAY)
    bh, bw = gray.shape[:2]

    # === Method 1: Contour-based gap detection ===
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    region_area = bw * bh
    min_area = region_area * 0.003
    max_area = region_area * 0.35

    best_contour = None
    best_score = -1

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        rx, ry, cw, ch = cv2.boundingRect(contour)
        aspect_ratio = max(cw, ch) / max(1, min(cw, ch))
        if aspect_ratio > 4:
            continue

        perimeter = cv2.arcLength(contour, True)
        if perimeter < 1:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)

        score = area
        if 0.05 < circularity < 0.85:
            score *= 1.5

        # Boost score for contours near the middle third of width (common gap position)
        contour_cx = rx + cw / 2
        if bw * 0.2 < contour_cx < bw * 0.8:
            score *= 1.3

        if score > best_score:
            best_score = score
            best_contour = contour

    if best_contour is not None:
        M = cv2.moments(best_contour)
        if M["m00"] > 0:
            cx = int(M["m10"] / M["m00"]) + bg_left
            cy = int(M["m01"] / M["m00"]) + bg_top
            print(f"[Solver] OpenCV contour method: gap at ({cx}, {cy})")
            return (cx, cy)

    # === Method 2: Edge-density horizontal scan ===
    # Divide the background into vertical strips, find peak edge density.
    # The gap border creates a vertical band of concentrated edges.
    strip_width = max(8, bw // 40)
    edge_col_sum = np.sum(edges, axis=0)  # sum edges per column
    # Smooth with a moving window
    kernel = np.ones(strip_width) / strip_width
    edge_density = np.convolve(edge_col_sum, kernel, mode='same')

    # Find the peak that's not at the very edges
    search_start = int(bw * 0.08)
    search_end = int(bw * 0.92)
    if search_end > search_start:
        peak_idx = search_start + np.argmax(edge_density[search_start:search_end])
        peak_value = edge_density[peak_idx]
        mean_density = np.mean(edge_density[search_start:search_end])
        if peak_value > mean_density * 1.3:
            cx = bg_left + peak_idx
            cy = bg_top + bh // 2
            print(f"[Solver] OpenCV edge-density method: gap at ({cx}, {cy}) (peak={peak_value:.0f}, mean={mean_density:.0f})")
            return (cx, cy)

    print("[Solver] OpenCV: no gap detected by either method")
    return None


def infer_visual_solution_with_zhipuai(
    gui_context: GuiContext,
    captcha_components: Dict[str, Any],
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if not _zhipu_enabled(gui_context):
        return {}

    config = gui_context.get("model_config", {})
    screenshot_path = gui_context.get("global_interface_context", {}).get("screenshot_path")
    cropped_path, crop_offset = _crop_captcha_region(screenshot_path, gui_context)
    image_paths = [screenshot_path] if screenshot_path else []
    if cropped_path:
        image_paths.insert(0, cropped_path)  # Send cropped image first (higher priority for model)
    model_response = invoke_zhipuai_multimodal(
        prompt=build_zhipu_solving_prompt(gui_context, captcha_components, candidate_regions, crop_offset),
        image_paths=image_paths,
        model=config.get("model") or DEFAULT_ZHIPUAI_MODEL,
        thinking_enabled=config.get("thinking_enabled", True),
    )
    parsed = model_response.get("parsed_json") if model_response.get("ok") else None
    if model_response.get("ok"):
        print("[ZhipuAI] CAPTCHA solving stage SUCCESS")
    else:
        print(f"[ZhipuAI] CAPTCHA solving stage FAILED: {model_response.get('error')}")
    if not parsed:
        return {"model_solving": model_response}
    parsed = dict(parsed)
    parsed["model_solving"] = model_response
    return parsed


def _visual_reasoning_result(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Optional MLLM output channel.

    Callers can attach model output to gui_context["mllm_result"] before
    solve_captcha(). The deterministic scaffold remains usable when no model is
    connected.
    """
    return gui_context.get("mllm_result") or gui_context.get("visual_reasoning_result") or {}


def _point_from_value(value: Any) -> Optional[Tuple[float, float]]:
    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
        if x is not None and y is not None:
            return float(x), float(y)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return None


def _region_by_index(regions: List[Dict[str, Any]], index: Any) -> Optional[Dict[str, Any]]:
    try:
        normalized_index = int(index)
    except Exception:
        return None
    for region in regions:
        if region.get("index") == normalized_index:
            return region
    return None


def _human_like_trajectory(
    start: Tuple[float, float],
    end: Tuple[float, float],
    steps: int = 6,
) -> List[Tuple[float, float]]:
    """
    Paper solving module: add small trajectory variations for drag actions.

    This is deterministic so tests are reproducible, but the path is not a
    perfectly straight two-point line.
    """
    points = []
    for step in range(steps + 1):
        ratio = step / steps
        x = start[0] + (end[0] - start[0]) * ratio
        y = start[1] + (end[1] - start[1]) * ratio
        if 0 < step < steps:
            y += math.sin(ratio * math.pi) * 3
            x += math.sin(ratio * math.pi * 2) * 2
        points.append((round(x, 2), round(y, 2)))
    return points


def _tap_action(point: Optional[Tuple[float, float]], reason: str) -> Optional[Dict[str, Any]]:
    if not point:
        return None
    return {"type": "tap", "point": point, "reason": reason}


def _drag_action(
    start: Optional[Tuple[float, float]],
    end: Optional[Tuple[float, float]],
    reason: str,
) -> Optional[Dict[str, Any]]:
    if not start or not end:
        return None
    return {
        "type": "drag",
        "start": start,
        "end": end,
        "duration": 0.8,
        "trajectory": _human_like_trajectory(start, end),
        "reason": reason,
    }


def _build_text_selection_solution(gui_context: GuiContext, captcha_components: Dict[str, Any]) -> Dict[str, Any]:
    """
    Paper solving module: Text Selection CAPTCHA.

    Uses OCR/text boxes and coordinate grounding. If exact character targets are
    not available yet, the action plan explicitly marks that LLM/vision
    resolution is required instead of falling back to unrelated old logic.
    """
    visual_result = _visual_reasoning_result(gui_context)
    ordered_points = visual_result.get("ordered_click_points") or visual_result.get("click_points") or []
    actions = []
    for index, point_value in enumerate(ordered_points, start=1):
        action = _tap_action(_point_from_value(point_value), f"text_selection_model_point:{index}")
        if action:
            actions.append(action)

    text_items = [
        item for item in gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", [])) if item.get("bounds") and item.get("text")
    ]
    text_items = sorted(
        text_items,
        key=lambda item: (
            parse_bounds(item.get("bounds"))[1] if parse_bounds(item.get("bounds")) else 0,
            parse_bounds(item.get("bounds"))[0] if parse_bounds(item.get("bounds")) else 0,
        ),
    )
    if not actions:
        for item in text_items:
            if len(actions) >= 3:
                break
            center = bounds_center(item.get("bounds"))
            action = _tap_action(center, f"text_selection_candidate:{item.get('text')}")
            if action:
                actions.append(action)

    return {
        "status": "ready" if actions else "needs_semantic_target_resolution",
        "captcha_type": "text_selection",
        "actions": actions,
        "ordered_text_candidates": text_items,
        "visual_reasoning_result": visual_result,
        "notes": "Text candidates are grounded from component-level textual semantics bounds.",
    }


def _build_slider_solution(gui_context: GuiContext, captcha_components: Dict[str, Any]) -> Dict[str, Any]:
    """
    Paper solving module: Slider CAPTCHA.

    Computes a coordinate-based drag in the unified UI coordinate space. When a
    target gap is unavailable, it uses the detected slider track as a safe action
    plan placeholder for later visual matching refinement.

    Priority for slider handle localization:
    1. LLM recognition localized_components with role="slider"
    2. LLM solving response handle_center
    3. Keyword-based slider_candidates (fallback)

    Gap localization:
    1. LLM prediction (if not defaulting to panel center)
    2. OpenCV edge-detection fallback
    """
    visual_result = _visual_reasoning_result(gui_context)
    recognition = gui_context.get("captcha_recognition", {})

    # Prefer LLM-identified slider from recognition stage
    slider_from_llm = None
    for comp in recognition.get("localized_components", []):
        if comp.get("role") == "slider":
            slider_from_llm = {"bounds": comp.get("bounds")}
            break

    slider = (slider_from_llm
              or (captcha_components.get("slider_candidates") or captcha_components.get("captcha_panels") or [None])[0])
    parsed = parse_bounds((slider or {}).get("bounds"))
    if slider_from_llm and slider_from_llm is slider:
        print(f"[Solver] using LLM-identified slider: {slider_from_llm.get('bounds')}")
    elif parsed:
        print(f"[Solver] using keyword-matched slider: {slider.get('bounds')}")

    # Compute panel center for detecting model default behavior
    panel_center_x = None
    for comp in recognition.get("localized_components", []):
        if comp.get("role") == "panel":
            pb = parse_bounds(comp.get("bounds"))
            if pb:
                panel_center_x = (pb[0] + pb[2]) / 2
            break

    # Get model-predicted gap
    target_gap = _point_from_value(visual_result.get("target_gap") or visual_result.get("target_gap_center"))

    # Detect model defaulting to panel center (within 3px tolerance)
    if target_gap and panel_center_x is not None:
        if abs(target_gap[0] - panel_center_x) < 3:
            print(f"[Solver] WARNING: model gap.x={target_gap[0]:.0f} matches panel center {panel_center_x:.0f} - model may have defaulted")
            # Try OpenCV fallback
            screenshot_path = gui_context.get("global_interface_context", {}).get("screenshot_path")
            cv_gap = _detect_gap_opencv(screenshot_path, gui_context, captcha_components)
            if cv_gap:
                print(f"[Solver] using OpenCV-detected gap: ({cv_gap[0]}, {cv_gap[1]}) instead of model default")
                target_gap = cv_gap
            else:
                print("[Solver] OpenCV fallback failed, keeping model prediction (may be inaccurate)")

    action = None
    if parsed:
        left, top, right, bottom = parsed
        y = (top + bottom) / 2
        handle_center = (_point_from_value(visual_result.get("handle_center"))
                         or ((left + right) / 2, y))
        if target_gap:
            action = _drag_action(handle_center, (target_gap[0], handle_center[1]), "slider_gap_displacement")

    return {
        "status": "ready" if action else "needs_mllm_gap_estimation",
        "captcha_type": "slider",
        "actions": [action] if action else [],
        "visual_reasoning_prompt": build_solving_prompt(gui_context, captcha_components),
        "interaction_parameters": {
            "slider_bounds": parsed,
            "target_gap": target_gap,
        },
        "notes": "Slider solving uses LLM + OpenCV edge detection for gap localization.",
    }


def _build_rotation_solution(gui_context: GuiContext, captcha_components: Dict[str, Any]) -> Dict[str, Any]:
    """
    Paper solving module: Image Rotation CAPTCHA.

    Rotation requires estimating angle theta from visual semantics. This returns
    the coordinate scaffold and marks the semantic angle as unresolved.
    """
    visual_result = _visual_reasoning_result(gui_context)
    slider = (captcha_components.get("slider_candidates") or captcha_components.get("captcha_panels") or [None])[0]
    parsed = parse_bounds((slider or {}).get("bounds"))
    action = None
    theta = visual_result.get("rotation_angle") or visual_result.get("theta")
    if parsed and theta is not None:
        left, top, right, bottom = parsed
        slider_length = max(0, right - left)
        y = (top + bottom) / 2
        displacement = (float(theta) / 360.0) * slider_length
        start = (left + 5, y)
        end = (min(right - 5, start[0] + displacement), y)
        action = _drag_action(start, end, "rotation_angle_to_slider_displacement")

    return {
        "status": "ready" if action else "needs_rotation_angle_reasoning",
        "captcha_type": "image_rotation",
        "actions": [action] if action else [],
        "visual_reasoning_prompt": build_solving_prompt(gui_context, captcha_components),
        "interaction_parameters": {
            "slider_bounds": parsed,
            "rotation_angle": theta,
        },
        "notes": "Requires MLLM estimation of rotation angle theta before final displacement.",
    }


def _build_image_selection_solution(
    gui_context: GuiContext,
    captcha_components: Dict[str, Any],
    captcha_type: str,
) -> Dict[str, Any]:
    """
    Paper solving module: Image Selection and Google reCAPTCHA.

    Segments the CAPTCHA panel into indexed candidate regions. Target selection
    is intentionally separated from execution so LLM semantic reasoning can
    choose region indexes from this stable coordinate map.
    """
    visual_result = _visual_reasoning_result(gui_context)
    panel = (captcha_components.get("captcha_panels") or [None])[0]
    regions = segment_candidate_regions(panel) if panel else []
    selected_indexes = (
        visual_result.get("selected_region_indexes")
        or visual_result.get("selected_indices")
        or visual_result.get("grid_indices")
        or []
    )
    actions = []
    for index in selected_indexes:
        region = _region_by_index(regions, index)
        if region:
            action = _tap_action(region.get("center"), f"{captcha_type}_region:{index}")
            if action:
                actions.append(action)
    prompt = build_solving_prompt(gui_context, captcha_components, regions)
    return {
        "status": "ready" if actions else "needs_region_semantic_selection" if regions else "needs_region_segmentation",
        "captcha_type": captcha_type,
        "candidate_regions": regions,
        "selected_region_indexes": selected_indexes,
        "actions": actions,
        "llm_prompt": prompt,
        "visual_reasoning_prompt": prompt,
        "notes": "Regions are indexed for LLM target selection and coordinate grounding.",
    }


def solve_captcha(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Paper module: CAPTCHA Solving.

    Connects the previous two modules:
    GUI Information Acquisition -> CAPTCHA Recognition -> CAPTCHA Solving.

    The output is an executable action plan with explicit status. This preserves
    compatibility because no existing click/drag code is forced to change.
    """
    recognition = gui_context.get("captcha_recognition") or recognize_captcha(gui_context)
    captcha_type = recognition.get("captcha_type", "none")
    captcha_components = extract_captcha_components(gui_context)
    candidate_regions = []
    if captcha_type in {"image_selection", "recaptcha"}:
        panel = (captcha_components.get("captcha_panels") or [None])[0]
        candidate_regions = segment_candidate_regions(panel) if panel else []
    model_solution = infer_visual_solution_with_zhipuai(gui_context, captcha_components, candidate_regions)
    if model_solution:
        existing_result = _visual_reasoning_result(gui_context)
        gui_context["mllm_result"] = {**existing_result, **model_solution}
        model_keys = [k for k in model_solution if k != "model_solving"]
        print(f"[Solver] 模型求解返回字段: {model_keys}")
        if "target_gap_center" in model_solution or "target_gap" in model_solution:
            print(f"[Solver] target_gap = {model_solution.get('target_gap_center') or model_solution.get('target_gap')}")
        if "handle_center" in model_solution:
            print(f"[Solver] handle_center = {model_solution.get('handle_center')}")

    if not recognition.get("is_captcha") or captcha_type in {"none", "unknown"}:
        solution = {
            "status": "not_applicable",
            "captcha_type": captcha_type,
            "actions": [],
            "notes": "Recognition did not identify a solvable CAPTCHA.",
        }
    elif captcha_type in {"text_selection", "character"}:
        solution = _build_text_selection_solution(gui_context, captcha_components)
    elif captcha_type == "slider":
        solution = _build_slider_solution(gui_context, captcha_components)
    elif captcha_type == "image_rotation":
        solution = _build_rotation_solution(gui_context, captcha_components)
    elif captcha_type == "image_selection":
        solution = _build_image_selection_solution(gui_context, captcha_components, "image_selection")
    elif captcha_type == "recaptcha":
        solution = _build_image_selection_solution(gui_context, captcha_components, "recaptcha")
        solution["notes"] += " Re-run acquisition after each round for refreshed regions."
    else:
        solution = {
            "status": "unsupported_type",
            "captcha_type": captcha_type,
            "actions": [],
            "notes": "No type-specific solver is available.",
        }

    solution["recognized_type"] = captcha_type
    solution["recognition_confidence"] = recognition.get("confidence", 0.0)
    solution["components"] = captcha_components
    solution["max_solving_attempts"] = DEFAULT_MAX_SOLVING_ATTEMPTS
    if "llm_prompt" not in solution:
        solution["llm_prompt"] = build_solving_prompt(gui_context, captcha_components, solution.get("candidate_regions"))
    return solution


def execute_solution(device: Any, solution: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Paper solving module: coordinate-based action execution.

    This optional executor translates action plans into uiautomator2 operations.
    It is intentionally separate from solve_captcha so tests and recognition can
    run without a device and existing shiyan.py branches remain compatible.
    """
    executed = []
    for action in solution.get("actions", []):
        if action.get("type") == "tap":
            x, y = action["point"]
            device.click(x, y)
            executed.append(action)
        elif action.get("type") == "drag":
            start_x, start_y = action["start"]
            end_x, end_y = action["end"]
            device.touch.down(start_x, start_y)
            trajectory = action.get("trajectory") or []
            pause = action.get("duration", 0.8) / max(1, len(trajectory))
            for point in trajectory[1:] or [(end_x, end_y)]:
                device.touch.sleep(pause)
                device.touch.move(point[0], point[1])
            device.touch.up(end_x, end_y)
            executed.append(action)
    return executed


def summarize_gui_state(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Paper verification module: GUI state abstraction.

    The feedback loop compares compact state summaries instead of raw XML:
    component count, textual cues, recognized CAPTCHA status, and page-level
    context. This supports activity transition and CAPTCHA disappearance checks.
    """
    text_values = []
    for item in gui_context.get("component_textual_semantics", gui_context.get("primitive_textual_layer", [])):
        text_values.extend([item.get("text"), item.get("hint"), item.get("content_desc")])
    text_surface = _lower_join(text_values)
    recognition = gui_context.get("captcha_recognition") or recognize_captcha(gui_context)
    global_context = gui_context.get("global_interface_context", {})
    return {
        "activity": global_context.get("activity"),
        "dominant_package": global_context.get("dominant_package"),
        "component_count": global_context.get("component_count"),
        "text_surface": text_surface,
        "is_captcha": recognition.get("is_captcha"),
        "captcha_type": recognition.get("captcha_type"),
        "confidence": recognition.get("confidence"),
    }


def extract_feedback_signals(gui_context: GuiContext) -> Dict[str, Any]:
    """
    Paper verification module: runtime feedback extraction.

    Captures success/failure indicators from hierarchy and OCR text so failed
    attempts can be categorized and fed into the next reasoning iteration.
    """
    text_surface = summarize_gui_state(gui_context)["text_surface"]
    success_keywords = [
        "success",
        "passed",
        "verified",
        "完成",
        "成功",
        "验证通过",
        "通过",
    ]
    failure_keywords = [
        "incorrect",
        "failed",
        "try again",
        "error",
        "失败",
        "错误",
        "重试",
        "再试",
        "请重新",
    ]
    refresh_keywords = [
        "select all",
        "next",
        "skip",
        "刷新",
        "换一张",
        "重新验证",
    ]
    return {
        "success_text": [keyword for keyword in success_keywords if keyword in text_surface],
        "failure_text": [keyword for keyword in failure_keywords if keyword in text_surface],
        "refresh_text": [keyword for keyword in refresh_keywords if keyword in text_surface],
        "text_surface": text_surface,
    }


def classify_verification_outcome(
    before_context: GuiContext,
    after_context: GuiContext,
    solution: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Paper verification module: success/failure judgment.

    Success signals follow the paper: CAPTCHA disappearance, page/activity
    transition, successful status text, or verification state change. Failures
    are categorized into recognition, localization, or execution failures.
    """
    before_state = summarize_gui_state(before_context)
    after_state = summarize_gui_state(after_context)
    feedback = extract_feedback_signals(after_context)
    actions = solution.get("actions", [])

    activity_changed = before_state.get("activity") != after_state.get("activity")
    captcha_disappeared = before_state.get("is_captcha") and not after_state.get("is_captcha")
    success_text_found = bool(feedback["success_text"])
    failure_text_found = bool(feedback["failure_text"])

    if activity_changed or captcha_disappeared or success_text_found:
        return {
            "status": "success",
            "success": True,
            "failure_type": None,
            "before_state": before_state,
            "after_state": after_state,
            "feedback": feedback,
            "reason": "activity_changed_or_captcha_disappeared_or_success_text",
        }

    if not before_state.get("is_captcha"):
        failure_type = "recognition_failure"
        reason = "captcha_not_detected_before_execution"
    elif solution.get("status", "").startswith("needs_") or not actions:
        failure_type = "localization_failure"
        reason = "solver_needs_more_target_or_coordinate_information"
    elif failure_text_found or before_state.get("text_surface") != after_state.get("text_surface"):
        failure_type = "execution_failure"
        reason = "runtime_feedback_indicates_failed_or_changed_verification"
    else:
        failure_type = "execution_failure"
        reason = "no_success_signal_after_execution"

    return {
        "status": "failed",
        "success": False,
        "failure_type": failure_type,
        "before_state": before_state,
        "after_state": after_state,
        "feedback": feedback,
        "reason": reason,
    }


def build_feedback_refinement(
    verification_result: Dict[str, Any],
    previous_solution: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Paper verification module: feedback-driven refinement.

    Maps the observed failure category to the next module that should be
    refined, matching the paper's three failure classes.
    """
    failure_type = verification_result.get("failure_type")
    if verification_result.get("success"):
        return {
            "next_step": "stop",
            "prompt_update": "Verification succeeded; no further refinement is required.",
            "retry_recommended": False,
        }

    if failure_type == "recognition_failure":
        next_step = "rerun_recognition"
        prompt_update = "Re-extract hierarchical GUI context and rerun CAPTCHA type inference with updated text/layout evidence."
    elif failure_type == "localization_failure":
        next_step = "rerun_localization"
        prompt_update = "Reuse the recognized type but refine component localization, OCR boxes, segmentation, and coordinate grounding."
    else:
        next_step = "rerun_execution_strategy"
        prompt_update = "Use runtime feedback text and previous actions to adjust interaction trajectory or regenerate executable actions."

    return {
        "next_step": next_step,
        "prompt_update": prompt_update,
        "retry_recommended": True,
        "previous_status": previous_solution.get("status"),
        "previous_actions": previous_solution.get("actions", []),
        "feedback": verification_result.get("feedback", {}),
    }


def verify_and_refine(
    before_context: GuiContext,
    after_context: GuiContext,
    solution: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Paper module: Verification workflow + feedback loop.

    This pure function is easy to test: callers provide before/after contexts
    acquired via acquire_gui_information(), and it returns verification outcome
    plus refinement guidance for the next round.
    """
    verification = classify_verification_outcome(before_context, after_context, solution)
    refinement = build_feedback_refinement(verification, solution)
    return {
        "verification": verification,
        "refinement": refinement,
    }


def run_verification_round(
    device: Any,
    before_context: GuiContext,
    solution: Dict[str, Any],
    save_path: str,
    execute: bool = True,
) -> Dict[str, Any]:
    """
    Paper module: closed-loop runtime verification.

    Optional convenience wrapper:
    1. Execute the current solution.
    2. Re-acquire GUI information from runtime.
    3. Verify success or categorize failure.
    4. Return feedback refinement instructions.
    """
    executed_actions = execute_solution(device, solution) if execute else []
    _, after_context, hierarchy_path = acquire_gui_information(device, save_path, capture_label="after_feedback")
    feedback_loop = verify_and_refine(before_context, after_context, solution)
    feedback_loop["executed_actions"] = executed_actions
    feedback_loop["after_hierarchy_path"] = hierarchy_path
    return feedback_loop


def build_semantic_rows(
    components: List[Component],
    screen_size: Optional[Dict[str, int]],
) -> List[Dict[str, Any]]:
    """
    Paper mapping: semantic row grouping in the Structural and Spatial Context.

    Latest Approach states that sibling/nearby components whose vertical
    coordinate difference is smaller than 5% of the screen height should be
    treated as contextually related. This function implements exactly that
    rule:

        abs(center_y_i - center_y_j) < 0.05 * screen_height

    Rows are sorted top-to-bottom, and each row records member indexes, bounds,
    texts, and classes so CAPTCHA instructions can be associated with nearby
    buttons, image regions, or sliders.
    """
    if not screen_size or not screen_size.get("height"):
        return []

    threshold = 0.05 * screen_size["height"]
    indexed_components = [
        (index, component)
        for index, component in enumerate(components)
        if component.get("center") is not None
    ]
    indexed_components.sort(key=lambda item: (item[1]["center"][1], item[1]["center"][0]))

    rows: List[Dict[str, Any]] = []
    for index, component in indexed_components:
        center_x, center_y = component["center"]
        target_row = None
        for row in rows:
            if abs(center_y - row["mean_center_y"]) < threshold:
                target_row = row
                break

        if target_row is None:
            target_row = {
                "row_id": len(rows),
                "mean_center_y": center_y,
                "component_indexes": [],
                "members": [],
            }
            rows.append(target_row)

        target_row["component_indexes"].append(index)
        target_row["members"].append(
            {
                "index": index,
                "class": component.get("class"),
                "text": component.get("text") or component.get("content_desc"),
                "resource_id": component.get("resource_id"),
                "bounds": component.get("bounds"),
                "center": component.get("center"),
            }
        )
        target_row["mean_center_y"] = sum(member["center"][1] for member in target_row["members"]) / len(target_row["members"])

    for row in rows:
        row["members"].sort(key=lambda member: member["center"][0])
        row["component_indexes"] = [member["index"] for member in row["members"]]
        row["member_count"] = len(row["members"])

    return rows


def semantic_row_id_for_component(component_index: int, semantic_rows: List[Dict[str, Any]]) -> Optional[int]:
    for row in semantic_rows:
        if component_index in row.get("component_indexes", []):
            return row.get("row_id")
    return None


def build_gui_context(
    data_dict: Dict[str, Any],
    device_info: Optional[Dict[str, Any]] = None,
    screenshot_path: Optional[str] = None,
    ocr_items: Optional[List[Dict[str, Any]]] = None,
    model_config: Optional[Dict[str, Any]] = None,
) -> GuiContext:
    """
    Build the structured GUI representation required by the paper.

    The structure is intentionally a dictionary so existing code can adopt it
    incrementally without introducing a new object model.
    """
    screen_size = get_screen_size(device_info)
    leaf_components = getAllComponents(data_dict)
    all_nodes = getAllComponents(data_dict, include_containers=True)

    normalized_components = [
        normalize_component(component, screen_size) for component in leaf_components
    ]
    normalized_nodes = [
        normalize_component(component, screen_size) for component in all_nodes
    ]
    semantic_rows = build_semantic_rows(normalized_components, screen_size)

    # Paper cue 1: Component-level textual semantics.
    component_textual_semantics = []
    for component in normalized_components:
        if any(
            [
                component.get("text"),
                component.get("hint"),
                component.get("content_desc"),
                component.get("resource_id"),
            ]
        ):
            component_textual_semantics.append(
                {
                    "text": component.get("text"),
                    "hint": component.get("hint"),
                    "content_desc": component.get("content_desc"),
                    "resource_id": component.get("resource_id"),
                    "bounds": component.get("bounds"),
                    "source": "hierarchy",
                }
            )

    # Component-level textual semantics extension: OCR supplements dynamic or missing text.
    for item in ocr_items or []:
        component_textual_semantics.append(
            {
                "text": item.get("text"),
                "bounds": item.get("bounds"),
                "source": "ocr",
            }
        )

    # Paper cue 2: Structural and spatial context.
    structural_spatial_context = [
        {
            "component_index": index,
            "class": component.get("class"),
            "bounds": component.get("bounds"),
            "relative_position": component.get("relative_position"),
            "depth": component.get("depth"),
            "sibling_index": component.get("sibling_index"),
            "sibling_count": component.get("sibling_count"),
            "parent_class": component.get("parent_class"),
            "parent_resource_id": component.get("parent_resource_id"),
            "semantic_row_id": semantic_row_id_for_component(index, semantic_rows),
        }
        for index, component in enumerate(normalized_components)
    ]

    # Paper cue 3: Global interface context.
    packages = [
        component.get("package")
        for component in normalized_components
        if component.get("package")
    ]
    dominant_package = max(set(packages), key=packages.count) if packages else None
    screenshot_semantics = extract_screenshot_semantics(screenshot_path)
    screenshot_multimodal_input = encode_screenshot_for_multimodal(screenshot_path)
    global_interface_context = {
        "activity": (device_info or {}).get("currentPackageName"),
        "dominant_package": dominant_package,
        "screen_size": screen_size,
        "screenshot_path": screenshot_path,
        "screenshot_semantics": screenshot_semantics,
        "screenshot_multimodal_input": screenshot_multimodal_input,
        "component_count": len(normalized_components),
        "container_count": len(normalized_nodes) - len(normalized_components),
    }

    gui_context = {
        "raw_hierarchy": data_dict,
        "components": normalized_components,
        "all_nodes": normalized_nodes,
        "interactive_candidates": filter_interactive_components(normalized_components),
        "component_textual_semantics": component_textual_semantics,
        "primitive_textual_layer": component_textual_semantics,  # backward-compatible alias
        "structural_spatial_context": structural_spatial_context,
        "structural_contextual_layer": structural_spatial_context,  # backward-compatible alias
        "semantic_rows": semantic_rows,
        "global_interface_context": global_interface_context,
        "model_config": model_config or {
            "provider": "zhipuai" if _get_zhipu_api_key() else "deterministic",
            "enabled": bool(_get_zhipu_api_key()),
            "model": DEFAULT_ZHIPUAI_MODEL if _get_zhipu_api_key() else None,
            "thinking_enabled": bool(_get_zhipu_api_key()),
        },
    }
    # Paper connection: GUI Information Acquisition feeds CAPTCHA Recognition.
    # Downstream code can use this field directly, while legacy code can still
    # call check_type_verification(components).
    gui_context["captcha_recognition"] = recognize_captcha(gui_context)
    # Paper connection: Recognition feeds CAPTCHA Solving. The solving stage is
    # only activated after recognition confirms a CAPTCHA; non-CAPTCHA pages are
    # explicitly skipped to avoid false solving attempts on normal UI screens.
    if gui_context["captcha_recognition"].get("is_captcha"):
        gui_context["captcha_solution"] = solve_captcha(gui_context)
    else:
        gui_context["captcha_solution"] = {
            "status": "skipped_non_captcha",
            "captcha_type": "none",
            "actions": [],
            "notes": "CAPTCHA solving was skipped because recognition returned is_captcha=False.",
            "recognized_type": gui_context["captcha_recognition"].get("captcha_type", "none"),
            "recognition_confidence": gui_context["captcha_recognition"].get("confidence", 0.0),
        }
    # Paper connection: Verification and feedback loop. Runtime callers fill
    # this after executing a solution and re-acquiring the GUI state.
    gui_context["verification_feedback"] = {
        "status": "not_run",
        "usage": "Call verify_and_refine(before_context, after_context, solution) after execution.",
    }
    return gui_context


def make_capture_paths(save_path: str, capture_label: str = "gui_context") -> Dict[str, str]:
    """
    Build non-overwriting runtime artifact paths for one acquisition round.

    Earlier versions always wrote gui_context_screenshot.png. During feedback
    loops, later acquisitions overwrote the first screenshot, making the file
    look like it was not the current screen for the printed context.
    """
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", capture_label or "gui_context")
    return {
        "hierarchy": os.path.join(save_path, f"{safe_label}_hierarchy.xml"),
        "screenshot": os.path.join(save_path, f"{safe_label}_gui_context_screenshot.png"),
    }


def acquire_gui_information(
    device: Any,
    save_path: str,
    ocr_items: Optional[List[Dict[str, Any]]] = None,
    capture_screenshot: bool = True,
    model_config: Optional[Dict[str, Any]] = None,
    capture_label: str = "gui_context",
) -> Tuple[Dict[str, Any], GuiContext, str]:
    """
    Runtime GUI Information Acquisition entry point.

    Paper mapping:
    - Obtain the UIAutomator hierarchy for structural completeness.
    - Save the hierarchy for reproducibility.
    - Capture a screenshot for screenshot-level context.
    - Return a structured GUI representation for downstream recognition.

    Compatibility:
    - Returns data_dict as the first value so legacy code can keep using
      getAllComponents(data_dict), find_EditText(data_dict), etc.
    """
    os.makedirs(save_path, exist_ok=True)
    capture_paths = make_capture_paths(save_path, capture_label)
    hierarchy_path = capture_paths["hierarchy"]

    # Screenshot must be taken BEFORE hierarchy dump because
    # uiautomator dump can trigger UI highlights/refreshes that
    # alter the on-screen state.
    screenshot_path = None
    if capture_screenshot:
        screenshot_path = capture_paths["screenshot"]
        try:
            device.screenshot(screenshot_path)
            if not _is_valid_png(screenshot_path):
                screenshot_path = None
        except Exception:
            screenshot_path = None
        # Fallback: try ADB screencap if uiautomator2 screenshot failed.
        if screenshot_path is None:
            try:
                serial = getattr(device, "serial", None)
                screenshot_path = capture_screenshot_adb(capture_paths["screenshot"], serial=serial, timeout=30)
            except Exception:
                screenshot_path = None

    device_info = device.info
    page_source = device.dump_hierarchy(compressed=True, pretty=True)
    with open(hierarchy_path, "w", encoding="utf-8") as xml_file:
        xml_file.write(page_source)

    data_dict = parse_hierarchy_xml(page_source)

    if ocr_items is None:
        ocr_items = extract_text_from_screenshot(screenshot_path)

    gui_context = build_gui_context(
        data_dict=data_dict,
        device_info=device_info,
        screenshot_path=screenshot_path,
        ocr_items=ocr_items,
        model_config=model_config,
    )

    return data_dict, gui_context, hierarchy_path


def _adb_base(serial: Optional[str] = None) -> List[str]:
    command = ["adb"]
    if serial:
        command.extend(["-s", serial])
    return command


def _adb_text(args: List[str], serial: Optional[str] = None, timeout: int = 20) -> str:
    result = subprocess.run(
        _adb_base(serial) + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
        check=True,
    )
    return result.stdout.strip()


def _adb_bytes(args: List[str], serial: Optional[str] = None, timeout: int = 20) -> bytes:
    result = subprocess.run(
        _adb_base(serial) + args,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def _is_valid_png(path: Optional[str]) -> bool:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def capture_screenshot_adb(
        screenshot_path: str,
        serial: Optional[str] = None,
        timeout: int = 30,
) -> Optional[str]:
    """Safe screenshot capture tailored for Android 9.0 to avoid Windows CRLF corruption."""
    try:
        if os.path.exists(screenshot_path):
            os.remove(screenshot_path)
    except Exception:
        pass

    # Android 9.0 专属最稳妥方案：手机端落盘 + adb pull（避开不稳定的 exec-out 流传输）
    remote_path = "/data/local/tmp/captchamind_90_screenshot.png"
    try:
        # 1. 强制强洗设备端可能存在的旧残余
        subprocess.run(_adb_base(serial) + ["shell", "rm", "-f", remote_path], capture_output=True, timeout=10)

        # 2. 调用系统底层 screencap 并在手机端安全目录落盘
        _adb_text(["shell", "screencap", "-p", remote_path], serial=serial, timeout=timeout)

        # 3. 通过 pull 把完好的二进制文件拉回电脑（通过通道传输，不经过控制台流，免疫CRLF污染）
        subprocess.run(
            _adb_base(serial) + ["pull", remote_path, screenshot_path],
            capture_output=True,
            timeout=timeout,
            check=True,
        )

        # 4. 验证图片合法性
        if _is_valid_png(screenshot_path):
            return screenshot_path

    except Exception as e:
        print(f"[ADB Screenshot Warning] Drop-and-pull method failed: {e}")
        pass

    return None

def get_adb_device_info(serial: Optional[str] = None) -> Dict[str, Any]:
    """
    ADB fallback for page-level device metadata.

    This avoids the phone-side uiautomator2 JSON-RPC service when it returns
    502, while still providing the global interface context with screen size
    and activity/package hints.
    """
    info: Dict[str, Any] = {}

    try:
        size_output = _adb_text(["shell", "wm", "size"], serial=serial)
        match = re.search(r"(\d+)x(\d+)", size_output)
        if match:
            info["displayWidth"] = int(match.group(1))
            info["displayHeight"] = int(match.group(2))
    except Exception:
        pass

    try:
        focus_output = _adb_text(["shell", "dumpsys", "window", "windows"], serial=serial, timeout=30)
        match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/", focus_output)
        if match:
            info["currentPackageName"] = match.group(1)
    except Exception:
        pass

    return info


def acquire_gui_information_adb(
    save_path: str,
    serial: Optional[str] = None,
    ocr_items: Optional[List[Dict[str, Any]]] = None,
    capture_screenshot: bool = True,
    model_config: Optional[Dict[str, Any]] = None,
    capture_label: str = "gui_context",
) -> Tuple[Dict[str, Any], GuiContext, str]:
    """
    ADB fallback for GUI Information Acquisition.

    Paper mapping is identical to acquire_gui_information(), but data is
    collected with platform ADB commands instead of the uiautomator2 Python
    JSON-RPC bridge:
    - adb shell uiautomator dump
    - adb exec-out screencap -p
    - adb shell wm size / dumpsys window
    """
    os.makedirs(save_path, exist_ok=True)
    capture_paths = make_capture_paths(save_path, capture_label)
    hierarchy_path = capture_paths["hierarchy"]

    # Screenshot must be taken BEFORE uiautomator dump because
    # uiautomator dump can trigger UI highlights/refreshes that
    # alter the on-screen state.
    screenshot_path = None
    if capture_screenshot:
        screenshot_path = capture_paths["screenshot"]
        screenshot_path = capture_screenshot_adb(screenshot_path, serial=serial, timeout=30)
    if ocr_items is None:
        ocr_items = extract_text_from_screenshot(screenshot_path)

    remote_xml = "/sdcard/window_dump.xml"
    _adb_text(["shell", "uiautomator", "dump", remote_xml], serial=serial, timeout=30)
    page_source = _adb_text(["exec-out", "cat", remote_xml], serial=serial, timeout=30)
    with open(hierarchy_path, "w", encoding="utf-8") as xml_file:
        xml_file.write(page_source)

    data_dict = parse_hierarchy_xml(page_source)
    gui_context = build_gui_context(
        data_dict=data_dict,
        device_info=get_adb_device_info(serial),
        screenshot_path=screenshot_path,
        ocr_items=ocr_items,
        model_config=model_config,
    )

    return data_dict, gui_context, hierarchy_path


def execute_solution_adb(
    solution: Dict[str, Any],
    serial: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    ADB fallback for coordinate-based action execution.

    This is useful when uiautomator2/atx-agent fails but ADB itself works.
    """
    executed = []
    for action in solution.get("actions", []):
        if action.get("type") == "tap":
            x, y = action["point"]
            _adb_text(["shell", "input", "tap", str(int(x)), str(int(y))], serial=serial)
            executed.append(action)
        elif action.get("type") == "drag":
            start_x, start_y = action["start"]
            end_x, end_y = action["end"]
            duration_ms = int(action.get("duration", 0.8) * 1000)
            _adb_text(
                [
                    "shell",
                    "input",
                    "swipe",
                    str(int(start_x)),
                    str(int(start_y)),
                    str(int(end_x)),
                    str(int(end_y)),
                    str(duration_ms),
                ],
                serial=serial,
            )
            executed.append(action)
    return executed


def _self_test() -> None:
    sample = {
        "hierarchy": {
            "@class": "hierarchy",
            "@package": "com.example",
            "@bounds": "[0,0][1080,1920]",
            "node": {
                "@class": "android.widget.LinearLayout",
                "@package": "com.example",
                "@bounds": "[0,0][1080,1920]",
                "node": [
                    {
                        "@class": "android.widget.TextView",
                        "@package": "com.example",
                        "@resource-id": "captcha_instruction",
                        "@text": "drag the slider",
                        "@content-desc": "",
                        "@bounds": "[100,200][600,260]",
                        "@clickable": "false",
                        "@enabled": "true",
                        "@focusable": "false",
                    },
                    {
                        "@class": "android.view.View",
                        "@package": "com.example",
                        "@resource-id": "slider_track",
                        "@text": "",
                        "@bounds": "[100,900][900,940]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                    {
                        "@class": "android.widget.Button",
                        "@package": "com.example",
                        "@resource-id": "verify_button",
                        "@text": "verify",
                        "@bounds": "[700,1600][980,1700]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                ],
            },
        }
    }
    context = build_gui_context(sample, {"displayWidth": 1080, "displayHeight": 1920},
                                 model_config={"provider": "deterministic", "enabled": False,
                                               "model": None, "thinking_enabled": False})
    assert len(getAllComponents(sample)) == 3
    assert len(getAllComponents(sample, include_containers=True)) == 5
    assert find_EditText(sample) == []
    assert get_basic_info(getAllComponents(sample)[0])["id"] == "captcha_instruction"
    assert len(context["interactive_candidates"]) == 3
    assert context["global_interface_context"]["screen_size"] == {"width": 1080, "height": 1920}
    assert context["component_textual_semantics"][0]["text"] == "drag the slider"
    assert context["captcha_recognition"]["is_captcha"] is True
    assert context["captcha_recognition"]["captcha_type"] == "slider"
    assert context["captcha_detection"]["is_captcha_candidate"] is True
    assert context["captcha_solution"]["captcha_type"] == "slider"
    assert context["captcha_solution"]["status"] == "needs_mllm_gap_estimation"
    context["mllm_result"] = {"target_gap_center": (760, 920)}
    solved_with_visual_target = solve_captcha(context)
    assert solved_with_visual_target["actions"][0]["type"] == "drag"
    assert solved_with_visual_target["actions"][0]["trajectory"]
    assert context["verification_feedback"]["status"] == "not_run"
    assert check_type_verification(getAllComponents(sample)) == 2

    after_sample = {
        "hierarchy": {
            "@class": "hierarchy",
            "@package": "com.example",
            "@bounds": "[0,0][1080,1920]",
            "node": {
                "@class": "android.widget.TextView",
                "@package": "com.example",
                "@resource-id": "success_text",
                "@text": "verification success",
                "@bounds": "[100,200][600,260]",
                "@clickable": "false",
                "@enabled": "true",
                "@focusable": "false",
            },
        }
    }
    after_context = build_gui_context(after_sample, {"displayWidth": 1080, "displayHeight": 1920},
                                       model_config={"provider": "deterministic", "enabled": False,
                                                     "model": None, "thinking_enabled": False})
    feedback_loop = verify_and_refine(context, after_context, context["captcha_solution"])
    assert feedback_loop["verification"]["success"] is True
    assert feedback_loop["refinement"]["next_step"] == "stop"

    launcher_sample = {
        "hierarchy": {
            "@class": "hierarchy",
            "@package": "com.android.launcher3",
            "@bounds": "[0,0][1080,1920]",
            "node": {
                "@class": "android.widget.FrameLayout",
                "@package": "com.android.launcher3",
                "@bounds": "[0,0][1080,1920]",
                "node": [
                    {
                        "@class": "android.widget.ImageView",
                        "@package": "com.android.launcher3",
                        "@resource-id": "",
                        "@text": "QQ音乐",
                        "@content-desc": "QQ音乐",
                        "@bounds": "[540,842][786,1105]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                    {
                        "@class": "android.widget.ImageView",
                        "@package": "com.android.launcher3",
                        "@resource-id": "",
                        "@text": "百度网盘",
                        "@content-desc": "百度网盘",
                        "@bounds": "[786,579][1032,842]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                    {
                        "@class": "android.widget.ImageView",
                        "@package": "com.android.launcher3",
                        "@resource-id": "",
                        "@text": "抖音",
                        "@content-desc": "抖音",
                        "@bounds": "[48,842][294,1105]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                    {
                        "@class": "android.widget.ImageView",
                        "@package": "com.android.launcher3",
                        "@resource-id": "",
                        "@text": "哔哩哔哩",
                        "@content-desc": "哔哩哔哩",
                        "@bounds": "[540,579][786,842]",
                        "@clickable": "true",
                        "@enabled": "true",
                        "@focusable": "true",
                    },
                ],
            },
        }
    }
    launcher_context = build_gui_context(
        launcher_sample,
        {
            "displayWidth": 1080,
            "displayHeight": 1920,
            "currentPackageName": "com.android.launcher3",
        },
        model_config={"provider": "deterministic", "enabled": False,
                      "model": None, "thinking_enabled": False},
    )
    assert launcher_context["captcha_recognition"]["is_captcha"] is False
    assert launcher_context["captcha_recognition"]["captcha_type"] == "none"
    assert launcher_context["captcha_solution"]["status"] == "skipped_non_captcha"
    assert launcher_context["captcha_solution"]["actions"] == []
    assert any(row["member_count"] >= 2 for row in launcher_context["semantic_rows"])
    print("GUI Information Acquisition, CAPTCHA Recognition, CAPTCHA Solving, and Feedback Loop self-test passed")


if __name__ == "__main__":
    _self_test()
