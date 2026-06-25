"""
Chat-refine: Claude reads the user's plain-English request + current config and returns
a structured patch to the config (it edits PARAMETERS, never runs arbitrary code).
Uses the Anthropic Messages API with a single tool.
"""
import json, copy
try:
    import anthropic
except Exception:
    anthropic = None

MODEL = "claude-sonnet-4-6"

CONFIG_SCHEMA_DOC = """
You tune a sonographer-routing plan by editing a JSON config. Available knobs:

GLOBAL
- value_weight (int, ~5-100): how strongly higher recommended_events_per_year is prioritized vs. driving. Higher = chase high-rec towns even if farther.
- rotation_weeks (6/8/10): cycle length.
- urban: {"city_contains": "<prefix>", "max_events_per_year": <int>} or null. Caps dense-urban ZIPs (e.g. "Chicago city") to a lower cadence.
- rec_bumps: {"map": {"4":5,"5":6}, "add": {"10-12":1,"13-999":2}} — raise model recommendations for non-urban towns so techs have enough close work.
- closest_lock: {"tech":"<name>","within_min":<int>} or null. Towns within N min of this tech AND closest to them are locked to that tech (use for a drive-averse tech).
- frontier: {"tech":"<name>","budget":<visits/yr>,"max_min":<int>,"min_rec":4} or null. Lets one travel-willing tech take a few far new-region "seed" towns as overnights.

PER TECH (in config["techs"], match by "name")
- hard_cap (int minutes): max one-way drive this tech will do.
- aversion (float 0-3): how much this tech dislikes driving. Higher = avoids long days; their long trips get pushed to less-averse techs.
- days_per_week (int): sets annual capacity (days*52).
- overnight_ok (bool).

PRINCIPLES
- "Make X more comfortable / X hates driving" -> raise X.aversion and/or set closest_lock to X with a sensible within_min (e.g. 25), and/or lower X.hard_cap.
- "Share load between A and B" -> nudge the more-loaded tech's aversion up and the lighter tech's down.
- "Cut <urban area>" -> set urban cap (2 or 1). If techs would be idle, also add rec_bumps so suburban demand fills them.
- "Prioritize closest first" works against techs who live far from demand — explain the tradeoff rather than over-forcing.
Only change what the user asked for. Keep other fields intact.
"""

PATCH_TOOL = {
    "name": "update_config",
    "description": "Apply changes to the routing-plan config and trigger a re-run. Provide ONLY the fields to change (deep-merged into current config). Also provide a short plain-English explanation of what you changed and the expected effect/tradeoff.",
    "input_schema": {
        "type": "object",
        "properties": {
            "patch": {"type": "object", "description": "Partial config to deep-merge (e.g. {'techs':[{'name':'Kim','aversion':2.5}], 'closest_lock':{'tech':'Kim','within_min':25}})."},
            "explanation": {"type": "string", "description": "1-3 sentences: what changed and the expected effect/tradeoff."}
        },
        "required": ["patch", "explanation"]
    }
}

def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if k == "techs" and isinstance(v, list):
            by = {t["name"]: t for t in out.get("techs", [])}
            for tp in v:
                by.setdefault(tp.get("name"), {"name": tp.get("name")}).update(tp)
            out["techs"] = list(by.values())
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def refine(api_key, user_message, config, metrics_summary):
    """Returns (new_config, explanation). Falls back to no-op if anthropic unavailable."""
    if anthropic is None:
        return config, "Anthropic SDK not installed — run `pip install anthropic`."
    client = anthropic.Anthropic(api_key=api_key)
    sys = ("You are a scheduling copilot for a mobile-mammography routing tool. "
           "Translate the user's request into a config patch using the update_config tool.\n" + CONFIG_SCHEMA_DOC)
    context = (f"CURRENT CONFIG:\n{json.dumps(config, indent=2)}\n\n"
               f"CURRENT TECH METRICS:\n{json.dumps(metrics_summary, indent=2)}\n\n"
               f"USER REQUEST:\n{user_message}")
    msg = client.messages.create(
        model=MODEL, max_tokens=1500, system=sys,
        tools=[PATCH_TOOL], tool_choice={"type": "tool", "name": "update_config"},
        messages=[{"role": "user", "content": context}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "update_config":
            patch = block.input.get("patch", {}); expl = block.input.get("explanation", "")
            return _deep_merge(config, patch), expl
    return config, "No change proposed."


# ----------------------------- natural-language tech parser -----------------------------
TECH_TOOL = {
    "name": "set_techs",
    "description": "Convert free-text descriptions of each field rep into structured tech configs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "techs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "home_zip": {"type": "string", "description": "5-digit home ZIP if stated (else empty)."},
                        "home_address": {"type": "string", "description": "Full street/city address if stated (else empty). Prefer this when given."},
                        "days_per_week": {"type": "integer"},
                        "working_days": {"type": "array", "items": {"type": "string", "enum": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]},
                                         "description": "Specific working weekdays. If only a day off is stated (e.g. 'off Fridays'), list the remaining weekdays for the given days_per_week (default start Monday)."},
                        "hard_cap": {"type": "integer", "description": "Max one-way drive minutes. Infer: 'strict no travel'≈60, 'no overnight but long day-trips ok'≈120, 'will travel/overnight'≈150+."},
                        "aversion": {"type": "number", "description": "0=fine with driving, 3=strongly dislikes. 'strict no travel'/'older, hates driving'≈2.5-3; 'will travel'≈0.5-1."},
                        "overnight_ok": {"type": "boolean"},
                        "far_travel": {"type": "string", "enum": ["none","limited","willing"],
                                       "description": "Willingness for far new-region trips: 'no travel'=none, 'limited basis'=limited, 'will travel with notice'=willing."},
                        "notes": {"type": "string", "description": "Any nuance you couldn't fully encode."}
                    },
                    "required": ["name","days_per_week","hard_cap","aversion","overnight_ok"]
                }
            },
            "explanation": {"type": "string", "description": "Brief summary of how you interpreted each rep, so the user can verify/correct."}
        },
        "required": ["techs","explanation"]
    }
}

def parse_techs(api_key, instructions):
    """Free-text rep descriptions -> (list_of_tech_dicts, explanation)."""
    if anthropic is None:
        return [], "Anthropic SDK not installed — run `pip install anthropic`."
    client = anthropic.Anthropic(api_key=api_key)
    sys = ("You translate plain-English descriptions of mobile-mammography field reps into structured "
           "scheduling parameters using the set_techs tool. Be faithful to what's stated and make reasonable, "
           "clearly-explained inferences for anything implied (driving tolerance, overnight, working days). "
           "If a home ZIP or city is given, capture the ZIP; if only a city, leave home_zip empty and note it.")
    msg = client.messages.create(
        model=MODEL, max_tokens=2000, system=sys,
        tools=[TECH_TOOL], tool_choice={"type": "tool", "name": "set_techs"},
        messages=[{"role": "user", "content": instructions}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "set_techs":
            techs = block.input["techs"]
            for t in techs:                       # map far_travel -> frontier eligibility flag
                t["frontier_ok"] = t.get("far_travel") in ("limited", "willing")
            return techs, block.input.get("explanation", "")
    return [], "Could not parse."
