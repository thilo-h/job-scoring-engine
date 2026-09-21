"""Per-job Chat Assistant — Claude Sonnet with multi-mode personalities.

Three modes share the same conversation infrastructure but get different
system prompts and tools:

  - ``letter_review`` — react to a draft the applicant already wrote. Can
    return a ``suggest_revision`` tool call with the revised markdown, which
    the UI surfaces as a proposal with an explicit "Apply" button. It refuses
    when there is no draft: this mode revises, it does not author.
  - ``interview_prep`` — interview preparation, mock questions, company
    research. Has access to ``web_search`` for interviewer/company lookups.
  - ``application_advisor`` — explain how to apply, which documents to
    attach, what the apply-method JSON says. No tools, just guidance.

Each user message + assistant reply is persisted in ``job_chat_messages``
keyed by (job_id, mode), so the dialogue survives page reloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-sonnet-5"
# Thinking and response share this budget on this model, and a revision returns
# the whole letter. 2048 was tight even before thinking counted against it.
MAX_TOKENS = 8000

# Sonnet 4.6 pricing
PRICE_INPUT_PER_MTOK = 2.00
PRICE_OUTPUT_PER_MTOK = 10.00
PRICE_CACHE_READ_PER_MTOK = 0.20   # 10% of input
PRICE_CACHE_WRITE_PER_MTOK = 2.50  # 1.25x input


CHAT_MODES = ("letter_review", "interview_prep", "application_advisor")


@dataclass
class ChatTurn:
    reply_text: str
    proposed_edit: Optional[str] = None  # full-markdown cover letter draft
    web_searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


def _system_prompt_for(
    mode: str,
    *,
    profile_name: str,
    background: str,
    job: dict,
    cover_letter: Optional[str],
    apply_method: Optional[dict],
    attachment_catalog: list[dict],
) -> str:
    """Build the mode-specific system prompt with job + profile context."""
    bg = background.strip() or f"{profile_name} — siehe CV (nicht hier eingebettet)."
    job_blk = (
        f"## Aktuelle Stelle (Job-ID {job.get('id')})\n"
        f"- Title: {job.get('title','')}\n"
        f"- Company: {job.get('company','')}\n"
        f"- Location: {job.get('location','') or '(unbekannt)'}\n"
        f"- URL: {job.get('url','') or '(keine)'}\n"
        f"- Score: {job.get('relevance_score','—')} / Match: {job.get('match_score','—')}\n"
        "\n### Description\n"
        f"{(job.get('description') or '(keine Description gescraped)')[:4000]}"
    )

    profile_blk = (
        f"## Profil\nName: {profile_name}\n\n{bg}"
    )

    if mode == "letter_review":
        has_draft = bool((cover_letter or "").strip())
        body = f"""Du redigierst einen Brief, den {profile_name} selbst
geschrieben hat. Du schreibst keinen Brief. Der Unterschied ist wichtig:

- **Kein Entwurf vorhanden?** Dann sage genau das und bitte um den Text —
  auch einen rohen ersten Versuch. Formuliere keinen Brief und keinen Absatz
  "als Vorschlag zum Anfangen". Ohne Entwurf gibt es nichts zu redigieren.
- **Entwurf vorhanden?** Arbeite ausschliesslich daran. Verbessere, was der
  User anspricht, und lasse alles andere Wort für Wort stehen. Du kürzt,
  schärfst und korrigierst — du ersetzt nicht die Substanz durch eigene.
- Erfinde nie Belege. Was nicht im Entwurf oder im CV steht, kommt nicht
  hinein. Fällt dir eine unbelegte Behauptung des Users auf, benenne sie.

Fragt der User nach einer Änderung, liefere zwei Dinge:
1. Eine **kurze Begründung** im Antworttext (1–3 Sätze): was du geändert hast
   und warum. Nenne die Regel aus dem Stil-Leitfaden, wenn eine greift.
2. Die **überarbeitete Fassung** über das Tool `suggest_revision` — nie im
   Antworttext. Es ist ein Vorschlag: der User sieht ihn und entscheidet.

Stellt der User eine Frage oder will nur Feedback, antworte normal, ohne
Tool-Call. Ein Vorschlag ohne Auftrag ist ein Übergriff.

Struktur der überarbeiteten Fassung: dieselbe Markdown-Struktur wie der
Entwurf (date_line, Anrede, Intro, Absätze oder Sections mit Bullets,
Closing, Signoff, Name).

Aktueller Entwurf des Users:
```markdown
{cover_letter if has_draft else '(kein Entwurf gespeichert — bitte um den Text, schreibe keinen)'}
```

{job_blk}

{profile_blk}

Sei knapp und präzise. Antworte in der Sprache des Users."""

    elif mode == "interview_prep":
        body = f"""Du bist ein Interview-Coach für {profile_name}. Bereite auf
das Gespräch zu der unten genannten Stelle vor.

Du hast Zugriff auf `web_search` — nutze es um aktuelle Company-News,
Funding-Status, Produktrichtung zu recherchieren, oder wenn der User einen
konkreten Interviewer nennt (LinkedIn-Background).

Was du gut kannst:
- Wahrscheinliche Fragen für diese Rolle + Firma (technisch + behavioral)
- STAR-Antworten basierend auf {profile_name}'s Background
- Reverse-Fragen die {profile_name} der Firma stellen sollte
- Salary-Range Einschätzung anhand Standort/Firma/Rolle
- Red-Flags die im Interview auffallen könnten

{job_blk}

{profile_blk}

