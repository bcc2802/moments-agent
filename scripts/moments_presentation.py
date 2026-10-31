"""Response views only: never change the validated pool, artifacts or state."""
from copy import deepcopy


def draft_delivery():
    return {"text_ready": True, "next_action": "DISPLAY_DRAFTS_NOW",
            "media_stage": "SEPARATE", "display_confirmed": False,
            "instruction": "立即展示已保存的完整三稿，再独立继续配图；仅返回数据不证明用户已经看到。"}


def present(action, result, view="full"):
    if view not in {"full", "compact"}:
        raise ValueError("response_view must be full or compact")
    if view == "full" or not isinstance(result, dict):
        return result
    value = deepcopy(result)
    if action == "workflow-resume" and value.get("stage") == "CURATED":
        pool = value.pop("pool", {})
        value["pool_summary"] = {k: pool[k] for k in
            ("pool_id", "fetched_at", "counts", "source_reports") if k in pool}
        if isinstance(value.get("menu"), dict):
            value["menu"].pop("audit", None)
    # Full audit stays in the checksummed run; the displayed menu is unchanged.
    if action in {"workflow-curate", "curate-hot"}:
        value.pop("audit", None)
    return value
