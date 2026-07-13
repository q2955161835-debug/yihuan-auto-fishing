from auto_fishing.model import NormalizedRect


TOP_ROI = NormalizedRect(0.24, 0.00, 0.76, 0.15)
BITE_ROI = NormalizedRect(0.89, 0.79, 0.99, 0.99)
REEL_PROMPT_ROI = NormalizedRect(0.22, 0.16, 0.60, 0.24)
READY_ROI = NormalizedRect(0.84, 0.68, 1.00, 1.00)
READY_HOOK_ROI = NormalizedRect(0.90, 0.75, 0.99, 0.95)
RESULT_ROI = NormalizedRect(0.25, 0.05, 0.75, 0.95)
RESULT_CENTER_ROI = NormalizedRect(0.35, 0.20, 0.65, 0.72)
RESULT_HEADER_ROI = NormalizedRect(0.38, 0.04, 0.62, 0.13)
