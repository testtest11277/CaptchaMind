# Prompt Collection

This directory contains the prompts used by the CAPTCHAMind pipeline and the direct screenshot comparison experiments.

## Files

| File | Condition | Purpose |
| --- | --- | --- |
| `01_tool_captcha_type_inference.md` | CAPTCHAMind | CAPTCHA detection and type inference from structured GUI context and retrieved examples |
| `02_tool_captcha_solving.md` | CAPTCHAMind | CAPTCHA-specific action planning and coordinate grounding |
| `03_baseline_adb_generation.txt` | Direct screenshot comparison | Generate an executable ADB command sequence from a screenshot |
| `04_baseline_captcha_detection.txt` | Direct screenshot comparison | Determine whether a screenshot contains a CAPTCHA |
| `05_baseline_captcha_type.txt` | Direct screenshot comparison | Infer the CAPTCHA type from a screenshot |

The direct screenshot prompts do not provide the CAPTCHA type to the model before inference or action generation. The type may be added later as a ground-truth annotation for evaluation.

The two CAPTCHAMind prompts use structured GUI information because they correspond to the proposed method rather than the screenshot-only comparison condition.