Antworte in der Sprache des Users. Knapp, action-orientiert."""

    elif mode == "application_advisor":
        am_blk = "(noch keine Apply-Method erkannt — User kann den 'Detect Apply Method'-Button drücken)"
        if apply_method:
            am_blk = (
                f"- Channel: {apply_method.get('primary_channel','?')}\n"
                f"- Email: {apply_method.get('email') or '—'}\n"
                f"- Portal-URL: {apply_method.get('portal_url') or '—'}\n"
                f"- Required: {', '.join(apply_method.get('required_documents') or []) or '—'}\n"
                f"- Notes: {apply_method.get('notes') or '—'}"
            )

        cat_blk = "(keine Anhang-Catalog konfiguriert)"
        if attachment_catalog:
            lines = [
                f"- {a['file']} (default={a.get('default', False)}, type={a.get('doc_type','?')}) — {a.get('label','')}"
                for a in attachment_catalog
            ]
            cat_blk = "\n".join(lines)

        body = f"""Du bist ein Application-Advisor für {profile_name}. Erkläre
**wie** man sich auf diese Stelle am besten bewirbt, und **welche
Dokumente** mitgeschickt werden sollten.

Apply-Method-Detection (was bisher erkannt wurde):
{am_blk}

Anhang-Catalog von {profile_name} (alle verfügbaren Files):
{cat_blk}

{job_blk}

{profile_blk}

Wenn der User fragt "wie bewerben?" → gib einen konkreten Schritt-für-Schritt
Plan zurück (Channel, Empfänger, Subject-Vorschlag, Liste der zu sendenden
Anhänge mit Begründung warum/warum nicht, ggf. Hinweis auf zeitliche Strategie).

Sei knapp und konkret. Antworte in der Sprache des Users."""

    else:
        raise ValueError(f"Unknown chat mode: {mode}")

    return body


class JobChatAgent:
    def __init__(self, api_key: Optional[str] = None, *, config: Optional[dict] = None):
        self.client = get_client(api_key, purpose="the job chat")

        profile = (config or {}).get("profile") or {}
        self._profile_name = profile.get("name", "User")
        self._background = profile.get("background_summary", "") or ""

    def reply(
        self,
        *,
        mode: str,
        job: dict,
        history: list[dict],
        user_message: str,
        cover_letter: Optional[str] = None,
        apply_method: Optional[dict] = None,
        attachment_catalog: Optional[list[dict]] = None,
    ) -> ChatTurn:
        """Send one user message + reply.

        ``history`` is a list of prior messages [{role: 'user'|'assistant',
        content: str}, ...] — only role + content matter for the API call.
        """
        if mode not in CHAT_MODES:
            raise ValueError(f"Invalid mode: {mode}")

        system_prompt = _system_prompt_for(
            mode,
            profile_name=self._profile_name,
            background=self._background,
            job=job,
            cover_letter=cover_letter,
            apply_method=apply_method,
            attachment_catalog=attachment_catalog or [],
        )

        # Build the messages list: prior turns then the new user message.
        # Each prior assistant turn is plain text; tool calls are not replayed.
        messages: list[dict] = []
        for m in history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        # Tool palette per mode.
        tools: list[dict] = []
        if mode == "letter_review":
            tools.append({
                "name": "suggest_revision",
                "description": (
                    "Propose a revised version of the letter the user wrote. "
                    "Only call this when the user actually asked for a change, "
                    "and only when a draft exists — skip the tool for questions, "
                    "for feedback, and when there is nothing to revise."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "new_markdown": {
                            "type": "string",
                            "description": "The revised letter as complete markdown. Keep every passage the user did not ask about byte-for-byte.",
                        },
                        "change_summary": {
                            "type": "string",
                            "description": "1-2 sentences explaining what changed.",
                        },
                    },
                    "required": ["new_markdown", "change_summary"],
                },
            })
        if mode == "interview_prep":
            # The newer tool version is available on this model and brings
            # dynamic filtering. Haiku callers keep the basic variant, which is
            # what that tier supports.
            tools.append({
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 3,
            })

        kwargs = dict(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Left adaptive rather than disabled: this is the one conversational
            # path in the project, and the quality gain is worth the latency.
            # Thinking text is not rendered, so `display` stays at its default.
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        response = self.client.messages.create(**kwargs)

        reply_text_parts: list[str] = []
        proposed_edit: Optional[str] = None
        web_searches = 0

        for block in response.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                reply_text_parts.append(block.text)
            elif btype == "tool_use" and getattr(block, "name", "") == "suggest_revision":
                data = block.input or {}
                proposed_edit = data.get("new_markdown") or None
                summary = data.get("change_summary") or ""
                if summary:
                    reply_text_parts.append(summary)
            elif btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                web_searches += 1

        reply_text = "\n\n".join(p.strip() for p in reply_text_parts if p.strip()).strip()
        if not reply_text:
            reply_text = "(no reply)"

        usage = response.usage
        input_tok = getattr(usage, "input_tokens", 0)
        output_tok = getattr(usage, "output_tokens", 0)
        cache_read_tok = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write_tok = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (
            input_tok * PRICE_INPUT_PER_MTOK / 1_000_000
            + cache_read_tok * PRICE_CACHE_READ_PER_MTOK / 1_000_000
            + cache_write_tok * PRICE_CACHE_WRITE_PER_MTOK / 1_000_000
            + output_tok * PRICE_OUTPUT_PER_MTOK / 1_000_000
        )

        logger.info(
            "chat(%s, job=%s, history=%d) → %d chars, edit=%s, web=%d, $%.4f",
            mode, job.get("id"), len(history), len(reply_text),
            bool(proposed_edit), web_searches, cost,
        )

        return ChatTurn(
            reply_text=reply_text,
            proposed_edit=proposed_edit,
            web_searches=web_searches,
            input_tokens=input_tok,
            output_tokens=output_tok,
            cache_read_tokens=cache_read_tok,
            cache_write_tokens=cache_write_tok,
            cost_usd=cost,
        )
