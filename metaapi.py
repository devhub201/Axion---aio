"""Pure helpers for Meta's Responses-style API (POST https://api.meta.ai/v1/responses). No third-party imports."""


def structured_input(messages):
    """OpenAI/Meta Responses format: [{"role", "content": [{"type": input_text|output_text, "text"}]}]."""
    out = []
    for m in messages:
        role = m["role"] if m["role"] in ("system", "user", "assistant", "developer") else "user"
        ctype = "output_text" if role == "assistant" else "input_text"
        out.append({"role": role, "content": [{"type": ctype, "text": str(m["content"])}]})
    return out


def flat_input(messages):
    """Fallback that only needs one plain user message: system prompt + transcript folded together."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = []
    for m in messages:
        if m["role"] == "system":
            continue
        who = "Assistant" if m["role"] == "assistant" else "User"
        turns.append(f"{who}: {m['content']}")
    text = (f"{system}\n\n" if system else "") + "\n".join(turns)
    if turns and not turns[-1].startswith("Assistant"):
        text += "\nAssistant:"
    return [{"role": "user", "content": [{"type": "input_text", "text": text}]}]


def extract_text(data):
    """Pull the answer text out of a Responses-style (or chat.completions-style) JSON body. '' if none."""
    if not isinstance(data, dict):
        return ""
    ot = data.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot.strip()
    parts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") in ("reasoning", "function_call"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") in (None, "output_text", "text") and isinstance(c.get("text"), str):
                    parts.append(c["text"])
    if parts:
        return "".join(parts).strip()
    try:
        msg = data["choices"][0]["message"]["content"]
        if isinstance(msg, list):
            msg = "".join(p.get("text", "") for p in msg if isinstance(p, dict))
        return (msg or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""
